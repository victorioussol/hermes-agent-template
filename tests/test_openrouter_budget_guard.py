import unittest

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


if __name__ == "__main__":
    unittest.main()
