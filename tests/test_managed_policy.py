import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


class ManagedPolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = yaml.safe_load((ROOT / "managed-config.yaml").read_text())

    def test_main_and_routine_work_stay_on_codex_terra(self):
        self.assertEqual(self.config["model"], {
            "provider": "openai-codex",
            "default": "gpt-5.6-terra",
        })
        self.assertEqual(self.config["agent"]["reasoning_effort"], "medium")
        self.assertEqual(self.config["delegation"]["provider"], "openai-codex")
        self.assertEqual(self.config["delegation"]["model"], "gpt-5.6-terra")
        for task, route in self.config["auxiliary"].items():
            if task == "free_only":
                continue
            self.assertEqual(route["provider"], "openai-codex", task)
            self.assertEqual(route["model"], "gpt-5.6-terra", task)

    def test_only_approved_deepseek_flash_is_automatic_fallback(self):
        self.assertEqual(self.config["fallback_providers"], [{
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-flash-0731",
        }])

    def test_policy_contains_no_legacy_models_or_retired_cli(self):
        policy = (ROOT / "managed-config.yaml").read_text()
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertNotIn("gpt-5.4", policy)
        self.assertNotIn("claude-code", dockerfile)

    def test_curator_is_reversible_and_does_not_consolidate(self):
        curator = self.config["curator"]
        self.assertTrue(curator["enabled"])
        self.assertFalse(curator["consolidate"])
        self.assertFalse(curator["prune_builtins"])
        self.assertTrue(curator["backup"]["enabled"])


if __name__ == "__main__":
    unittest.main()
