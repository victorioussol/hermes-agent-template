import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

TEST_HOME = tempfile.mkdtemp(prefix="hermes-wrapper-tests-")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("HERMES_HOME", TEST_HOME)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server


def request(path="/setup/api/config", method="GET", headers=None, body=b""):
    raw_headers = [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("hermes.example", 443),
    }
    return server.Request(scope, receive)


class IsolatedHermesHome(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name)
        self.originals = {
            "HERMES_HOME": server.HERMES_HOME,
            "ENV_FILE": server.ENV_FILE,
            "PAIRING_DIR": server.PAIRING_DIR,
            "APP_OPS_DIR": server._APP_OPS_DIR,
            "APP_OPS_RUNS_DIR": server._APP_OPS_RUNS_DIR,
            "APP_OPS_LOG": server._APP_OPS_LOG,
        }
        server.HERMES_HOME = str(self.home)
        server.ENV_FILE = self.home / ".env"
        server.PAIRING_DIR = self.home / "platforms" / "pairing"
        server._APP_OPS_DIR = self.home / "app-ops-action-inbox"
        server._APP_OPS_RUNS_DIR = server._APP_OPS_DIR / "runs"
        server._APP_OPS_RUNS_DIR.mkdir(parents=True)
        server._APP_OPS_LOG = server._APP_OPS_DIR / "actions.jsonl"

    def tearDown(self):
        for key, value in self.originals.items():
            setattr(server, key, value)
        self.tempdir.cleanup()


class ConfigTests(IsolatedHermesHome):
    def test_new_scheduled_job_failure_is_visible_without_sensitive_details(self):
        cron_dir = self.home / "cron"
        cron_dir.mkdir()
        (cron_dir / "jobs.json").write_text(json.dumps([{
            "id": "private-job-id",
            "enabled": True,
            "last_status": "error",
            "last_error": "secret provider response",
            "last_run_at": datetime.now(timezone.utc).isoformat(),
        }]))
        status = server.cron_health_status()
        self.assertEqual(status["new_failures"], 1)
        self.assertEqual(status["failure_kinds"]["other"], 1)
        self.assertFalse(status["healthy"])
        self.assertNotIn("private-job-id", json.dumps(status))
        self.assertNotIn("secret provider response", json.dumps(status))

    def test_provider_auth_failure_is_safely_classified(self):
        cron_dir = self.home / "cron"
        cron_dir.mkdir()
        (cron_dir / "jobs.json").write_text(json.dumps([{
            "id": "private-job-id",
            "enabled": True,
            "last_status": "error",
            "last_error": "Provider authentication failed: secret-token-value",
            "last_run_at": datetime.now(timezone.utc).isoformat(),
        }]))
        status = server.cron_health_status()
        self.assertEqual(status["failure_kinds"]["provider_auth"], 1)
        self.assertNotIn("secret-token-value", json.dumps(status))

    def test_fallback_requires_fresh_five_dollar_monthly_receipt(self):
        (self.home / ".env").write_text("OPENROUTER_API_KEY=private-key\n")
        (self.home / "openrouter-budget.json").write_text(json.dumps({
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "status": "ok",
            "limit_usd": 5,
            "limit_reset": "monthly",
            "remaining_usd": 4.95,
        }))
        status = server.fallback_health_status()
        self.assertTrue(status["ready"])
        self.assertEqual(status["model"], "deepseek/deepseek-v4-flash-0731")
        self.assertNotIn("private-key", json.dumps(status))

    def test_fallback_rejects_uncapped_receipt(self):
        (self.home / ".env").write_text("OPENROUTER_API_KEY=private-key\n")
        (self.home / "openrouter-budget.json").write_text(json.dumps({
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "status": "ok",
            "limit_usd": None,
            "limit_reset": None,
        }))
        self.assertFalse(server.fallback_health_status()["ready"])

    def test_fallback_rejects_exhausted_five_dollar_receipt(self):
        (self.home / ".env").write_text("OPENROUTER_API_KEY=private-key\n")
        (self.home / "openrouter-budget.json").write_text(json.dumps({
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "status": "critical",
            "limit_usd": 5,
            "limit_reset": "monthly",
            "usage_monthly_usd": 5,
        }))
        self.assertFalse(server.fallback_health_status()["ready"])

    def test_historical_scheduled_job_failure_does_not_block_deploy(self):
        cron_dir = self.home / "cron"
        cron_dir.mkdir()
        (cron_dir / "jobs.json").write_text(json.dumps([{
            "id": "old-job",
            "enabled": True,
            "last_status": "error",
            "last_run_at": "2000-01-01T00:00:00+00:00",
        }]))
        status = server.cron_health_status()
        self.assertEqual(status["new_failures"], 0)
        self.assertTrue(status["healthy"])

    def test_codex_provider_survives_openrouter_key(self):
        (self.home / "config.yaml").write_text(
            "model:\n"
            "  default: old-model\n"
            "  provider: openai-codex\n"
            "  base_url: https://openrouter.ai/api/v1\n"
            "  api_key: leaked-inline-key\n"
        )
        with patch.dict(os.environ, {"HERMES_MODEL_PROVIDER": ""}):
            server.write_config_yaml({
                "LLM_MODEL": "gpt-5.6-terra",
                "OPENROUTER_API_KEY": "openrouter-key",
            })

        import yaml
        model = yaml.safe_load((self.home / "config.yaml").read_text())["model"]
        self.assertEqual(model["provider"], "openai-codex")
        self.assertEqual(model["default"], "gpt-5.6-terra")
        self.assertNotIn("base_url", model)
        self.assertNotIn("api_key", model)

    def test_explicit_provider_override_wins(self):
        server.write_config_yaml({
            "LLM_MODEL": "gpt-5.6-terra",
            "HERMES_MODEL_PROVIDER": "openai-codex",
            "OPENROUTER_API_KEY": "openrouter-key",
        })
        import yaml
        model = yaml.safe_load((self.home / "config.yaml").read_text())["model"]
        self.assertEqual(model["provider"], "openai-codex")

    def test_railway_provider_and_model_override_persisted_values(self):
        with patch.dict(os.environ, {
            "HERMES_MODEL_PROVIDER": "openai-codex",
            "LLM_MODEL": "gpt-5.6-terra",
        }):
            server.write_config_yaml({
                "LLM_MODEL": "openrouter/other-model",
                "HERMES_MODEL_PROVIDER": "openrouter",
                "OPENROUTER_API_KEY": "openrouter-key",
            })
        import yaml
        model = yaml.safe_load((self.home / "config.yaml").read_text())["model"]
        self.assertEqual(model, {"default": "gpt-5.6-terra", "provider": "openai-codex"})

    def test_new_config_defaults_to_auto_without_explicit_provider(self):
        with patch.dict(os.environ, {"HERMES_MODEL_PROVIDER": ""}):
            server.write_config_yaml({
                "LLM_MODEL": "openrouter/model",
                "OPENROUTER_API_KEY": "openrouter-key",
            })
        import yaml
        model = yaml.safe_load((self.home / "config.yaml").read_text())["model"]
        self.assertEqual(model["provider"], "auto")

    def test_non_retired_mcp_server_survives_config_merge(self):
        (self.home / "config.yaml").write_text(
            "mcp_servers:\n"
            "  example-search:\n"
            "    url: https://mcp.example.com/search\n"
        )
        server.write_config_yaml({
            "LLM_MODEL": "gpt-5.6-terra",
            "HERMES_MODEL_PROVIDER": "openai-codex",
        })
        import yaml
        server_config = yaml.safe_load((self.home / "config.yaml").read_text())["mcp_servers"]["example-search"]
        self.assertEqual(server_config["url"], "https://mcp.example.com/search")

    def test_retired_typefully_server_and_secret_are_removed(self):
        (self.home / "config.yaml").write_text(
            "mcp_servers:\n"
            "  typefully:\n"
            "    url: https://mcp.typefully.com/mcp?TYPEFULLY_API_KEY=test-typefully-key\n"
            "  example-search:\n"
            "    url: https://mcp.example.com/search\n"
        )
        server.write_env(self.home / ".env", {
            "TYPEFULLY_API_KEY": "test-typefully-key",
            "TAVILY_API_KEY": "keep-this-key",
        })
        self.assertTrue(server.remove_typefully_configuration())

        import yaml
        config = yaml.safe_load((self.home / "config.yaml").read_text())
        self.assertNotIn("typefully", config["mcp_servers"])
        self.assertIn("example-search", config["mcp_servers"])
        env = server.read_env(self.home / ".env")
        self.assertNotIn("TYPEFULLY_API_KEY", env)
        self.assertEqual(env["TAVILY_API_KEY"], "keep-this-key")
        self.assertEqual((self.home / ".env").stat().st_mode & 0o777, 0o600)
        self.assertNotIn("test-typefully-key", (self.home / "config.yaml").read_text())
        cleaned_config = (self.home / "config.yaml").read_text()
        self.assertFalse(server.remove_typefully_configuration())
        self.assertEqual((self.home / "config.yaml").read_text(), cleaned_config)
        with patch.dict(os.environ, {"TYPEFULLY_API_KEY": "railway-secret"}):
            self.assertNotIn("TYPEFULLY_API_KEY", server.build_hermes_env())

    def test_pinned_oauth_provider_is_complete_without_api_key(self):
        (self.home / "config.yaml").write_text(
            "model:\n  default: gpt-5.6-terra\n  provider: openai-codex\n"
        )
        self.assertTrue(server.is_config_complete({"LLM_MODEL": "gpt-5.6-terra"}))

    def test_unknown_secret_names_are_masked_and_hidden(self):
        raw = {
            "LLM_MODEL": "gpt-5.6-terra",
            "CAREER_OPS_WEBHOOK_SECRET": "career-secret-value",
            "HERMES_ACTION_KEY": "action-secret-value",
        }
        masked = server.mask(raw)
        self.assertEqual(masked["CAREER_OPS_WEBHOOK_SECRET"], "***")
        self.assertEqual(masked["HERMES_ACTION_KEY"], "***")
        self.assertEqual(server.visible_config(raw), {"LLM_MODEL": "gpt-5.6-terra"})

    def test_env_writer_rejects_newlines_and_sets_private_mode(self):
        with self.assertRaises(ValueError):
            server.write_env(self.home / ".env", {"LLM_MODEL": "gpt-5.6-terra\nINJECTED=true"})
        server.write_env(self.home / ".env", {"LLM_MODEL": "gpt-5.6-terra"})
        self.assertEqual((self.home / ".env").stat().st_mode & 0o777, 0o600)

    def test_invalid_yaml_is_never_overwritten(self):
        config_path = self.home / "config.yaml"
        invalid = "model: [unterminated\n"
        config_path.write_text(invalid)
        with self.assertRaises(RuntimeError):
            server.write_config_yaml({"LLM_MODEL": "gpt-5.6-terra"})
        self.assertEqual(config_path.read_text(), invalid)

    def test_hermes_environment_excludes_wrapper_and_railway_secrets(self):
        server.write_env(self.home / ".env", {
            "LLM_MODEL": "gpt-5.6-terra",
            "HERMES_ACTION_WEBHOOK_SECRET": "persisted-webhook-secret",
            "TAVILY_API_KEY": "search-key",
        })
        with patch.dict(os.environ, {
            "ADMIN_PASSWORD": "admin-secret",
            "RAILWAY_TOKEN": "railway-secret",
            "RAILWAY_API_TOKEN": "railway-api-secret",
            "SUPABASE_SERVICE_ROLE_KEY": "supabase-service-secret",
            "HERMES_COO_WATCHDOG_ENABLED": "true",
            "UNRELATED_SECRET": "unrelated-secret",
            "HERMES_MODEL_PROVIDER": "openai-codex",
        }):
            env = server.build_hermes_env()
        self.assertEqual(env["HERMES_MODEL_PROVIDER"], "openai-codex")
        self.assertEqual(env["TAVILY_API_KEY"], "search-key")
        for key in (
            "ADMIN_PASSWORD",
            "RAILWAY_TOKEN",
            "RAILWAY_API_TOKEN",
            "SUPABASE_SERVICE_ROLE_KEY",
            "HERMES_COO_WATCHDOG_ENABLED",
            "UNRELATED_SECRET",
            "HERMES_ACTION_WEBHOOK_SECRET",
        ):
            self.assertNotIn(key, env)

    def test_railway_only_official_voice_and_idle_settings_propagate(self):
        optional_keys = self.home / "optional-env-keys"
        optional_keys.write_text("MISTRAL_API_KEY\n")
        with (
            patch.object(server, "HERMES_OPTIONAL_ENV_KEYS_FILE", optional_keys),
            patch.dict(os.environ, {
                "ELEVENLABS_API_KEY": "voice-key",
                "MISTRAL_API_KEY": "speech-key",
                "HERMES_DASHBOARD_IDLE_SECONDS": "0",
                "UNRELATED_RAILWAY_SECRET": "must-not-pass",
            }),
        ):
            env = server.build_hermes_env()
            dashboard = server.Dashboard()
        self.assertEqual(env["ELEVENLABS_API_KEY"], "voice-key")
        self.assertEqual(env["MISTRAL_API_KEY"], "speech-key")
        self.assertEqual(env["HERMES_DASHBOARD_IDLE_SECONDS"], "0")
        self.assertNotIn("UNRELATED_RAILWAY_SECRET", env)
        self.assertEqual(dashboard.idle_seconds, 0)

    def test_deployment_mailbox_app_password_is_allowlisted(self):
        optional_keys = self.home / "optional-env-keys"
        optional_keys.write_text("HERMES_GMAIL_APP_PASSWORD\n")
        with (
            patch.object(server, "HERMES_OPTIONAL_ENV_KEYS_FILE", optional_keys),
            patch.dict(os.environ, {
                "HERMES_GMAIL_APP_PASSWORD": "mailbox-app-password",
                "UNRELATED_RAILWAY_SECRET": "must-not-pass",
            }),
        ):
            env = server.build_hermes_env()
        self.assertEqual(env["HERMES_GMAIL_APP_PASSWORD"], "mailbox-app-password")
        self.assertNotIn("UNRELATED_RAILWAY_SECRET", env)

    def test_app_ops_environment_excludes_channel_and_unapproved_credentials(self):
        server.write_env(self.home / ".env", {
            "LLM_MODEL": "gpt-5.6-terra",
            "HERMES_MODEL_PROVIDER": "openai-codex",
            "TELEGRAM_BOT_TOKEN": "telegram-secret",
            "SOCIAL_MEDIA_API_KEY": "publishing-secret",
            "TAVILY_API_KEY": "search-key",
        })
        env = server.build_app_ops_env()
        self.assertEqual(env["TAVILY_API_KEY"], "search-key")
        self.assertNotIn("TELEGRAM_BOT_TOKEN", env)
        self.assertNotIn("SOCIAL_MEDIA_API_KEY", env)

    def test_app_ops_profile_persists_only_read_only_worker_settings(self):
        server.write_env(self.home / ".env", {
            "LLM_MODEL": "gpt-5.6-terra",
            "HERMES_MODEL_PROVIDER": "openai-codex",
            "TELEGRAM_BOT_TOKEN": "telegram-secret",
            "SOCIAL_MEDIA_API_KEY": "publishing-secret",
            "TAVILY_API_KEY": "search-key",
        })
        env = server._prepare_app_ops_runtime_env()
        profile_home = Path(env["HERMES_HOME"])
        profile_env = server.read_env(profile_home / ".env")
        self.assertEqual(profile_env["TAVILY_API_KEY"], "search-key")
        self.assertNotIn("TELEGRAM_BOT_TOKEN", profile_env)
        self.assertNotIn("SOCIAL_MEDIA_API_KEY", profile_env)
        profile_config = (profile_home / "config.yaml").read_text()
        self.assertIn("default: gpt-5.6-terra", profile_config)
        self.assertIn("provider: openai-codex", profile_config)


class SecurityTests(unittest.TestCase):
    def test_startup_preserves_private_pre_upgrade_backup(self):
        start_script = (Path(__file__).resolve().parents[1] / "start.sh").read_text()
        self.assertIn("backups/pre-v2026.8.3", start_script)
        self.assertIn("chmod 700 /data/.hermes/backups", start_script)
        self.assertIn("backup_once /data/.hermes/config.yaml config.yaml", start_script)
        self.assertIn("backup_once /data/.hermes/auth.json auth.json", start_script)
        self.assertIn("backup_once /data/.hermes/platforms/pairing pairing", start_script)

    def test_redacts_query_json_and_bearer_secrets(self):
        raw = (
            "GET /api/pty?token=session-secret&channel=1 "
            "SOCIAL_MEDIA_API_KEY=publishing-secret "
            '"access_token":"oauth-secret" Authorization: Bearer bearer-secret'
        )
        clean = server.redact_sensitive_text(raw)
        for secret in ("session-secret", "publishing-secret", "oauth-secret", "bearer-secret"):
            self.assertNotIn(secret, clean)

    def test_uvicorn_access_log_is_disabled(self):
        config = server.build_uvicorn_config(8080)
        self.assertFalse(config.access_log)
        self.assertEqual(config.log_level, "warning")

    def test_cross_site_state_change_is_rejected(self):
        token = server._make_auth_token()
        req = request(
            method="POST",
            headers={
                "cookie": f"{server.COOKIE_NAME}={token}",
                "host": "hermes.example",
                "origin": "https://evil.example",
            },
        )
        self.assertEqual(server.guard(req).status_code, 403)

    def test_alpine_dependency_is_exact_and_integrity_pinned(self):
        html = (Path(__file__).resolve().parents[1] / "templates" / "index.html").read_text()
        self.assertIn("alpinejs@3.14.9", html)
        self.assertIn("integrity=\"sha384-", html)
        self.assertNotIn("alpinejs@3.x.x", html)


class ConfigApiTests(IsolatedHermesHome, unittest.IsolatedAsyncioTestCase):
    async def test_auxiliary_provider_save_cannot_replace_railway_codex_pin(self):
        server.write_env(server.ENV_FILE, {
            "LLM_MODEL": "gpt-5.6-terra",
            "HERMES_MODEL_PROVIDER": "openrouter",
        })
        token = server._make_auth_token()
        payload = {
            "vars": {
                "LLM_MODEL": "openrouter/other-model",
                "OPENROUTER_API_KEY": "openrouter-key",
                "_MODEL_OPENROUTER_API_KEY": "openrouter/other-model",
            },
            "_restart": False,
            "_active_provider_id": "openrouter",
        }
        req = request(
            method="PUT",
            headers={
                "cookie": f"{server.COOKIE_NAME}={token}",
                "host": "hermes.example",
                "origin": "https://hermes.example",
                "content-type": "application/json",
            },
            body=json.dumps(payload).encode(),
        )
        with patch.dict(os.environ, {
            "HERMES_MODEL_PROVIDER": "openai-codex",
            "LLM_MODEL": "gpt-5.6-terra",
        }):
            response = await server.api_config_put(req)
        self.assertEqual(response.status_code, 200)
        self.assertIn("main provider remains pinned", json.loads(response.body)["warning"])
        persisted = server.read_env(server.ENV_FILE)
        self.assertEqual(persisted["HERMES_MODEL_PROVIDER"], "openai-codex")
        self.assertEqual(persisted["LLM_MODEL"], "gpt-5.6-terra")
        self.assertEqual(persisted["OPENROUTER_API_KEY"], "openrouter-key")


class AppOpsTests(IsolatedHermesHome, unittest.IsolatedAsyncioTestCase):
    def test_founder_items_are_selected_only_for_explicit_hermes_filtering(self):
        founder_item = {"audience": "victor", "handled": False, "title": "Founder decision"}

        selected, skipped, _ = server._select_app_ops_items(
            [founder_item],
            include_founder_items=True,
        )
        self.assertEqual(selected, [founder_item])
        self.assertEqual(skipped, [])

        selected, skipped, _ = server._select_app_ops_items([founder_item])
        self.assertEqual(selected, [])
        self.assertEqual(skipped, [{"index": 0, "reason": "audience_not_hermes"}])

    async def test_explicit_hermes_founder_handoff_queues_agent_run(self):
        payload = {
            "source": "app-ops-action-inbox",
            "generated_at": "2026-07-11T17:47:45.222Z",
            "requires_victor": True,
            "requires_hermes": True,
            "text": "One founder decision",
            "items": [{"audience": "victor", "handled": False, "title": "Founder decision"}],
            "handled": [],
        }
        req = request(
            "/ingest/app-ops-action-inbox",
            method="POST",
            headers={"x-hermes-action-key": "test-action-key", "x-request-id": "delivery-founder"},
            body=json.dumps(payload).encode(),
        )
        with (
            patch.dict(os.environ, {"HERMES_ACTION_WEBHOOK_SECRET": "test-action-key"}),
            patch.object(server, "_schedule_app_ops_run", return_value=None) as schedule,
        ):
            response = await server.ingest_app_ops_action_inbox(req)

        self.assertEqual(response.status_code, 202)
        body = json.loads(response.body)
        self.assertEqual(body["status"], "accepted")
        self.assertEqual(body["hermes_item_count"], 1)
        schedule.assert_called_once()

    def test_requires_exact_delivery_matched_result_contract(self):
        valid = (
            'APP_OPS_RESULT {"delivery_id":"delivery-1","handled_count":1,'
            '"skipped_count":0,"actions":[{"summary":"Investigated",'
            '"status":"analyzed","evidence":["source checked"]}],"escalations":[]}'
        )
        self.assertIsNotNone(server._parse_app_ops_result(valid, "delivery-1"))
        multiline = valid.replace("APP_OPS_RESULT ", "APP_OPS_RESULT\n", 1)
        self.assertIsNotNone(server._parse_app_ops_result(multiline, "delivery-1", 1))
        quoted_marker = valid.replace("source checked", "source checked for APP_OPS_RESULT")
        self.assertIsNotNone(server._parse_app_ops_result(quoted_marker, "delivery-1", 1))
        fenced = valid.replace("APP_OPS_RESULT ", "APP_OPS_RESULT\n```json\n", 1) + "\n```"
        self.assertIsNotNone(server._parse_app_ops_result(fenced, "delivery-1", 1))
        self.assertIsNotNone(server._parse_app_ops_result(valid, "delivery-1", 1))
        self.assertIsNone(server._parse_app_ops_result(valid, "delivery-1", 2))
        self.assertIsNone(server._parse_app_ops_result(valid, "delivery-2"))
        self.assertIsNone(server._parse_app_ops_result(valid + "\ncontradictory trailing prose", "delivery-1"))
        self.assertIsNone(server._parse_app_ops_result("handled_count escalations", "delivery-1"))

    async def test_app_ops_agent_uses_raw_oneshot_output(self):
        payload_path = server._APP_OPS_RUNS_DIR / "oneshot.json"
        result = (
            'APP_OPS_RESULT {"delivery_id":"delivery-oneshot","handled_count":1,'
            '"skipped_count":0,"actions":[{"summary":"Investigated",'
            '"status":"analyzed","evidence":["source checked"]}],"escalations":[]}'
        )
        process = AsyncMock()
        process.communicate.return_value = (result.encode(), b"")
        process.returncode = 0
        runtime_env = {
            "PATH": os.environ.get("PATH", ""),
            "LLM_MODEL": "gpt-5.6-terra",
            "HERMES_MODEL_PROVIDER": "openai-codex",
        }
        with (
            patch.object(server, "_prepare_app_ops_runtime_env", return_value=runtime_env),
            patch.object(server.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)) as spawn,
        ):
            await server._run_app_ops_agent(
                payload_path,
                "delivery-oneshot",
                [{"title": "Investigate"}],
            )

        args = spawn.await_args.args
        self.assertEqual(args[0:2], ("hermes", "--oneshot"))
        self.assertNotIn("chat", args)
        self.assertNotIn("-q", args)
        self.assertEqual(args[-6:], ("--model", "gpt-5.6-terra", "--provider", "openai-codex", "--toolsets", "web,search"))
        records = [json.loads(line) for line in server._APP_OPS_LOG.read_text().splitlines()]
        self.assertEqual(records[-1]["status"], "agent_finished")
        self.assertTrue(records[-1]["semantic_success"])

    def test_public_status_omits_raw_output_and_paths(self):
        public = server._public_app_ops_record({
            "delivery_id": "delivery-1",
            "status": "agent_finished",
            "output_tail": "secret output",
            "payload_path": "/secret/path",
        })
        self.assertNotIn("output_tail", public)
        self.assertNotIn("payload_path", public)

    def test_delivery_filenames_include_original_id_digest(self):
        self.assertNotEqual(server._safe_delivery_stem("a/b"), server._safe_delivery_stem("a?b"))

    async def test_duplicate_delivery_is_not_enqueued_twice(self):
        payload = {
            "source": "test",
            "generated_at": "2026-07-11T00:00:00Z",
            "requires_victor": False,
            "text": "nothing to do",
            "items": [{"audience": "other", "handled": False}],
            "handled": False,
        }
        body = json.dumps(payload).encode()
        headers = {
            "x-hermes-action-key": "test-action-secret",
            "x-request-id": "delivery-duplicate",
            "content-type": "application/json",
        }
        with patch.dict(os.environ, {"HERMES_ACTION_WEBHOOK_SECRET": "test-action-secret"}):
            first = await server.ingest_app_ops_action_inbox(
                request("/ingest/app-ops-action-inbox", "POST", headers, body)
            )
            second = await server.ingest_app_ops_action_inbox(
                request("/ingest/app-ops-action-inbox", "POST", headers, body)
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(json.loads(second.body)["status"], "duplicate")

    async def test_accepted_delivery_is_recovered_after_restart(self):
        payload_path = server._APP_OPS_RUNS_DIR / "recover.json"
        payload_path.write_text(json.dumps({
            "items": [{"audience": "hermes", "handled": False, "title": "Investigate"}],
        }))
        server._append_app_ops_action({
            "delivery_id": "delivery-recover",
            "status": "accepted",
            "payload_path": str(payload_path),
        })
        with patch.object(server, "_schedule_app_ops_run", return_value=None) as schedule:
            await server._recover_app_ops_jobs()
        schedule.assert_called_once()

    async def test_full_queue_rejects_new_work_with_retry_signal(self):
        payload = {
            "source": "test",
            "generated_at": "2026-07-11T00:00:00Z",
            "requires_victor": False,
            "text": "investigate",
            "items": [{"audience": "hermes", "handled": False, "title": "Check issue"}],
            "handled": False,
        }
        req = request(
            "/ingest/app-ops-action-inbox",
            method="POST",
            headers={"x-hermes-action-key": "test-action-key"},
            body=json.dumps(payload).encode(),
        )
        current = asyncio.current_task()
        with (
            patch.dict(os.environ, {"HERMES_ACTION_WEBHOOK_SECRET": "test-action-key"}),
            patch.object(server, "_APP_OPS_MAX_PENDING_RUNS", 1),
            patch.object(server, "_APP_OPS_TASKS", {current}),
        ):
            response = await server.ingest_app_ops_action_inbox(req)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "60")
        self.assertEqual(json.loads(response.body)["action"], "retry_later")


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_runtime_state_is_authoritative_readiness(self):
        with tempfile.TemporaryDirectory() as home:
            Path(home, "gateway_state.json").write_text(json.dumps({
                "pid": 5150,
                "gateway_state": "running",
            }))

            class FakeProcess:
                pid = 5150
                returncode = None

            gateway = server.Gateway()
            with patch.object(server, "HERMES_HOME", home):
                self.assertTrue(await gateway._wait_until_ready(FakeProcess()))

    async def test_concurrent_gateway_starts_spawn_only_one_process(self):
        stopped = asyncio.Event()

        class FakeStdout:
            def __init__(self):
                self.sent_ready = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self.sent_ready:
                    self.sent_ready = True
                    return b"Gateway running with 1 platform(s)\n"
                await stopped.wait()
                raise StopAsyncIteration

        class FakeProcess:
            def __init__(self):
                self.pid = 4242
                self.returncode = None
                self.stdout = FakeStdout()

            async def wait(self):
                await stopped.wait()
                return self.returncode

            def terminate(self):
                self.returncode = 0
                stopped.set()

            def kill(self):
                self.terminate()

        spawn = AsyncMock(return_value=FakeProcess())
        gateway = server.Gateway()
        with (
            patch("server.asyncio.create_subprocess_exec", spawn),
            patch("server.write_config_yaml"),
            patch.dict(os.environ, {
                "LLM_MODEL": "gpt-5.6-terra",
                "HERMES_MODEL_PROVIDER": "openai-codex",
            }),
        ):
            await asyncio.gather(gateway.start(), gateway.start())
            self.assertEqual(gateway.state, "running")
            await gateway.stop()
        self.assertEqual(spawn.await_count, 1)

    async def test_starting_gateway_is_not_railway_ready(self):
        original_state = server.gw.state
        server.gw.state = "starting"
        try:
            with patch("server.is_config_complete", return_value=True):
                response = await server.route_health(request("/health"))
        finally:
            server.gw.state = original_state
        self.assertEqual(response.status_code, 503)

    async def test_new_scheduled_job_failure_is_not_railway_ready(self):
        original_state = server.gw.state
        server.gw.state = "running"
        try:
            with (
                patch("server.is_config_complete", return_value=True),
                patch("server.cron_health_status", return_value={
                    "available": True,
                    "healthy": False,
                    "new_failures": 1,
                }),
            ):
                response = await server.route_health(request("/health"))
        finally:
            server.gw.state = original_state
        self.assertEqual(response.status_code, 503)

    async def test_enabled_misconfigured_watchdog_is_not_railway_ready(self):
        class MisconfiguredWatchdog:
            @staticmethod
            def public_status():
                return {
                    "enabled": True,
                    "configured": False,
                    "last_outcome": "misconfigured",
                }

        original_state = server.gw.state
        original_watchdog = server._coo_watchdog
        server.gw.state = "running"
        server._coo_watchdog = MisconfiguredWatchdog()
        try:
            with patch("server.is_config_complete", return_value=True):
                response = await server.route_health(request("/health"))
        finally:
            server.gw.state = original_state
            server._coo_watchdog = original_watchdog
        self.assertEqual(response.status_code, 503)

    async def test_stopped_watchdog_task_is_not_railway_ready(self):
        class ConfiguredWatchdog:
            @staticmethod
            def public_status():
                return {
                    "enabled": True,
                    "configured": True,
                    "last_outcome": "healthy",
                }

        completed_task = asyncio.create_task(asyncio.sleep(0))
        await completed_task
        original_state = server.gw.state
        original_watchdog = server._coo_watchdog
        original_task = server._coo_watchdog_task
        server.gw.state = "running"
        server._coo_watchdog = ConfiguredWatchdog()
        server._coo_watchdog_task = completed_task
        try:
            with patch("server.is_config_complete", return_value=True):
                response = await server.route_health(request("/health"))
        finally:
            server.gw.state = original_state
            server._coo_watchdog = original_watchdog
            server._coo_watchdog_task = original_task
        self.assertEqual(response.status_code, 503)
        payload = json.loads(response.body)
        self.assertFalse(payload["coo_watchdog"]["task_running"])


if __name__ == "__main__":
    unittest.main()
