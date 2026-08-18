#!/usr/bin/env python3
"""Scan a resulting Git tree for high-confidence secrets without printing values."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import re
import subprocess
import sys


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    category: str


PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    ),
    (
        "JSON web token",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
    (
        "Git hosting token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    ),
    ("cloud access key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    (
        "payment provider secret",
        re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    ),
    ("chat service token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
)

GENERIC_ASSIGNMENT = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"password|passwd|private[_-]?key|secret[_-]?key)\s*[=:]\s*"
    r"[\"']?([A-Za-z0-9_./+\-=]{16,})"
)

PLACEHOLDERS = (
    "process.env",
    "os.environ",
    "getenv(",
    "${",
    "{{",
    "<redacted",
    "<placeholder",
    "example",
    "placeholder",
    "dummy",
    "changeme",
    "your_",
    "your-",
    "fake",
    "not-a-secret",
    "secret-scan: allow",
)


def git(*args: str, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], check=False, capture_output=True, text=text
    )


def diff(mode: str, revision_range: str | None) -> str:
    if mode == "staged":
        result = git(
            "-c",
            "core.quotePath=false",
            "diff",
            "--cached",
            "--unified=0",
            "--no-color",
            "--diff-filter=ACMR",
            "--",
        )
    else:
        result = git(
            "-c",
            "core.quotePath=false",
            "diff",
            "--unified=0",
            "--no-color",
            "--diff-filter=ACMR",
            revision_range or "",
            "--",
        )
    if result.returncode != 0:
        raise RuntimeError("Git could not produce the candidate diff")
    return result.stdout


def added_lines(patch: str):
    current_path: str | None = None
    current_line = 0
    for line in patch.splitlines():
        if line.startswith("+++ "):
            value = line[4:]
            current_path = value[2:] if value.startswith("b/") else value
            if current_path == "/dev/null":
                current_path = None
            continue
        if line.startswith("@@ "):
            match = re.search(r"\+(\d+)(?:,\d+)?", line)
            current_line = int(match.group(1)) if match else 0
            continue
        if current_path is None or current_line == 0:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            yield current_path, current_line, line[1:]
            current_line += 1
        elif line.startswith(" "):
            current_line += 1


def is_placeholder(line: str) -> bool:
    lowered = line.lower()
    return any(marker in lowered for marker in PLACEHOLDERS)


def scan(mode: str, revision_range: str | None = None) -> list[Finding]:
    findings: set[Finding] = set()
    for path, line_number, line in added_lines(diff(mode, revision_range)):
        for category, pattern in PATTERNS:
            if pattern.search(line) and not is_placeholder(line):
                findings.add(Finding(path, line_number, category))
        if GENERIC_ASSIGNMENT.search(line) and not is_placeholder(line):
            findings.add(Finding(path, line_number, "credential-like assignment"))
    return sorted(findings)


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true")
    mode.add_argument("--range", dest="revision_range")
    args = parser.parse_args()

    try:
        findings = scan(
            "staged" if args.staged else "range",
            args.revision_range,
        )
    except RuntimeError as error:
        print(f"secret scan error: {error}", file=sys.stderr)
        return 2

    if not findings:
        print("Secret scan passed; no values were displayed.")
        return 0

    print("Secret scan blocked this change; values were not displayed.", file=sys.stderr)
    for finding in findings:
        location = str(finding.line) if finding.line else "large-file"
        print(f"{finding.path}:{location}: {finding.category}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
