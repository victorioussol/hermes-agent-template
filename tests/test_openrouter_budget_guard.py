import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import openrouter_budget_guard as guard


class OpenRouterBudgetGuardTests(unittest.TestCase):
    def test_accepts_exact_monthly_five_dollar_limit(self):
        status, exit_code = guard.evaluate_metadata({
            "limit": 5,
            "limit_reset": "monthly",
            "usage_monthly": 0.18,
            "limit_remaining": 4.82,
        })
        self.assertEqual(exit_code, 0)
        self.assertEqual(status["status"], "ok")
        self.assertEqual(status["limit_usd"], 5.0)

    def test_rejects_a_larger_or_non_resetting_limit(self):
        status, exit_code = guard.evaluate_metadata({
            "limit": 70,
            "limit_reset": None,
            "usage": 0,
        })
        self.assertEqual(exit_code, 2)
        self.assertEqual(status["status"], "unsafe_limit")

    def test_raises_alert_levels_before_the_ceiling(self):
        cases = ((2.5, "notice"), (4.0, "warning"), (4.75, "critical"))
        for usage, expected in cases:
            with self.subTest(usage=usage):
                status, exit_code = guard.evaluate_metadata({
                    "limit": 5,
                    "limit_reset": "monthly",
                    "usage_monthly": usage,
                })
                self.assertEqual(exit_code, 0)
                self.assertEqual(status["status"], expected)

    def test_main_is_silent_when_key_is_safely_below_notice_threshold(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = io.StringIO()
            with (
                patch.dict(
                    os.environ,
                    {
                        "OPENROUTER_API_KEY": "test-key",
                        "HERMES_OPENROUTER_BUDGET_STATUS_FILE": str(
                            Path(tmpdir) / "status.json"
                        ),
                    },
                    clear=False,
                ),
                patch.object(
                    guard,
                    "fetch_metadata",
                    return_value={
                        "limit": 5,
                        "limit_reset": "monthly",
                        "usage_monthly": 0.18,
                        "limit_remaining": 4.82,
                    },
                ),
                redirect_stdout(output),
            ):
                exit_code = guard.main()

            self.assertEqual(exit_code, 0)
            self.assertEqual(output.getvalue(), "")
            saved = json.loads((Path(tmpdir) / "status.json").read_text())
            self.assertEqual(saved["status"], "ok")

    def test_main_prints_a_budget_notice(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = io.StringIO()
            with (
                patch.dict(
                    os.environ,
                    {
                        "OPENROUTER_API_KEY": "test-key",
                        "HERMES_OPENROUTER_BUDGET_STATUS_FILE": str(
                            Path(tmpdir) / "status.json"
                        ),
                    },
                    clear=False,
                ),
                patch.object(
                    guard,
                    "fetch_metadata",
                    return_value={
                        "limit": 5,
                        "limit_reset": "monthly",
                        "usage_monthly": 2.5,
                        "limit_remaining": 2.5,
                    },
                ),
                redirect_stdout(output),
            ):
                exit_code = guard.main()

            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "notice")

    def test_main_reads_the_private_hermes_env_when_cron_filters_secrets(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            (home / ".env").write_text(
                "IGNORED=value\nOPENROUTER_API_KEY='stored-key'\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            with (
                patch.dict(
                    os.environ,
                    {
                        "OPENROUTER_API_KEY": "",
                        "HERMES_HOME": str(home),
                        "HERMES_OPENROUTER_BUDGET_STATUS_FILE": str(
                            home / "status.json"
                        ),
                    },
                    clear=False,
                ),
                patch.object(
                    guard,
                    "fetch_metadata",
                    return_value={
                        "limit": 5,
                        "limit_reset": "monthly",
                        "usage_monthly": 0,
                        "limit_remaining": 5,
                    },
                ) as fetch,
                redirect_stdout(output),
            ):
                exit_code = guard.main()

            self.assertEqual(exit_code, 0)
            self.assertEqual(output.getvalue(), "")
            fetch.assert_called_once_with("stored-key")


if __name__ == "__main__":
    unittest.main()
