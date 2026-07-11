import asyncio
import json
import os
import sys
import tempfile
import unittest
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
                "LLM_MODEL": "gpt-5.4",
                "OPENROUTER_API_KEY": "openrouter-key",
            })

        import yaml
        model = yaml.safe_load((self.home / "config.yaml").read_text())["model"]
        self.assertEqual(model["provider"], "openai-codex")
        self.assertEqual(model["default"], "gpt-5.4")
        self.assertNotIn("base_url", model)
        self.assertNotIn("api_key", model)

    def test_explicit_provider_override_wins(self):
        server.write_config_yaml({
            "LLM_MODEL": "gpt-5.4",
            "HERMES_MODEL_PROVIDER": "openai-codex",
            "OPENROUTER_API_KEY": "openrouter-key",
        })
        import yaml
        model = yaml.safe_load((self.home / "config.yaml").read_text())["model"]
        self.assertEqual(model["provider"], "openai-codex")

    def test_railway_provider_and_model_override_persisted_values(self):
        with patch.dict(os.environ, {
            "HERMES_MODEL_PROVIDER": "openai-codex",
            "LLM_MODEL": "gpt-5.4",
        }):
            server.write_config_yaml({
                "LLM_MODEL": "openrouter/other-model",
                "HERMES_MODEL_PROVIDER": "openrouter",
                "OPENROUTER_API_KEY": "openrouter-key",
            })
        import yaml
        model = yaml.safe_load((self.home / "config.yaml").read_text())["model"]
        self.assertEqual(model, {"default": "gpt-5.4", "provider": "openai-codex"})

    def test_new_config_defaults_to_auto_without_explicit_provider(self):
        with patch.dict(os.environ, {"HERMES_MODEL_PROVIDER": ""}):
            server.write_config_yaml({
                "LLM_MODEL": "openrouter/model",
                "OPENROUTER_API_KEY": "openrouter-key",
            })
        import yaml
        model = yaml.safe_load((self.home / "config.yaml").read_text())["model"]
        self.assertEqual(model["provider"], "auto")

    def test_mcp_placeholder_survives_config_merge(self):
        (self.home / "config.yaml").write_text(
            "mcp_servers:\n"
            "  typefully:\n"
            "    url: https://mcp.typefully.com/mcp?TYPEFULLY_API_KEY=${TYPEFULLY_API_KEY}\n"
        )
        server.write_config_yaml({
            "LLM_MODEL": "gpt-5.4",
            "HERMES_MODEL_PROVIDER": "openai-codex",
        })
        import yaml
        url = yaml.safe_load((self.home / "config.yaml").read_text())["mcp_servers"]["typefully"]["url"]
        self.assertEqual(url, "https://mcp.typefully.com/mcp?TYPEFULLY_API_KEY=${TYPEFULLY_API_KEY}")

    def test_pinned_oauth_provider_is_complete_without_api_key(self):
        (self.home / "config.yaml").write_text(
            "model:\n  default: gpt-5.4\n  provider: openai-codex\n"
        )
        self.assertTrue(server.is_config_complete({"LLM_MODEL": "gpt-5.4"}))

    def test_unknown_secret_names_are_masked_and_hidden(self):
        raw = {
            "LLM_MODEL": "gpt-5.4",
            "CAREER_OPS_WEBHOOK_SECRET": "career-secret-value",
            "HERMES_ACTION_KEY": "action-secret-value",
        }
        masked = server.mask(raw)
        self.assertEqual(masked["CAREER_OPS_WEBHOOK_SECRET"], "***")
        self.assertEqual(masked["HERMES_ACTION_KEY"], "***")
        self.assertEqual(server.visible_config(raw), {"LLM_MODEL": "gpt-5.4"})

    def test_env_writer_rejects_newlines_and_sets_private_mode(self):
        with self.assertRaises(ValueError):
            server.write_env(self.home / ".env", {"LLM_MODEL": "gpt-5.4\nINJECTED=true"})
        server.write_env(self.home / ".env", {"LLM_MODEL": "gpt-5.4"})
        self.assertEqual((self.home / ".env").stat().st_mode & 0o777, 0o600)

    def test_invalid_yaml_is_never_overwritten(self):
        config_path = self.home / "config.yaml"
        invalid = "model: [unterminated\n"
        config_path.write_text(invalid)
        with self.assertRaises(RuntimeError):
            server.write_config_yaml({"LLM_MODEL": "gpt-5.4"})
        self.assertEqual(config_path.read_text(), invalid)

    def test_hermes_environment_excludes_wrapper_and_railway_secrets(self):
        server.write_env(self.home / ".env", {
            "LLM_MODEL": "gpt-5.4",
            "HERMES_ACTION_WEBHOOK_SECRET": "persisted-webhook-secret",
            "TAVILY_API_KEY": "search-key",
        })
        with patch.dict(os.environ, {
            "ADMIN_PASSWORD": "admin-secret",
            "RAILWAY_TOKEN": "railway-secret",
            "UNRELATED_SECRET": "unrelated-secret",
            "HERMES_MODEL_PROVIDER": "openai-codex",
        }):
            env = server.build_hermes_env()
        self.assertEqual(env["HERMES_MODEL_PROVIDER"], "openai-codex")
        self.assertEqual(env["TAVILY_API_KEY"], "search-key")
        for key in ("ADMIN_PASSWORD", "RAILWAY_TOKEN", "UNRELATED_SECRET", "HERMES_ACTION_WEBHOOK_SECRET"):
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

    def test_app_ops_environment_excludes_channel_and_publishing_credentials(self):
        server.write_env(self.home / ".env", {
            "LLM_MODEL": "gpt-5.4",
            "HERMES_MODEL_PROVIDER": "openai-codex",
            "TELEGRAM_BOT_TOKEN": "telegram-secret",
            "TYPEFULLY_API_KEY": "publishing-secret",
            "TAVILY_API_KEY": "search-key",
        })
        env = server.build_app_ops_env()
        self.assertEqual(env["TAVILY_API_KEY"], "search-key")
        self.assertNotIn("TELEGRAM_BOT_TOKEN", env)
        self.assertNotIn("TYPEFULLY_API_KEY", env)

    def test_app_ops_profile_persists_only_read_only_worker_settings(self):
        server.write_env(self.home / ".env", {
            "LLM_MODEL": "gpt-5.4",
            "HERMES_MODEL_PROVIDER": "openai-codex",
            "TELEGRAM_BOT_TOKEN": "telegram-secret",
            "TYPEFULLY_API_KEY": "publishing-secret",
            "TAVILY_API_KEY": "search-key",
        })
        env = server._prepare_app_ops_runtime_env()
        profile_home = Path(env["HERMES_HOME"])
        profile_env = server.read_env(profile_home / ".env")
        self.assertEqual(profile_env["TAVILY_API_KEY"], "search-key")
        self.assertNotIn("TELEGRAM_BOT_TOKEN", profile_env)
        self.assertNotIn("TYPEFULLY_API_KEY", profile_env)


class SecurityTests(unittest.TestCase):
    def test_startup_preserves_private_pre_upgrade_backup(self):
        start_script = (Path(__file__).resolve().parents[1] / "start.sh").read_text()
        self.assertIn("backups/pre-v2026.7.7.2", start_script)
        self.assertIn("chmod 700 /data/.hermes/backups", start_script)
        self.assertIn("backup_once /data/.hermes/config.yaml config.yaml", start_script)
        self.assertIn("backup_once /data/.hermes/auth.json auth.json", start_script)
        self.assertIn("backup_once /data/.hermes/platforms/pairing pairing", start_script)

    def test_redacts_query_json_and_bearer_secrets(self):
        raw = (
            "GET /api/pty?token=session-secret&channel=1 "
            "TYPEFULLY_API_KEY=typefully-secret "
            '"access_token":"oauth-secret" Authorization: Bearer bearer-secret'
        )
        clean = server.redact_sensitive_text(raw)
        for secret in ("session-secret", "typefully-secret", "oauth-secret", "bearer-secret"):
            self.assertNotIn(secret, clean)

    def test_uvicorn_access_log_is_disabled(self):
        self.assertFalse(server.build_uvicorn_config(8080).access_log)

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
            "LLM_MODEL": "gpt-5.4",
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
            "LLM_MODEL": "gpt-5.4",
        }):
            response = await server.api_config_put(req)
        self.assertEqual(response.status_code, 200)
        self.assertIn("main provider remains pinned", json.loads(response.body)["warning"])
        persisted = server.read_env(server.ENV_FILE)
        self.assertEqual(persisted["HERMES_MODEL_PROVIDER"], "openai-codex")
        self.assertEqual(persisted["LLM_MODEL"], "gpt-5.4")
        self.assertEqual(persisted["OPENROUTER_API_KEY"], "openrouter-key")


class AppOpsTests(IsolatedHermesHome, unittest.IsolatedAsyncioTestCase):
    def test_requires_exact_delivery_matched_result_contract(self):
        valid = (
            'APP_OPS_RESULT {"delivery_id":"delivery-1","handled_count":1,'
            '"skipped_count":0,"actions":[{"summary":"Investigated",'
            '"status":"analyzed","evidence":["source checked"]}],"escalations":[]}'
        )
        self.assertIsNotNone(server._parse_app_ops_result(valid, "delivery-1"))
        self.assertIsNotNone(server._parse_app_ops_result(valid, "delivery-1", 1))
        self.assertIsNone(server._parse_app_ops_result(valid, "delivery-1", 2))
        self.assertIsNone(server._parse_app_ops_result(valid, "delivery-2"))
        self.assertIsNone(server._parse_app_ops_result("handled_count escalations", "delivery-1"))

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
                "LLM_MODEL": "gpt-5.4",
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


if __name__ == "__main__":
    unittest.main()
