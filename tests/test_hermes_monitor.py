import os
import unittest
from unittest.mock import patch

import hermes_monitor as monitor


def result(status, payload=None):
    return monitor.ProbeResult(status, payload or {})


class AssessmentTests(unittest.TestCase):
    def test_gateway_down_gets_one_redeploy_path(self):
        assessment = monitor.assess_health(result(503, {
            "status": "degraded",
            "configured": True,
            "gateway": "stopped",
        }))
        self.assertEqual(assessment.action, "redeploy")

    def test_provider_auth_uses_only_ready_capped_fallback(self):
        payload = {
            "status": "degraded",
            "configured": True,
            "gateway": "running",
            "scheduled_jobs": {
                "healthy": False,
                "failure_kinds": {"provider_auth": 1, "other": 0},
            },
            "fallback": {"ready": True},
        }
        self.assertEqual(monitor.assess_health(result(503, payload)).action, "fallback")
        payload["fallback"] = {"ready": False}
        self.assertEqual(monitor.assess_health(result(503, payload)).action, "none")

    def test_arbitrary_cron_failure_is_not_hidden_by_restart(self):
        assessment = monitor.assess_health(result(503, {
            "status": "degraded",
            "configured": True,
            "gateway": "running",
            "scheduled_jobs": {
                "healthy": False,
                "failure_kinds": {"provider_auth": 0, "other": 1},
            },
        }))
        self.assertEqual(assessment.action, "none")

    def test_invalid_success_response_is_not_automatically_redeployed(self):
        assessment = monitor.assess_health(
            monitor.ProbeResult(200, {}, "invalid_json")
        )
        self.assertEqual(assessment.action, "none")


class OrchestrationTests(unittest.TestCase):
    def test_new_gateway_incident_redeploys_exactly_once(self):
        notices = []
        redeploys = []
        code = monitor.run_check(
            "https://example.test/health",
            probe=lambda *_: result(503, {
                "status": "degraded", "configured": True, "gateway": "stopped",
            }),
            prior_failures=0,
            notify=lambda message: notices.append(message) is None,
            redeploy=lambda: redeploys.append(True) is None,
            wait=lambda _url: True,
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(redeploys), 1)
        self.assertEqual(len(notices), 2)

    def test_existing_incident_never_redeploys_again(self):
        redeploys = []
        code = monitor.run_check(
            "https://example.test/health",
            probe=lambda *_: result(None),
            prior_failures=2,
            notify=lambda _message: True,
            redeploy=lambda: redeploys.append(True) is None,
        )
        self.assertEqual(code, 1)
        self.assertEqual(redeploys, [])

    def test_provider_auth_does_not_restart_service(self):
        redeploys = []
        payload = {
            "status": "degraded",
            "configured": True,
            "gateway": "running",
            "scheduled_jobs": {
                "healthy": False,
                "failure_kinds": {"provider_auth": 1},
            },
            "fallback": {"ready": True},
        }
        monitor.run_check(
            "https://example.test/health",
            probe=lambda *_: result(503, payload),
            prior_failures=0,
            notify=lambda _message: True,
            redeploy=lambda: redeploys.append(True) is None,
        )
        self.assertEqual(redeploys, [])

    def test_recovery_notice_after_failed_scheduled_run(self):
        notices = []
        code = monitor.run_check(
            "https://example.test/health",
            probe=lambda *_: result(200, {"status": "ok"}),
            prior_failures=2,
            notify=lambda message: notices.append(message) is None,
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(notices), 1)


class HistoryTests(unittest.TestCase):
    def test_history_counts_only_consecutive_failures(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return (
                    b'{"workflow_runs": ['
                    b'{"id": 10, "conclusion": "failure"},'
                    b'{"id": 9, "conclusion": "failure"},'
                    b'{"id": 8, "conclusion": "success"},'
                    b'{"id": 7, "conclusion": "failure"}]}'
                )

        with patch("hermes_monitor.urlopen", return_value=Response()):
            count = monitor.consecutive_failed_scheduled_runs("owner/repo", "token", "11")
        self.assertEqual(count, 2)

    def test_monitor_messages_never_include_credentials(self):
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "bot-secret",
            "TELEGRAM_CHAT_ID": "chat-secret",
        }):
            message = monitor.initial_message(monitor.Assessment(False, "gateway stopped", "redeploy"))
        self.assertNotIn("bot-secret", message)
        self.assertNotIn("chat-secret", message)


if __name__ == "__main__":
    unittest.main()
