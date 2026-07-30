import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from coo_watchdog import (
    CooWatchdog,
    OPS_HUB_ENVIRONMENT_ID,
    OPS_HUB_PROJECT_ID,
    OPS_HUB_SERVICE_ID,
    WatchdogConfig,
)


NOW = datetime(2026, 7, 30, 21, 0, tzinfo=timezone.utc)


def timestamp(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


class WatchdogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.requests: list[tuple[str, str, dict]] = []

    def tearDown(self):
        self.tempdir.cleanup()

    def config(self, **overrides) -> WatchdogConfig:
        values = {
            "enabled": True,
            "supabase_url": "https://supabase.example",
            "supabase_service_role_key": "service-role",
            "railway_api_token": "railway-token",
            "hermes_home": Path(self.tempdir.name),
            "interval_seconds": 900,
            "stale_minutes": 45,
            "no_contact_minutes": 1440,
            "recovery_cooldown_minutes": 60,
            "max_recovery_attempts": 2,
            "verify_timeout_seconds": 60,
            "verify_poll_seconds": 5,
        }
        values.update(overrides)
        return WatchdogConfig(**values)

    def client(self, handler):
        async def capture(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else {}
            self.requests.append((request.method, str(request.url), body))
            return await handler(request, body)

        return httpx.AsyncClient(transport=httpx.MockTransport(capture))

    @staticmethod
    def heartbeat(minutes_ago: int) -> list[dict]:
        return [{
            "key": "railway_guiri_ops_hub",
            "updated_at": timestamp(minutes_ago),
            "value": json.dumps({
                "completed_at": timestamp(minutes_ago),
                "railway_deployment_id": "deployment-heartbeat",
            }),
        }]

    @staticmethod
    def deployment(minutes_ago: int, *, can_redeploy: bool = True) -> dict:
        return {
            "data": {
                "deployments": {
                    "edges": [{
                        "node": {
                            "id": "deployment-old",
                            "status": "CRASHED",
                            "createdAt": timestamp(minutes_ago),
                            "canRedeploy": can_redeploy,
                        }
                    }]
                }
            }
        }

    async def test_fresh_heartbeat_does_nothing(self):
        async def handler(request, body):
            self.assertEqual(request.method, "GET")
            return httpx.Response(200, json=self.heartbeat(5))

        client = self.client(handler)
        watchdog = CooWatchdog(self.config(), client=client, now=lambda: NOW)
        self.assertEqual(await watchdog.check_once(), "healthy")
        self.assertEqual(len(self.requests), 1)
        self.assertFalse((Path(self.tempdir.name) / "app-ops-action-inbox" / "coo-watchdog.jsonl").exists())
        await client.aclose()

    async def test_recent_railway_run_prevents_false_restart(self):
        async def handler(request, body):
            if request.method == "GET":
                return httpx.Response(200, json=self.heartbeat(90))
            if "deploymentLogs" in body.get("query", ""):
                return httpx.Response(200, json={
                    "data": {"deploymentLogs": [{"timestamp": timestamp(10)}]}
                })
            return httpx.Response(200, json=self.deployment(10))

        client = self.client(handler)
        watchdog = CooWatchdog(self.config(), client=client, now=lambda: NOW)
        self.assertEqual(await watchdog.check_once(), "scheduler_recent_no_recovery")
        graphql = [body for method, url, body in self.requests if "graphql" in url]
        self.assertEqual(len(graphql), 2)
        self.assertIn("deployments(input:", graphql[0]["query"])
        self.assertIn("deploymentLogs", graphql[1]["query"])
        self.assertFalse(any("deploymentRedeploy" in body["query"] for body in graphql))
        await client.aclose()

    async def test_recent_build_without_logs_prevents_duplicate_redeploy(self):
        async def handler(request, body):
            if request.method == "GET":
                return httpx.Response(200, json=self.heartbeat(90))
            if "deploymentLogs" in body.get("query", ""):
                return httpx.Response(200, json={"data": {"deploymentLogs": []}})
            deployment = self.deployment(10)
            deployment["data"]["deployments"]["edges"][0]["node"]["status"] = "BUILDING"
            return httpx.Response(200, json=deployment)

        client = self.client(handler)
        watchdog = CooWatchdog(self.config(), client=client, now=lambda: NOW)
        self.assertEqual(await watchdog.check_once(), "scheduler_recent_no_recovery")
        graphql = [body for _, url, body in self.requests if "graphql" in url]
        self.assertFalse(any("deploymentRedeploy" in body["query"] for body in graphql))
        await client.aclose()

    async def test_stale_scheduler_redeploys_exact_target_and_recovers_silently(self):
        heartbeat_reads = 0

        async def handler(request, body):
            nonlocal heartbeat_reads
            if request.method == "GET":
                heartbeat_reads += 1
                return httpx.Response(
                    200,
                    json=self.heartbeat(90 if heartbeat_reads == 1 else 1),
                )
            if "ops_learning_events" in str(request.url):
                return httpx.Response(201)
            if "deploymentRedeploy" in body.get("query", ""):
                return httpx.Response(200, json={
                    "data": {"deploymentRedeploy": {"id": "deployment-new", "status": "BUILDING"}}
                })
            if "deploymentLogs" in body.get("query", ""):
                return httpx.Response(200, json={
                    "data": {"deploymentLogs": [{"timestamp": timestamp(90)}]}
                })
            return httpx.Response(200, json=self.deployment(90))

        async def no_wait(_seconds):
            return None

        client = self.client(handler)
        watchdog = CooWatchdog(
            self.config(),
            client=client,
            now=lambda: NOW,
            sleep=no_wait,
        )
        self.assertEqual(await watchdog.check_once(), "recovered")
        graphql = [body for method, url, body in self.requests if "graphql" in url]
        list_input = graphql[0]["variables"]["input"]
        self.assertEqual(list_input, {
            "projectId": OPS_HUB_PROJECT_ID,
            "environmentId": OPS_HUB_ENVIRONMENT_ID,
            "serviceId": OPS_HUB_SERVICE_ID,
        })
        redeploy = next(body for body in graphql if "deploymentRedeploy" in body["query"])
        self.assertEqual(redeploy["variables"], {"id": "deployment-old"})
        self.assertFalse(any("/functions/v1/app-ops-dispatcher" in url for _, url, _ in self.requests))
        await client.aclose()

    async def test_no_contact_for_24_hours_pages_founder_with_trusted_marker(self):
        async def handler(request, body):
            if request.method == "GET":
                return httpx.Response(200, json=self.heartbeat(1500))
            if "ops_learning_events" in str(request.url):
                return httpx.Response(201)
            if "/functions/v1/app-ops-dispatcher" in str(request.url):
                return httpx.Response(200, json={
                    "ok": True,
                    "event_id": "event-1",
                    "telegram_delivery_confirmed": True,
                })
            return httpx.Response(200, json=self.deployment(1500))

        client = self.client(handler)
        watchdog = CooWatchdog(self.config(), client=client, now=lambda: NOW)
        self.assertEqual(await watchdog.check_once(), "founder_attention_no_contact")
        alert = next(
            body
            for _, url, body in self.requests
            if "/functions/v1/app-ops-dispatcher" in url
        )
        self.assertEqual(alert["source"], "hermes-coo-watchdog")
        self.assertTrue(alert["communication"]["victor_action"])
        marker = alert["payload"]["hermes_coo_recovery"]
        self.assertEqual(marker["triaged_by"], "hermes")
        self.assertEqual(marker["outcome"], "no_contact_24h")
        self.assertEqual(marker["target_service_id"], OPS_HUB_SERVICE_ID)
        self.assertNotIn("attempted", alert["body"].lower())
        self.assertNotIn("attempt limit", alert["communication"]["action_underway"].lower())
        self.assertFalse(any("graphql" in url for _, url, _ in self.requests))
        await client.aclose()

    async def test_legacy_dispatcher_telegram_receipt_is_accepted_during_rollout(self):
        async def handler(request, body):
            if request.method == "GET":
                return httpx.Response(200, json=self.heartbeat(1500))
            if "ops_learning_events" in str(request.url):
                return httpx.Response(201)
            if "/functions/v1/app-ops-dispatcher" in str(request.url):
                return httpx.Response(200, json={
                    "ok": True,
                    "event_id": "event-legacy",
                    "dispatched": {"telegram": True, "discord": True},
                })
            raise AssertionError(f"unexpected request {request.url}")

        client = self.client(handler)
        watchdog = CooWatchdog(self.config(), client=client, now=lambda: NOW)
        self.assertEqual(await watchdog.check_once(), "founder_attention_no_contact")
        actions = [
            json.loads(line)["action"]
            for line in watchdog._log_path.read_text().splitlines()
        ]
        self.assertIn("founder_alert_sent", actions)
        await client.aclose()

    async def test_two_failed_attempts_stop_recovery_and_page_founder(self):
        async def handler(request, body):
            if request.method == "GET":
                return httpx.Response(200, json=self.heartbeat(90))
            if "ops_learning_events" in str(request.url):
                return httpx.Response(201)
            if "/functions/v1/app-ops-dispatcher" in str(request.url):
                return httpx.Response(200, json={
                    "ok": True,
                    "event_id": "event-2",
                    "telegram_delivery_confirmed": True,
                })
            if "deploymentLogs" in body.get("query", ""):
                return httpx.Response(200, json={
                    "data": {"deploymentLogs": [{"timestamp": timestamp(90)}]}
                })
            return httpx.Response(200, json=self.deployment(90))

        client = self.client(handler)
        watchdog = CooWatchdog(self.config(), client=client, now=lambda: NOW)
        for index in range(2):
            watchdog._append_local({
                "action": "recovery_attempt_started",
                "recorded_at": timestamp(120 - index),
                "payload": {},
            })
        self.assertEqual(
            await watchdog.check_once(),
            "founder_attention_recovery_exhausted",
        )
        self.assertFalse(any(
            "deploymentRedeploy" in body.get("query", "")
            for _, url, body in self.requests
            if "graphql" in url
        ))
        await client.aclose()

    async def test_missing_safe_redeploy_target_pages_founder_without_mutation(self):
        async def handler(request, body):
            if request.method == "GET":
                return httpx.Response(200, json=self.heartbeat(90))
            if "ops_learning_events" in str(request.url):
                return httpx.Response(201)
            if "/functions/v1/app-ops-dispatcher" in str(request.url):
                return httpx.Response(200, json={
                    "ok": True,
                    "event_id": "event-3",
                    "telegram_delivery_confirmed": True,
                })
            if "deploymentLogs" in body.get("query", ""):
                return httpx.Response(200, json={
                    "data": {"deploymentLogs": [{"timestamp": timestamp(90)}]}
                })
            return httpx.Response(200, json=self.deployment(90, can_redeploy=False))

        client = self.client(handler)
        watchdog = CooWatchdog(self.config(), client=client, now=lambda: NOW)
        self.assertEqual(
            await watchdog.check_once(),
            "founder_attention_recovery_exhausted",
        )
        self.assertFalse(any(
            "deploymentRedeploy" in body.get("query", "")
            for _, url, body in self.requests
            if "graphql" in url
        ))
        await client.aclose()

    async def test_unconfirmed_dispatcher_response_does_not_suppress_founder_retry(self):
        async def handler(request, body):
            if request.method == "GET":
                return httpx.Response(200, json=self.heartbeat(1500))
            if "ops_learning_events" in str(request.url):
                return httpx.Response(201)
            if "/functions/v1/app-ops-dispatcher" in str(request.url):
                return httpx.Response(200, json={
                    "ok": True,
                    "event_id": "event-undelivered",
                    "dispatched": {"telegram": False, "discord": False},
                    "telegram_delivery_confirmed": False,
                })
            raise AssertionError(f"unexpected request {request.url}")

        client = self.client(handler)
        watchdog = CooWatchdog(self.config(), client=client, now=lambda: NOW)
        self.assertEqual(
            await watchdog.check_once(),
            "founder_alert_pending_no_contact",
        )
        actions = [
            json.loads(line)["action"]
            for line in watchdog._log_path.read_text().splitlines()
        ]
        self.assertIn("founder_alert_failed", actions)
        self.assertNotIn("founder_alert_sent", actions)
        await client.aclose()

    async def test_persistent_railway_failures_exhaust_bounded_recovery(self):
        current = [NOW]

        async def handler(request, body):
            if request.method == "GET":
                return httpx.Response(200, json=self.heartbeat(90))
            if "ops_learning_events" in str(request.url):
                return httpx.Response(201)
            if "/functions/v1/app-ops-dispatcher" in str(request.url):
                return httpx.Response(200, json={
                    "ok": True,
                    "event_id": "event-railway-unavailable",
                    "telegram_delivery_confirmed": True,
                })
            if "graphql" in str(request.url):
                return httpx.Response(503, text="railway unavailable")
            raise AssertionError(f"unexpected request {request.url}")

        client = self.client(handler)
        watchdog = CooWatchdog(
            self.config(),
            client=client,
            now=lambda: current[0],
        )
        self.assertEqual(await watchdog.check_once(), "recovery_inspection_failed")
        current[0] = NOW + timedelta(minutes=61)
        self.assertEqual(
            await watchdog.check_once(),
            "founder_attention_recovery_exhausted",
        )
        actions = [
            json.loads(line)["action"]
            for line in watchdog._log_path.read_text().splitlines()
        ]
        self.assertEqual(actions.count("recovery_inspection_failed"), 2)
        self.assertIn("founder_alert_sent", actions)
        await client.aclose()

    async def test_missing_heartbeat_starts_observation_instead_of_immediate_page(self):
        async def handler(request, body):
            self.assertEqual(request.method, "GET")
            return httpx.Response(200, json=[])

        client = self.client(handler)
        watchdog = CooWatchdog(self.config(), client=client, now=lambda: NOW)
        self.assertEqual(
            await watchdog.check_once(),
            "heartbeat_missing_observation",
        )
        self.assertFalse(any(
            "/functions/v1/app-ops-dispatcher" in url
            for _, url, _ in self.requests
        ))
        records = [
            json.loads(line)
            for line in watchdog._log_path.read_text().splitlines()
        ]
        self.assertEqual(records[-1]["action"], "heartbeat_missing_observed")
        await client.aclose()

    async def test_failed_remote_receipt_is_synced_on_next_check(self):
        receipt_writes = 0

        async def handler(request, body):
            nonlocal receipt_writes
            if request.method == "GET":
                return httpx.Response(200, json=self.heartbeat(5))
            if "ops_learning_events" in str(request.url):
                receipt_writes += 1
                return httpx.Response(201)
            raise AssertionError(f"unexpected request {request.url}")

        client = self.client(handler)
        watchdog = CooWatchdog(self.config(), client=client, now=lambda: NOW)
        watchdog._append_local({
            "action": "recovery_verified",
            "stage": "verification_recorded",
            "status": "heartbeat_restored",
            "source_record_id": "deployment-old",
            "idempotency_key": "receipt-pending",
            "payload": {"silent_recovery": True},
            "evidence_links": [],
            "recorded_at": timestamp(10),
        })
        watchdog._append_local({
            "action": "remote_receipt_failed",
            "status": "deferred",
            "receipt_idempotency_key": "receipt-pending",
            "recorded_at": timestamp(9),
        })
        self.assertEqual(await watchdog.check_once(), "healthy")
        self.assertEqual(receipt_writes, 1)
        actions = [
            json.loads(line)["action"]
            for line in watchdog._log_path.read_text().splitlines()
        ]
        self.assertIn("remote_receipt_synced", actions)
        await client.aclose()

    async def test_disabled_and_missing_configuration_never_mutate_production(self):
        with patch.dict("os.environ", {
            "HERMES_COO_WATCHDOG_ENABLED": "true",
            "SUPABASE_URL": "",
            "SUPABASE_SERVICE_ROLE_KEY": "",
            "RAILWAY_API_TOKEN": "",
            "HERMES_HOME": self.tempdir.name,
        }, clear=False):
            config = WatchdogConfig.from_env()
        watchdog = CooWatchdog(config, now=lambda: NOW)
        self.assertEqual(await watchdog.check_once(), "misconfigured")
        self.assertEqual(self.requests, [])

    def test_no_contact_threshold_always_exceeds_stale_threshold(self):
        with patch.dict("os.environ", {
            "HERMES_COO_STALE_MINUTES": "2000",
            "HERMES_COO_NO_CONTACT_MINUTES": "1440",
        }, clear=False):
            config = WatchdogConfig.from_env()
        self.assertEqual(config.stale_minutes, 1439)
        self.assertEqual(config.no_contact_minutes, 1440)


if __name__ == "__main__":
    unittest.main()
