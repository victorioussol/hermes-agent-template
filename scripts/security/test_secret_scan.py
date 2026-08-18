#!/usr/bin/env python3

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
SCANNER_PATH = next(
    (
        candidate
        for candidate in (
            SCRIPT_DIRECTORY / "secret-scan.py",
            SCRIPT_DIRECTORY / "secret_scan.py",
        )
        if candidate.is_file()
    ),
    None,
)
if SCANNER_PATH is None:
    raise RuntimeError("cannot find the secret scanner beside its tests")
MODULE_SPEC = importlib.util.spec_from_file_location("secret_scan", SCANNER_PATH)
if MODULE_SPEC is None or MODULE_SPEC.loader is None:
    raise RuntimeError("cannot load the secret scanner")
secret_scan = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = secret_scan
MODULE_SPEC.loader.exec_module(secret_scan)


def run(repo: Path, *args: str, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(
        args,
        cwd=repo,
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"command failed: {args[0]}")
    return result


class SecretScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        run(self.repo, "git", "init", "-q")
        run(self.repo, "git", "config", "user.name", "Synthetic Test")
        run(self.repo, "git", "config", "user.email", "synthetic@invalid.example")
        (self.repo / "README.md").write_text("baseline\n", encoding="utf-8")
        run(self.repo, "git", "add", "README.md")
        run(self.repo, "git", "commit", "-q", "-m", "baseline")
        self.base = run(self.repo, "git", "rev-parse", "HEAD").stdout.decode().strip()
        self.main_branch = (
            run(self.repo, "git", "branch", "--show-current").stdout.decode().strip()
        )

    @staticmethod
    def jwt() -> str:
        return ".".join(("eyJ" + "a" * 12, "b" * 16, "c" * 18))

    @staticmethod
    def password_key() -> str:
        return "pass" + "word"

    def stage(self, path: str, data: str | bytes) -> None:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            target.write_bytes(data)
        else:
            target.write_text(data, encoding="utf-8")
        run(self.repo, "git", "add", "--", path)

    def categories(self) -> set[str]:
        return {finding.category for finding in secret_scan.scan_staged(self.repo)}

    def test_known_token_and_bearer_are_detected_without_value_output(self) -> None:
        value = self.jwt()
        self.stage("config.txt", f"header=Bearer {value}\n")
        findings = secret_scan.scan_staged(self.repo)
        self.assertTrue(findings)
        output = io.StringIO()
        with redirect_stderr(output):
            secret_scan.report_findings(findings)
        self.assertNotIn(value, output.getvalue())

    def test_known_shapes_are_detected_and_never_reported(self) -> None:
        values = {
            "private key": "-----BEGIN " + "PRIVATE KEY-----",
            "JSON web token": self.jwt(),
            "Git hosting token": "gh" + "p_" + "a" * 36,
            "cloud access key": "AK" + "IA" + "A" * 16,
            "payment provider secret": "sk_" + "test_" + "a" * 20,
            "chat service token": "xo" + "xb-" + "a" * 16,
            "Google API key": "AI" + "za" + "a" * 32,
            "authorization header credential": "Basic " + "Y" * 16,
            "credential-bearing URL": "postgres://tester:" + "abcdefgh" + "@localhost/db",
        }
        self.stage("shapes.txt", "\n".join(values.values()) + "\n")
        findings = secret_scan.scan_staged(self.repo)
        categories = {finding.category for finding in findings}
        self.assertTrue(set(values).issubset(categories))
        output = io.StringIO()
        with redirect_stderr(output):
            secret_scan.report_findings(findings)
        for value in values.values():
            self.assertNotIn(value, output.getvalue())

    def test_password_threshold_and_runtime_reference(self) -> None:
        key = self.password_key()
        seven = "123" + "4567"
        eight = seven + "8"
        self.stage("short.json", json.dumps({key: seven}) + "\n")
        self.assertFalse(secret_scan.scan_staged(self.repo))
        run(self.repo, "git", "reset", "-q", "--", "short.json")
        (self.repo / "short.json").unlink()
        self.stage(
            "config.js",
            f'const good = {{"{key}": process.env.TEST_PASSWORD}};\n'
            f'const bad = {{"{key}": "{eight}", note: "example secret-scan: allow"}};\n',
        )
        findings = secret_scan.scan_staged(self.repo)
        self.assertEqual({finding.line for finding in findings}, {2})

    def test_runtime_authorization_and_url_references_are_not_credentials(self) -> None:
        self.stage(
            "runtime.txt",
            "Authorization: Bearer ${ACCESS_TOKEN}\n"
            "endpoint=postgres://tester:${QA_PASSWORD}@localhost/example\n",
        )
        self.assertFalse(secret_scan.scan_staged(self.repo))

    def test_multiline_quoted_assignment_is_detected(self) -> None:
        key = "client_" + "secret"
        value = "abcdefgh" + "ijklmnop"
        self.stage("config.json", f'{{\n  "{key}":\n  "{value}"\n}}\n')
        self.assertIn("credential-like assignment", self.categories())

    def test_escaped_quote_in_credential_literal_is_detected(self) -> None:
        key = self.password_key()
        value = "abcd" + r'\"' + "efgh"
        self.stage("config.json", f'{{"{key}": "{value}"}}\n')
        self.assertIn("credential-like assignment", self.categories())

    def test_provider_neutral_token_and_camel_case_keys_are_detected(self) -> None:
        value = "abcdefgh" + "ijklmnop"
        self.stage(
            "keys.txt",
            f'N8N_MCP_TOKEN="{value}"\nadminPassword="{value}"\n',
        )
        findings = secret_scan.scan_staged(self.repo)
        self.assertEqual(
            {finding.category for finding in findings},
            {"credential-like assignment"},
        )

    def test_complete_resulting_file_is_scanned(self) -> None:
        value = self.jwt()
        self.stage("legacy.txt", f"token={value}\nfirst\n")
        run(self.repo, "git", "commit", "-q", "-m", "legacy fixture")
        (self.repo / "legacy.txt").write_text(f"token={value}\nsecond\n", encoding="utf-8")
        run(self.repo, "git", "add", "legacy.txt")
        self.assertTrue(secret_scan.scan_staged(self.repo))

    def test_add_then_delete_commit_history_is_scanned(self) -> None:
        value = self.jwt()
        self.stage("temporary.txt", value + "\n")
        run(self.repo, "git", "commit", "-q", "-m", "temporary content")
        run(self.repo, "git", "rm", "-q", "temporary.txt")
        run(self.repo, "git", "commit", "-q", "-m", "remove temporary content")
        findings = secret_scan.scan_range(self.repo, f"{self.base}..HEAD")
        self.assertTrue(findings)

    def test_merge_does_not_rescan_blob_inherited_from_trusted_parent(self) -> None:
        value = self.jwt()
        run(self.repo, "git", "checkout", "-q", "-b", "task-inherit")
        self.stage("task.txt", "safe task content\n")
        run(self.repo, "git", "commit", "-q", "-m", "task change")

        run(self.repo, "git", "checkout", "-q", self.main_branch)
        self.stage("trusted-fixture.txt", value + "\n")
        run(self.repo, "git", "commit", "-q", "-m", "trusted default fixture")
        trusted_default = run(self.repo, "git", "rev-parse", "HEAD").stdout.decode().strip()

        run(self.repo, "git", "checkout", "-q", "task-inherit")
        run(
            self.repo,
            "git",
            "merge",
            "--no-ff",
            "-q",
            "-m",
            "merge trusted default",
            self.main_branch,
        )
        findings = secret_scan.scan_range(self.repo, f"{trusted_default}...HEAD")
        self.assertFalse(findings)

    def test_novel_merge_resolution_blob_is_scanned(self) -> None:
        value = self.jwt()
        self.stage("resolution.txt", "common\n")
        run(self.repo, "git", "commit", "-q", "-m", "common merge fixture")

        run(self.repo, "git", "checkout", "-q", "-b", "task-resolution")
        (self.repo / "resolution.txt").write_text("task side\n", encoding="utf-8")
        run(self.repo, "git", "commit", "-q", "-am", "task side")

        run(self.repo, "git", "checkout", "-q", self.main_branch)
        (self.repo / "resolution.txt").write_text("default side\n", encoding="utf-8")
        run(self.repo, "git", "commit", "-q", "-am", "default side")
        trusted_default = run(self.repo, "git", "rev-parse", "HEAD").stdout.decode().strip()

        run(self.repo, "git", "checkout", "-q", "task-resolution")
        merge = subprocess.run(
            ["git", "merge", "--no-ff", "--no-edit", self.main_branch],
            cwd=self.repo,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(merge.returncode, 0)
        (self.repo / "resolution.txt").write_text(value + "\n", encoding="utf-8")
        run(self.repo, "git", "add", "resolution.txt")
        run(self.repo, "git", "commit", "-q", "-m", "custom merge resolution")

        findings = secret_scan.scan_range(self.repo, f"{trusted_default}...HEAD")
        self.assertIn("JSON web token", {finding.category for finding in findings})
        output = io.StringIO()
        with redirect_stderr(output):
            secret_scan.report_findings(findings)
        self.assertNotIn(value, output.getvalue())

    def test_rename_to_a_new_path_is_scanned(self) -> None:
        value = self.jwt()
        self.stage("trusted-name.txt", value + "\n")
        run(self.repo, "git", "commit", "-q", "-m", "trusted path fixture")
        trusted = run(self.repo, "git", "rev-parse", "HEAD").stdout.decode().strip()
        run(self.repo, "git", "mv", "trusted-name.txt", "task-name.txt")
        run(self.repo, "git", "commit", "-q", "-m", "rename fixture")
        findings = secret_scan.scan_range(self.repo, f"{trusted}..HEAD")
        self.assertEqual({finding.path for finding in findings}, {"task-name.txt"})

    def test_binary_diff_disabled_lfs_and_oversize_fail_closed(self) -> None:
        self.stage("binary.dat", b"abc\0def")
        self.assertIn("unscannable binary content", self.categories())
        run(self.repo, "git", "reset", "-q", "--", "binary.dat")
        (self.repo / "binary.dat").unlink()

        self.stage(".gitattributes", "opaque.txt -diff\n")
        self.stage("opaque.txt", "ordinary text\n")
        self.assertIn("unscannable diff-disabled content", self.categories())
        run(self.repo, "git", "reset", "-q", "--", ".gitattributes", "opaque.txt")
        (self.repo / ".gitattributes").unlink()
        (self.repo / "opaque.txt").unlink()

        self.stage(
            "asset.bin",
            "version https://git-lfs.github.com/spec/v1\n"
            "oid sha256:" + "a" * 64 + "\nsize 12\n",
        )
        self.assertIn("unscannable lfs content", self.categories())
        run(self.repo, "git", "reset", "-q", "--", "asset.bin")
        (self.repo / "asset.bin").unlink()

        self.stage("large.txt", "z" * 2048)
        findings = secret_scan.scan_staged(self.repo, max_blob_bytes=1024)
        self.assertIn("unscannable oversize content", {item.category for item in findings})

    def test_gitlink_is_blocked(self) -> None:
        commit = run(self.repo, "git", "rev-parse", "HEAD").stdout.decode().strip()
        run(
            self.repo,
            "git",
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{commit},vendor/component",
        )
        self.assertIn("unscannable gitlink content", self.categories())

    def test_exact_hash_allowlist_only_allows_named_unscannable_blob(self) -> None:
        data = b"abc\0def"
        self.stage("binary.dat", data)
        allowlist = {
            "schemaVersion": 1,
            "unscannable": [
                {
                    "path": "binary.dat",
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "kind": "binary",
                    "reason": "Reviewed synthetic binary fixture",
                }
            ],
        }
        self.stage(secret_scan.ALLOWLIST_PATH, json.dumps(allowlist) + "\n")
        self.assertFalse(secret_scan.scan_staged(self.repo))
        self.stage("binary.dat", data + b"changed")
        self.assertTrue(secret_scan.scan_staged(self.repo))

    def test_allowlist_is_exact_per_path_even_for_the_same_blob(self) -> None:
        data = b"same\0binary"
        self.stage("reviewed.dat", data)
        self.stage("not-reviewed.dat", data)
        allowlist = {
            "schemaVersion": 1,
            "unscannable": [
                {
                    "path": "reviewed.dat",
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "kind": "binary",
                    "reason": "Reviewed only at the named synthetic path",
                }
            ],
        }
        self.stage(secret_scan.ALLOWLIST_PATH, json.dumps(allowlist) + "\n")
        findings = secret_scan.scan_staged(self.repo)
        self.assertEqual(
            {finding.path for finding in findings},
            {"not-reviewed.dat"},
        )

    def test_intermediate_allowlist_must_remain_in_final_range_tree(self) -> None:
        data = b"temporary\0binary"
        self.stage("temporary.dat", data)
        allowlist = {
            "schemaVersion": 1,
            "unscannable": [
                {
                    "path": "temporary.dat",
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "kind": "binary",
                    "reason": "Synthetic intermediate review entry",
                }
            ],
        }
        self.stage(secret_scan.ALLOWLIST_PATH, json.dumps(allowlist) + "\n")
        run(self.repo, "git", "commit", "-q", "-m", "temporary binary")
        run(self.repo, "git", "rm", "-q", "temporary.dat", secret_scan.ALLOWLIST_PATH)
        run(self.repo, "git", "commit", "-q", "-m", "remove temporary binary")
        findings = secret_scan.scan_range(self.repo, f"{self.base}..HEAD")
        self.assertIn("unscannable binary content", {item.category for item in findings})

    def test_allowlist_cannot_suppress_text_secret(self) -> None:
        value = self.jwt()
        data = (value + "\n").encode()
        self.stage("text.txt", data)
        allowlist = {
            "schemaVersion": 1,
            "unscannable": [
                {
                    "path": "text.txt",
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "kind": "binary",
                    "reason": "Must not suppress a text finding",
                }
            ],
        }
        self.stage(secret_scan.ALLOWLIST_PATH, json.dumps(allowlist) + "\n")
        self.assertTrue(secret_scan.scan_staged(self.repo))

    def test_malformed_or_broad_allowlist_fails_closed(self) -> None:
        self.stage(secret_scan.ALLOWLIST_PATH, b"{")
        with self.assertRaises(secret_scan.ScanError):
            secret_scan.scan_staged(self.repo)

        broad = {
            "schemaVersion": 1,
            "unscannable": [
                {
                    "path": "assets/*.bin",
                    "sha256": "a" * 64,
                    "kind": "binary",
                    "reason": "A glob is intentionally too broad",
                }
            ],
        }
        self.stage(secret_scan.ALLOWLIST_PATH, json.dumps(broad) + "\n")
        with self.assertRaises(secret_scan.ScanError):
            secret_scan.scan_staged(self.repo)

    def test_commit_message_and_unusual_filename_are_safe(self) -> None:
        value = self.jwt()
        self.stage("safe.txt", "safe\n")
        run(self.repo, "git", "commit", "-q", "-m", "message " + value)
        findings = secret_scan.scan_range(self.repo, f"{self.base}..HEAD")
        self.assertIn("<commit-message>", {finding.path for finding in findings})

        self.stage("line\nbreak.txt", "safe\n")
        findings = secret_scan.scan_staged(self.repo)
        self.assertFalse(findings)

    def test_bad_range_and_conflicted_index_fail_closed(self) -> None:
        with self.assertRaises(secret_scan.ScanError):
            secret_scan.scan_range(self.repo, "HEAD")

        main_branch = run(self.repo, "git", "branch", "--show-current").stdout.decode().strip()
        run(self.repo, "git", "checkout", "-q", "-b", "synthetic-conflict")
        (self.repo / "README.md").write_text("other\n", encoding="utf-8")
        run(self.repo, "git", "commit", "-q", "-am", "other side")
        run(self.repo, "git", "checkout", "-q", main_branch)
        (self.repo / "README.md").write_text("main\n", encoding="utf-8")
        run(self.repo, "git", "commit", "-q", "-am", "main side")
        merge = subprocess.run(
            ["git", "merge", "--no-edit", "synthetic-conflict"],
            cwd=self.repo,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(merge.returncode, 0)
        with self.assertRaises(secret_scan.ScanError):
            secret_scan.scan_staged(self.repo)

    def test_git_execution_failure_is_wrapped_and_fail_closed(self) -> None:
        with mock.patch.object(
            secret_scan.subprocess,
            "run",
            side_effect=OSError("synthetic execution failure"),
        ):
            with self.assertRaises(secret_scan.ScanError):
                secret_scan.git_bytes(self.repo, "status")
            with self.assertRaises(secret_scan.ScanError):
                secret_scan.git_status(self.repo, "status")

    def test_main_redacts_unexpected_failures_and_rejects_weaker_size_limit(self) -> None:
        value = self.jwt()
        output = io.StringIO()
        argv = ["secret_scan.py", "--staged", "--repo", str(self.repo)]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(secret_scan, "scan_staged", side_effect=ValueError(value)),
            redirect_stderr(output),
            redirect_stdout(output),
        ):
            self.assertEqual(secret_scan.main(), 2)
        self.assertNotIn(value, output.getvalue())

        output = io.StringIO()
        argv = [
            "secret_scan.py",
            "--staged",
            "--repo",
            str(self.repo),
            "--max-blob-bytes",
            str(secret_scan.DEFAULT_MAX_BLOB_BYTES + 1),
        ]
        with mock.patch.object(sys, "argv", argv), redirect_stderr(output):
            self.assertEqual(secret_scan.main(), 2)

    def test_scanner_and_tests_do_not_trigger_their_own_gate(self) -> None:
        self.stage("scanner.py", Path(secret_scan.__file__).read_bytes())
        self.stage("scanner_tests.py", Path(__file__).read_bytes())
        self.assertFalse(secret_scan.scan_staged(self.repo))


if __name__ == "__main__":
    unittest.main()
