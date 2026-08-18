#!/usr/bin/env python3
"""Fail-closed Git secret scanning without printing candidate values."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Iterable


DEFAULT_MAX_BLOB_BYTES = 2 * 1024 * 1024
MAX_ALLOWLIST_BYTES = 256 * 1024
ALLOWLIST_PATH = ".secret-scan-allowlist.json"


class ScanError(RuntimeError):
    """A scanner or Git failure that must block publication."""


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    category: str
    commit: str | None = None


@dataclass(frozen=True)
class BlobTarget:
    path: bytes
    object_oid: str
    object_kind: str
    mode: str
    commit: str | None
    source: str


KNOWN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
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

SENSITIVE_KEY = (
    r"password|passwd|authorization|bearer|api[_-]?key|private[_-]?key|"
    r"(?:access|auth|refresh|client|private|secret|signing|jwt|webhook|database|db|"
    r"service|session|admin|mcp|n8n)[_-]?(?:token|secret|password|passwd|key)|"
    r"(?:[a-z0-9]+[_-])+(?:token|secret|password|passwd|api[_-]?key)"
)

SENSITIVE_ASSIGNMENT = re.compile(
    rf"(?is)(?<![a-z0-9_])(?P<key_quote>[\"']?)(?P<key>{SENSITIVE_KEY})"
    r"(?P=key_quote)(?![a-z0-9_])"
    r"[ \t]*[:=][ \t]*(?:\r?\n[ \t]*)?(?:"
    r'\"(?P<double>(?:\\.|[^\"\\\r\n]){1,4096})\"|'
    r"'(?P<single>(?:\\.|[^'\\\r\n]){1,4096})'|"
    r"`(?P<backtick>(?:\\.|[^`\\\r\n]){1,4096})`|"
    r"(?P<bare>[^\s,;}]{1,4096})"
    r")"
)

AUTHORIZATION_VALUE = re.compile(
    r"(?i)\b(?P<scheme>Bearer|Basic)[ \t]+"
    r"(?P<value>\$\{[A-Z][A-Z0-9_]*\}|<(?:redacted|placeholder)>|[^\s\"'`,;}]+)"
)

CREDENTIAL_URL = re.compile(
    r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:(?P<value>[^@\s/]+)@"
)

RUNTIME_REFERENCE = re.compile(
    r"(?x)^(?:"
    r"(?:process|Bun)\.env(?:\.[A-Z][A-Z0-9_]*|\[[\"'][A-Z][A-Z0-9_]*[\"']\])|"
    r"import\.meta\.env\.[A-Z][A-Z0-9_]*|"
    r"Deno\.env\.get\([\"'][A-Z][A-Z0-9_]*[\"']\)|"
    r"os\.environ(?:\[[\"'][A-Z][A-Z0-9_]*[\"']\]|\.get\([\"'][A-Z][A-Z0-9_]*[\"']\))|"
    r"os\.getenv\([\"'][A-Z][A-Z0-9_]*[\"']\)|"
    r"getenv\([\"'][A-Z][A-Z0-9_]*[\"']\)|"
    r"requiredEnv\([\"'][A-Z][A-Z0-9_]*[\"']\)|"
    r"\$[A-Z][A-Z0-9_]*|"
    r"\$\{[A-Z][A-Z0-9_]*\}|"
    r"\{\{\s*[A-Z][A-Z0-9_]*\s*\}\}|"
    r"\$\{\{\s*(?:secrets|env)\.[A-Z][A-Z0-9_]*\s*\}\}|"
    r"<(?:redacted|placeholder)>"
    r")$"
)

LFS_PREFIX = b"version https://git-lfs.github.com/spec/v1\n"
SAFE_KINDS = {"binary", "diff-disabled", "gitlink", "lfs", "oversize"}


def git_bytes(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            input=input_bytes,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise ScanError("Git could not be executed") from error
    if result.returncode != 0:
        raise ScanError("Git could not produce trusted scan input")
    return result.stdout


def git_text(repo: Path, *args: str) -> str:
    try:
        return git_bytes(repo, *args).decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise ScanError("Git returned malformed text metadata") from error


def git_status(repo: Path, *args: str) -> int:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError as error:
        raise ScanError("Git could not be executed") from error
    return result.returncode


def safe_path(path: bytes) -> str:
    digest = hashlib.sha256(path).hexdigest()[:12]
    try:
        decoded = path.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return f"<redacted-path:{digest}>"
    if any(ord(character) < 32 or ord(character) == 127 for character in decoded):
        return f"<redacted-path:{digest}>"
    if scan_text(decoded, "<path>"):
        return f"<redacted-path:{digest}>"
    return decoded


def normalize_allowlist_path(value: str) -> str:
    candidate = PurePosixPath(value)
    if (
        not value
        or value == "."
        or candidate.is_absolute()
        or ".." in candidate.parts
        or value != candidate.as_posix()
        or value.startswith("-")
        or any(character in value for character in "*?[]\\")
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ScanError("secret-scan allowlist contains an unsafe path")
    return value


def parse_allowlist(raw: bytes | None) -> dict[tuple[str, str, str], str]:
    if raw is None:
        return {}
    try:
        document = json.loads(raw.decode("utf-8", "strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ScanError("secret-scan allowlist is malformed") from error
    if not isinstance(document, dict) or set(document) != {"schemaVersion", "unscannable"}:
        raise ScanError("secret-scan allowlist has unexpected fields")
    if document["schemaVersion"] != 1 or not isinstance(document["unscannable"], list):
        raise ScanError("secret-scan allowlist has an unsupported schema")

    output: dict[tuple[str, str, str], str] = {}
    for entry in document["unscannable"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "kind", "reason"}:
            raise ScanError("secret-scan allowlist entry has unexpected fields")
        path = normalize_allowlist_path(entry["path"] if isinstance(entry["path"], str) else "")
        digest = entry["sha256"] if isinstance(entry["sha256"], str) else ""
        kind = entry["kind"] if isinstance(entry["kind"], str) else ""
        reason = entry["reason"] if isinstance(entry["reason"], str) else ""
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or kind not in SAFE_KINDS:
            raise ScanError("secret-scan allowlist entry is invalid")
        if not 8 <= len(reason) <= 240 or any(
            ord(character) < 32 or ord(character) == 127 for character in reason
        ):
            raise ScanError("secret-scan allowlist reason is invalid")
        key = (path, digest, kind)
        if key in output:
            raise ScanError("secret-scan allowlist contains a duplicate entry")
        output[key] = reason
    return output


def scan_text(text: str, path: str, commit: str | None = None) -> list[Finding]:
    findings: set[Finding] = set()
    for category, pattern in KNOWN_PATTERNS:
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            findings.add(Finding(path, line, category, commit))

    for match in AUTHORIZATION_VALUE.finditer(text):
        value = match.group("value").strip()
        if RUNTIME_REFERENCE.fullmatch(value):
            continue
        scheme = match.group("scheme").lower()
        if scheme == "bearer" and len(value) >= 8:
            category = "authorization header credential"
        elif scheme == "basic" and re.fullmatch(r"[A-Za-z0-9+/]{12,}={0,2}", value):
            category = "authorization header credential"
        else:
            continue
        line = text.count("\n", 0, match.start("value")) + 1
        findings.add(Finding(path, line, category, commit))

    for match in CREDENTIAL_URL.finditer(text):
        value = match.group("value").strip()
        if len(value) < 8 or RUNTIME_REFERENCE.fullmatch(value):
            continue
        line = text.count("\n", 0, match.start("value")) + 1
        findings.add(Finding(path, line, "credential-bearing URL", commit))

    for match in SENSITIVE_ASSIGNMENT.finditer(text):
        value = next(
            (
                candidate
                for candidate in (
                    match.group("double"),
                    match.group("single"),
                    match.group("backtick"),
                    match.group("bare"),
                )
                if candidate is not None
            ),
            "",
        ).strip()
        if RUNTIME_REFERENCE.fullmatch(value):
            continue
        key = match.group("key").lower().replace("-", "_")
        minimum = 8 if re.search(r"passw(?:or)?d", key) else 12
        if len(value) < minimum:
            continue
        value_group = next(
            name
            for name in ("double", "single", "backtick", "bare")
            if match.group(name) is not None
        )
        line = text.count("\n", 0, match.start(value_group)) + 1
        findings.add(Finding(path, line, "credential-like assignment", commit))
    return sorted(findings)


def parse_name_status(raw: bytes) -> list[bytes]:
    fields = raw.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    paths: list[bytes] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if not re.fullmatch(rb"[ACMRT][0-9]*", status):
            raise ScanError("Git returned malformed path metadata")
        if status.startswith((b"R", b"C")):
            if index + 1 >= len(fields):
                raise ScanError("Git returned incomplete rename metadata")
            index += 1
            paths.append(fields[index])
            index += 1
        else:
            if index >= len(fields):
                raise ScanError("Git returned incomplete path metadata")
            paths.append(fields[index])
            index += 1
    return paths


def object_type(repo: Path, object_oid: str) -> str:
    return git_text(repo, "cat-file", "-t", object_oid).strip()


def object_size(repo: Path, object_oid: str) -> int:
    value = git_text(repo, "cat-file", "-s", object_oid).strip()
    if not value.isdigit():
        raise ScanError("Git returned malformed object size")
    return int(value)


def object_bytes(repo: Path, object_oid: str) -> bytes:
    return git_bytes(repo, "cat-file", "blob", object_oid)


def object_sha256(repo: Path, object_oid: str, expected_size: int) -> str:
    try:
        with subprocess.Popen(
            ["git", "-C", str(repo), "cat-file", "blob", object_oid],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as process:
            if process.stdout is None:
                raise ScanError("Git did not provide a scan stream")
            digest = hashlib.sha256()
            bytes_read = 0
            while True:
                chunk = process.stdout.read(1024 * 1024)
                if not chunk:
                    break
                bytes_read += len(chunk)
                digest.update(chunk)
            returncode = process.wait()
    except OSError as error:
        raise ScanError("Git could not stream a scan object") from error
    if returncode != 0 or bytes_read != expected_size:
        raise ScanError("Git could not stream a scan object")
    return digest.hexdigest()


def diff_attribute(repo: Path, path: bytes, source: str) -> str:
    if b"\0" in path:
        raise ScanError("Git path contains an invalid separator")
    path_text = path.decode("utf-8", "surrogateescape")
    if source == "index":
        raw = git_bytes(repo, "check-attr", "-z", "--cached", "diff", "--", path_text)
    else:
        raw = git_bytes(repo, "check-attr", "-z", f"--source={source}", "diff", "--", path_text)
    fields = raw.split(b"\0")
    if len(fields) != 4 or fields[0] != path or fields[1] != b"diff" or fields[3] != b"":
        raise ScanError("Git returned malformed attribute metadata")
    try:
        return fields[2].decode("ascii", "strict")
    except UnicodeDecodeError as error:
        raise ScanError("Git returned malformed attribute metadata") from error


def decode_metadata(value: bytes, label: str) -> str:
    try:
        decoded = value.decode("ascii", "strict")
    except UnicodeDecodeError as error:
        raise ScanError(f"Git returned malformed {label}") from error
    return decoded


def validate_entry(mode: bytes, kind: bytes, oid: bytes) -> tuple[str, str, str]:
    mode_text = decode_metadata(mode, "file mode")
    kind_text = decode_metadata(kind, "object type")
    oid_text = decode_metadata(oid, "object identifier")
    if not re.fullmatch(r"[0-7]{6}", mode_text):
        raise ScanError("Git returned malformed file mode")
    if kind_text not in {"blob", "commit"}:
        raise ScanError("Git returned an unsupported tree entry")
    if not re.fullmatch(r"[0-9a-f]{40,64}", oid_text):
        raise ScanError("Git returned malformed object identifier")
    if (mode_text == "160000") != (kind_text == "commit"):
        raise ScanError("Git returned inconsistent tree metadata")
    return mode_text, kind_text, oid_text


def entry_for_source(
    repo: Path,
    source: str,
    path: bytes,
    *,
    missing_ok: bool = False,
) -> tuple[str, str, str] | None:
    path_text = path.decode("utf-8", "surrogateescape")
    if source == "index":
        raw = git_bytes(repo, "ls-files", "-s", "-z", "--", path_text)
        records = [item for item in raw.split(b"\0") if item]
        if not records and missing_ok:
            return None
        if len(records) != 1:
            raise ScanError("Git index path did not resolve to one stage-zero entry")
        header, separator, returned_path = records[0].partition(b"\t")
        parts = header.split()
        if (
            separator != b"\t"
            or returned_path != path
            or len(parts) != 3
            or parts[2] != b"0"
        ):
            raise ScanError("Git index contains an unmerged or malformed entry")
        mode, oid = parts[:2]
        kind = b"commit" if mode == b"160000" else b"blob"
        return validate_entry(mode, kind, oid)

    raw = git_bytes(repo, "ls-tree", "-z", source, "--", path_text)
    records = [item for item in raw.split(b"\0") if item]
    if not records and missing_ok:
        return None
    if len(records) != 1:
        raise ScanError("Git tree path did not resolve to one entry")
    header, separator, returned_path = records[0].partition(b"\t")
    parts = header.split()
    if separator != b"\t" or returned_path != path or len(parts) != 3:
        raise ScanError("Git tree contains malformed metadata")
    return validate_entry(parts[0], parts[1], parts[2])


def allowlist_bytes(repo: Path, source: str) -> bytes | None:
    entry = entry_for_source(
        repo,
        source,
        ALLOWLIST_PATH.encode("utf-8"),
        missing_ok=True,
    )
    if entry is None:
        return None
    _, kind, oid = entry
    if kind != "blob":
        raise ScanError("secret-scan allowlist is not a regular Git blob")
    size = object_size(repo, oid)
    if size > MAX_ALLOWLIST_BYTES:
        raise ScanError("secret-scan allowlist is too large")
    data = object_bytes(repo, oid)
    if len(data) != size:
        raise ScanError("Git returned incomplete allowlist content")
    return data


def classify_unscannable(
    repo: Path,
    target: BlobTarget,
    max_blob_bytes: int,
) -> tuple[str | None, bytes | None, str]:
    if target.mode == "160000":
        digest = hashlib.sha256(target.object_oid.encode("ascii")).hexdigest()
        return "gitlink", None, digest
    if target.object_kind != "blob" or object_type(repo, target.object_oid) != "blob":
        raise ScanError("Git object metadata changed during the scan")
    size = object_size(repo, target.object_oid)
    if size > max_blob_bytes:
        return "oversize", None, object_sha256(repo, target.object_oid, size)
    attribute = diff_attribute(repo, target.path, target.source)
    data = object_bytes(repo, target.object_oid)
    if len(data) != size:
        raise ScanError("Git returned incomplete scan content")
    digest = hashlib.sha256(data).hexdigest()
    if attribute == "unset":
        return "diff-disabled", data, digest
    if data.startswith(LFS_PREFIX):
        return "lfs", data, digest
    if b"\0" in data:
        return "binary", data, digest
    try:
        data.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return "binary", data, digest
    return None, data, digest


def scan_targets(
    repo: Path,
    targets: Iterable[BlobTarget],
    *,
    max_blob_bytes: int,
    allowlist_source: str | None = None,
) -> list[Finding]:
    findings: set[Finding] = set()
    seen: set[tuple[str, str, bytes]] = set()
    allowlists: dict[str, dict[tuple[str, str, str], str]] = {}

    for target in targets:
        identity = (target.source, target.object_oid, target.path)
        if identity in seen:
            continue
        seen.add(identity)
        path_display = safe_path(target.path)
        current_allowlist_source = allowlist_source or target.source
        source_allowlist = allowlists.setdefault(
            current_allowlist_source,
            parse_allowlist(allowlist_bytes(repo, current_allowlist_source)),
        )
        kind, data, digest = classify_unscannable(repo, target, max_blob_bytes)
        if kind:
            try:
                allowlist_path = normalize_allowlist_path(
                    target.path.decode("utf-8", "strict")
                )
            except (UnicodeDecodeError, ScanError):
                allowlist_path = ""
            key = (allowlist_path, digest, kind)
            if not allowlist_path or key not in source_allowlist:
                findings.add(Finding(path_display, 0, f"unscannable {kind} content", target.commit))
            continue
        if data is None:
            raise ScanError("scanner classification returned incomplete content")
        text = data.decode("utf-8", "strict")
        findings.update(scan_text(text, path_display, target.commit))
    return sorted(findings)


def staged_targets(repo: Path) -> list[BlobTarget]:
    if git_bytes(repo, "ls-files", "-u", "-z"):
        raise ScanError("Git index contains unresolved conflicts")
    paths = parse_name_status(
        git_bytes(
            repo,
            "diff",
            "--cached",
            "--name-status",
            "-z",
            "--diff-filter=ACMRT",
            "--",
        )
    )
    targets: list[BlobTarget] = []
    for path in paths:
        entry = entry_for_source(repo, "index", path)
        if entry is None:
            raise ScanError("Git index entry disappeared during the scan")
        mode, kind, oid = entry
        targets.append(
            BlobTarget(
                path=path,
                object_oid=oid,
                object_kind=kind,
                mode=mode,
                commit=None,
                source="index",
            )
        )
    return targets


def resolve_range(repo: Path, revision_range: str) -> tuple[str, str]:
    if "..." in revision_range:
        left, right = revision_range.split("...", 1)
        if not left or not right:
            raise ScanError("revision range is malformed")
        baseline = git_text(repo, "merge-base", left, right).strip()
        head = git_text(repo, "rev-parse", f"{right}^{{commit}}").strip()
    elif ".." in revision_range:
        left, right = revision_range.split("..", 1)
        if not left or not right:
            raise ScanError("revision range is malformed")
        baseline = git_text(repo, "rev-parse", f"{left}^{{commit}}").strip()
        head = git_text(repo, "rev-parse", f"{right}^{{commit}}").strip()
    else:
        raise ScanError("revision range must use two-dot or three-dot syntax")
    if not re.fullmatch(r"[0-9a-f]{40,64}", baseline) or not re.fullmatch(r"[0-9a-f]{40,64}", head):
        raise ScanError("revision range did not resolve to commits")
    if git_status(repo, "merge-base", "--is-ancestor", baseline, head) != 0:
        raise ScanError("revision range baseline is not an ancestor of its head")
    return baseline, head


def empty_tree(repo: Path) -> str:
    try:
        value = git_bytes(
            repo,
            "hash-object",
            "-t",
            "tree",
            "--stdin",
            input_bytes=b"",
        ).decode("ascii", "strict").strip()
    except UnicodeDecodeError as error:
        raise ScanError("Git returned a malformed empty-tree identifier") from error
    if not re.fullmatch(r"[0-9a-f]{40,64}", value):
        raise ScanError("Git returned a malformed empty-tree identifier")
    return value


def commit_targets(
    repo: Path,
    revision_range: str,
) -> tuple[list[BlobTarget], list[Finding], str]:
    baseline, head = resolve_range(repo, revision_range)
    commits = [
        value
        for value in git_text(repo, "rev-list", "--reverse", "--topo-order", f"{baseline}..{head}").splitlines()
        if value
    ]
    targets: list[BlobTarget] = []
    message_findings: set[Finding] = set()
    for commit in commits:
        if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise ScanError("Git returned a malformed commit identifier")
        message = git_text(repo, "show", "-s", "--format=%B", commit)
        message_findings.update(scan_text(message, "<commit-message>", commit[:12]))
        parents = git_text(repo, "show", "-s", "--format=%P", commit).split()
        if not parents:
            parents = [empty_tree(repo)]
        paths: set[bytes] = set()
        for parent in parents:
            raw = git_bytes(
                repo,
                "diff-tree",
                "--no-commit-id",
                "-r",
                "--name-status",
                "-z",
                "--diff-filter=ACMRT",
                parent,
                commit,
                "--",
            )
            paths.update(parse_name_status(raw))
        for path in sorted(paths):
            entry = entry_for_source(repo, commit, path)
            if entry is None:
                raise ScanError("Git tree entry disappeared during the scan")
            mode, kind, oid = entry
            if len(parents) > 1:
                inherited = False
                for parent in parents:
                    parent_entry = entry_for_source(
                        repo,
                        parent,
                        path,
                        missing_ok=True,
                    )
                    if parent_entry is None:
                        continue
                    _, parent_kind, parent_oid = parent_entry
                    if parent_kind == kind and parent_oid == oid:
                        inherited = True
                        break
                if inherited:
                    continue
            targets.append(
                BlobTarget(
                    path=path,
                    object_oid=oid,
                    object_kind=kind,
                    mode=mode,
                    commit=commit[:12],
                    source=commit,
                )
            )
    return targets, sorted(message_findings), head


def scan_staged(repo: Path, *, max_blob_bytes: int = DEFAULT_MAX_BLOB_BYTES) -> list[Finding]:
    return scan_targets(repo, staged_targets(repo), max_blob_bytes=max_blob_bytes)


def scan_range(
    repo: Path,
    revision_range: str,
    *,
    max_blob_bytes: int = DEFAULT_MAX_BLOB_BYTES,
) -> list[Finding]:
    targets, message_findings, head = commit_targets(repo, revision_range)
    findings = set(message_findings)
    findings.update(
        scan_targets(
            repo,
            targets,
            max_blob_bytes=max_blob_bytes,
            allowlist_source=head,
        )
    )
    return sorted(findings)


def report_findings(findings: list[Finding]) -> None:
    print("Secret scan blocked this change; values were not displayed.", file=sys.stderr)
    for finding in findings:
        location = str(finding.line) if finding.line else "content"
        commit = f" commit={finding.commit}" if finding.commit else ""
        print(f"{finding.path}:{location}: {finding.category}{commit}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true")
    mode.add_argument("--range", dest="revision_range")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--max-blob-bytes", type=int, default=DEFAULT_MAX_BLOB_BYTES)
    args = parser.parse_args()
    if not 1024 <= args.max_blob_bytes <= DEFAULT_MAX_BLOB_BYTES:
        print("Secret scan error: invalid size policy.", file=sys.stderr)
        return 2
    try:
        repo = Path(args.repo).resolve()
        findings = (
            scan_staged(repo, max_blob_bytes=args.max_blob_bytes)
            if args.staged
            else scan_range(repo, args.revision_range, max_blob_bytes=args.max_blob_bytes)
        )
    except ScanError as error:
        print(f"Secret scan error: {error}", file=sys.stderr)
        return 2
    except Exception:
        print(
            "Secret scan error: internal failure; candidate values were not displayed.",
            file=sys.stderr,
        )
        return 2
    if findings:
        report_findings(findings)
        return 1
    print("Secret scan passed; no values were displayed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
