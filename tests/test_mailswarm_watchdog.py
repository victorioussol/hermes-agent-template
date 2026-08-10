import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.error import URLError

import mailswarm_watchdog as watchdog


NOW = datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)


def iso(value=NOW):
    return value.isoformat().replace("+00:00", "Z")


class AssessmentTests(unittest.TestCase):
    def test_unconfigured_future_target_is_not_a_failure(self):
        target = watchdog.Target("Daily Operations", "", False, "heartbeat", 60)
        assessment = watchdog.assess_target(
            target,
            watchdog.ProbeResult(None, {}),
            now=NOW,
        )
        self.assertEqual(assessment.state, "not_expected")
        self.assertFalse(assessment.alert)

    def test_unreachable_target_is_unknown_not_inferred_failure(self):
        target = watchdog.Target("MailSwarm Observer", "https://example", True, "heartbeat", 45)
        assessment = watchdog.assess_target(
            target,
            watchdog.ProbeResult(None, {}, "unreachable"),
            now=NOW,
        )
        self.assertEqual(assessment.state, "unknown")
        self.assertNotIn("failed", assessment.reason)

    def test_fresh_observer_heartbeat_is_healthy(self):
        target = watchdog.Target("MailSwarm Observer", "https://example", True, "heartbeat", 45)
        assessment = watchdog.assess_target(
            target,
            watchdog.ProbeResult(200, {
                "status": "ok",
                "enabled": True,
                "heartbeat_at": iso(NOW - timedelta(minutes=5)),
            }),
            now=NOW,
        )
        self.assertEqual(assessment.state, "healthy")

    def test_stale_heartbeat_is_unknown_not_failed(self):
        target = watchdog.Target("Quality Controller", "https://example", True, "heartbeat", 45)
        assessment = watchdog.assess_target(
            target,
            watchdog.ProbeResult(200, {
                "status": "ok",
                "heartbeat_at": iso(NOW - timedelta(minutes=46)),
            }),
            now=NOW,
        )
        self.assertEqual(assessment.state, "unknown")

    def test_degraded_observer_with_stopped_task_is_unhealthy_even_with_fresh_heartbeat(self):
        target = watchdog.Target("MailSwarm Observer", "https://example", True, "heartbeat", 45)
        assessment = watchdog.assess_target(
            target,
            watchdog.ProbeResult(503, {
                "status": "degraded",
                "enabled": True,
                "configured": True,
                "task_running": False,
                "heartbeat_at": iso(),
            }),
            now=NOW,
        )
        self.assertEqual(assessment.state, "unhealthy")

    def test_explicit_integration_failure_is_unhealthy(self):
        target = watchdog.Target("Integration", "https://example", True, "integration", 60)
        assessment = watchdog.assess_target(
            target,
            watchdog.ProbeResult(200, {
                "state": "failed",
                "completed_at": iso(),
            }),
            now=NOW,
        )
        self.assertEqual(assessment.state, "unhealthy")


class ProbeTests(unittest.TestCase):
    def test_probe_is_bounded_to_two_attempts(self):
        pauses = []
        with patch("mailswarm_watchdog.urlopen", side_effect=URLError("offline")) as request:
            result = watchdog.probe_json(
                "https://example.test/health",
                attempts=9,
                timeout=5,
                pause=lambda seconds: pauses.append(seconds),
            )
        self.assertEqual(result.error_type, "unreachable")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(len(pauses), 1)


class RuntimeTests(unittest.TestCase):
    def config(self):
        return watchdog.WatchdogConfig(
            targets=(
                watchdog.Target("Hermes", "https://example/health", True, "hermes", 10),
                watchdog.Target("Future Loop", "", False, "heartbeat", 60),
            ),
            telegram_bot_token="bot-secret",
            telegram_chat_id="chat-secret",
            max_attempts=2,
            timeout_seconds=10,
        )

    def test_healthy_check_does_not_alert(self):
        notices = []

        def probe(_url, **_kwargs):
            return watchdog.ProbeResult(200, {"status": "ok"})

        code = watchdog.run_check(
            self.config(),
            probe=probe,
            notify=lambda _config, message: notices.append(message) is None,
            now=NOW,
        )
        self.assertEqual(code, 0)
        self.assertEqual(notices, [])

    def test_unhealthy_check_only_sends_an_alert(self):
        notices = []

        def probe(_url, **_kwargs):
            return watchdog.ProbeResult(503, {"status": "degraded"})

        code = watchdog.run_check(
            self.config(),
            probe=probe,
            notify=lambda _config, message: notices.append(message) is None,
            now=NOW,
        )
        self.assertEqual(code, 1)
        self.assertEqual(len(notices), 1)
        self.assertIn("No repair, dispatch, merge, or production action", notices[0])
        self.assertNotIn("bot-secret", notices[0])

    def test_expected_target_without_url_alerts_without_probing(self):
        probes = []
        notices = []
        config = watchdog.WatchdogConfig(
            targets=(watchdog.Target("Daily Operations", "", True, "heartbeat", 60),),
            telegram_bot_token="bot-secret",
            telegram_chat_id="chat-secret",
        )
        code = watchdog.run_check(
            config,
            probe=lambda *_args, **_kwargs: probes.append(True),
            notify=lambda _config, message: notices.append(message) is None,
            now=NOW,
        )
        self.assertEqual(code, 1)
        self.assertEqual(probes, [])
        self.assertEqual(len(notices), 1)
        self.assertIn("URL is not configured", notices[0])

    def test_missing_hermes_url_is_expected_unknown_and_alerted(self):
        probes = []
        notices = []
        with patch.dict(os.environ, {}, clear=True):
            config = watchdog.WatchdogConfig.from_env()
        hermes = config.targets[0]
        self.assertEqual(hermes.name, "Hermes")
        self.assertTrue(hermes.expected)
        self.assertEqual(hermes.url, "")
        self.assertFalse(config.targets[2].expected)

        code = watchdog.run_check(
            config,
            probe=lambda *_args, **_kwargs: probes.append(True),
            notify=lambda _config, message: notices.append(message) is None,
            now=NOW,
        )
        self.assertEqual(code, 1)
        self.assertEqual(probes, [])
        self.assertEqual(len(notices), 1)
        self.assertIn("Hermes: HERMES_HEALTH_URL is not configured", notices[0])


if __name__ == "__main__":
    unittest.main()
