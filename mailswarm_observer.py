#!/usr/bin/env python3
"""Deterministic, read-only MailSwarm work observer.

The observer consumes a versioned, MailSwarm-scoped JSON snapshot, classifies
only explicit machine state, persists evidence locally, and can emit bounded
alerts. It does not dispatch agents, change work, merge, deploy, or call an AI
model. Deployment defaults are disabled and shadow-only.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx


JsonObject = dict[str, Any]
SnapshotReader = Callable[[], Awaitable[JsonObject]]
AlertSender = Callable[[str], Awaitable[bool]]

FULL_SOURCE_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
ARTIFACT_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
MAX_EVIDENCE_LINKS = 3

ACTIVE_STATUSES = {"in progress", "in review"}
EXPLICIT_RUNNING_STATES = {"active", "in progress", "running"}
EXPLICIT_STOPPED_STATES = {
    "canceled",
    "cancelled",
    "failed",
    "stopped",
    "timed out",
}
KNOWN_NONTERMINAL_PROVIDER_STATES = EXPLICIT_RUNNING_STATES | {
    "pending",
    "queued",
    "starting",
}
FAILURE_STATES = {"error", "failed", "failure", "timed out"}
SUCCESS_STATES = {"ok", "passed", "success", "succeeded"}
ALERTABLE_CLASSIFICATIONS = {
    "actions_usage_warning",
    "capacity_paused",
    "confirmed_stopped",
    "migration_conflict",
    "owner_decision_required",
    "quality_failed",
    "status_unknown",
    "suspected_stalled",
}
CLASSIFICATION_PRIORITY = {
    "migration_conflict": 100,
    "quality_failed": 95,
    "confirmed_stopped": 90,
    "owner_decision_required": 85,
    "capacity_paused": 80,
    "status_unknown": 70,
    "actions_usage_warning": 65,
    "suspected_stalled": 60,
    "blocked": 50,
    "in_review": 30,
    "active": 20,
    "ready": 10,
    "done": 0,
    "idle": 0,
}


def _truthy(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_strict_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _state(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-").replace("-", " ")


def _safe_url(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        return urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, "", "")
        )[:2000]
    except ValueError:
        return None


def _valid_source_sha(value: Any) -> bool:
    return FULL_SOURCE_SHA_PATTERN.fullmatch(str(value or "")) is not None


def _valid_artifact_digest(value: Any) -> bool:
    return ARTIFACT_DIGEST_PATTERN.fullmatch(str(value or "")) is not None


def _bounded_evidence_urls(*values: Any) -> tuple[str, ...]:
    urls: list[str] = []
    for value in values:
        candidates = value if isinstance(value, (list, tuple)) else [value]
        for candidate in candidates:
            url = _safe_url(candidate)
            if url and url not in urls:
                urls.append(url)
            if len(urls) >= MAX_EVIDENCE_LINKS:
                return tuple(urls)
    return tuple(urls)


def _fingerprint(*parts: Any) -> str:
    material = "\x1f".join(str(part or "") for part in parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ObserverConfig:
    enabled: bool
    shadow_mode: bool
    alerts_enabled: bool
    source_url: str
    source_bearer_token: str
    linear_team_id: str
    github_repository: str
    hermes_home: Path
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    interval_seconds: int = 900
    max_attempts: int = 2
    timeout_seconds: int = 15
    source_max_age_minutes: int = 30
    stalled_minutes: int = 120
    loop_stale_minutes: int = 150
    integration_stale_minutes: int = 1560
    actions_warning_percent: float = 80.0
    alert_cooldown_minutes: int = 360
    summary_enabled: bool = False
    summary_shadow_mode: bool = True

    @classmethod
    def from_env(cls) -> "ObserverConfig":
        return cls(
            enabled=_truthy(os.environ.get("MAILSWARM_OBSERVER_ENABLED")),
            shadow_mode=_truthy(
                os.environ.get("MAILSWARM_OBSERVER_SHADOW_MODE"), default=True
            ),
            alerts_enabled=_truthy(
                os.environ.get("MAILSWARM_OBSERVER_ALERTS_ENABLED")
            ),
            source_url=os.environ.get("MAILSWARM_OBSERVER_SOURCE_URL", "").strip(),
            source_bearer_token=os.environ.get(
                "MAILSWARM_OBSERVER_SOURCE_BEARER_TOKEN", ""
            ).strip(),
            linear_team_id=os.environ.get("MAILSWARM_LINEAR_TEAM_ID", "").strip(),
            github_repository=os.environ.get(
                "MAILSWARM_GITHUB_REPOSITORY", ""
            ).strip(),
            hermes_home=Path(
                os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
            ),
            telegram_bot_token=os.environ.get(
                "MAILSWARM_OBSERVER_TELEGRAM_BOT_TOKEN", ""
            ).strip(),
            telegram_chat_id=os.environ.get(
                "MAILSWARM_OBSERVER_TELEGRAM_CHAT_ID", ""
            ).strip(),
            interval_seconds=max(
                900, _env_int("MAILSWARM_OBSERVER_INTERVAL_SECONDS", 900)
            ),
            max_attempts=max(
                1, min(2, _env_int("MAILSWARM_OBSERVER_MAX_ATTEMPTS", 2))
            ),
            timeout_seconds=max(
                5, min(30, _env_int("MAILSWARM_OBSERVER_TIMEOUT_SECONDS", 15))
            ),
            source_max_age_minutes=max(
                5, _env_int("MAILSWARM_OBSERVER_SOURCE_MAX_AGE_MINUTES", 30)
            ),
            stalled_minutes=max(
                30, _env_int("MAILSWARM_OBSERVER_STALLED_MINUTES", 120)
            ),
            loop_stale_minutes=max(
                30, _env_int("MAILSWARM_OBSERVER_LOOP_STALE_MINUTES", 150)
            ),
            integration_stale_minutes=max(
                60,
                _env_int("MAILSWARM_OBSERVER_INTEGRATION_STALE_MINUTES", 1560),
            ),
            actions_warning_percent=max(
                1.0,
                min(
                    100.0,
                    _env_float("MAILSWARM_OBSERVER_ACTIONS_WARNING_PERCENT", 80.0),
                ),
            ),
            alert_cooldown_minutes=max(
                30,
                _env_int("MAILSWARM_OBSERVER_ALERT_COOLDOWN_MINUTES", 360),
            ),
            summary_enabled=_truthy(
                os.environ.get("MAILSWARM_OBSERVER_SUMMARY_ENABLED")
            ),
            summary_shadow_mode=_truthy(
                os.environ.get("MAILSWARM_OBSERVER_SUMMARY_SHADOW_MODE"),
                default=True,
            ),
        )

    @property
    def database_path(self) -> Path:
        return self.hermes_home / "mailswarm-observer" / "observer.sqlite3"

    def missing_requirements(self) -> list[str]:
        if not self.enabled:
            return []
        required = {
            "MAILSWARM_OBSERVER_SOURCE_URL": self.source_url,
            "MAILSWARM_LINEAR_TEAM_ID": self.linear_team_id,
            "MAILSWARM_GITHUB_REPOSITORY": self.github_repository,
        }
        if self.alerts_enabled and not self.shadow_mode:
            required.update(
                {
                    "MAILSWARM_OBSERVER_TELEGRAM_BOT_TOKEN": self.telegram_bot_token,
                    "MAILSWARM_OBSERVER_TELEGRAM_CHAT_ID": self.telegram_chat_id,
                }
            )
        return [name for name, value in required.items() if not value]

    def summary_missing_requirements(self) -> list[str]:
        if (
            not self.summary_enabled
            or self.summary_shadow_mode
            or self.shadow_mode
        ):
            return []
        required = {
            "MAILSWARM_OBSERVER_TELEGRAM_BOT_TOKEN": self.telegram_bot_token,
            "MAILSWARM_OBSERVER_TELEGRAM_CHAT_ID": self.telegram_chat_id,
        }
        return [name for name, value in required.items() if not value]


@dataclass(frozen=True)
class Condition:
    classification: str
    severity: str
    subject: str
    reason: str
    attempt: int = 0
    sha: str = ""
    artifact_digest: str = ""
    evidence_urls: tuple[str, ...] = ()

    @property
    def fingerprint(self) -> str:
        return _fingerprint(
            "mailswarm-observer",
            self.classification,
            self.subject,
            self.attempt,
            self.sha,
            self.artifact_digest,
        )

    def as_dict(self) -> JsonObject:
        return {
            "classification": self.classification,
            "severity": self.severity,
            "subject": self.subject,
            "reason": self.reason,
            "attempt": self.attempt,
            "sha": self.sha,
            "artifact_digest": self.artifact_digest,
            "evidence_urls": list(self.evidence_urls),
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class MilestoneUpdate:
    milestone_id: str
    state: str
    sha: str
    artifact_digest: str
    evidence_urls: tuple[str, ...] = ()
    delivery_attempts: int = 0

    @property
    def subject(self) -> str:
        return f"milestone:{self.milestone_id}"

    @property
    def fingerprint(self) -> str:
        return _fingerprint(
            "mailswarm-milestone-update",
            self.milestone_id,
            self.state,
            self.sha,
            self.artifact_digest,
        )


@dataclass(frozen=True)
class ClassificationResult:
    classification: str
    source_state: str
    observed_at: str | None
    active_work_count: int
    issue_counts: dict[str, int]
    issues: list[JsonObject]
    conditions: list[Condition]
    milestone_counts: dict[str, int]
    milestones: list[JsonObject]


class SnapshotError(ValueError):
    pass


def _condition_for_issue(issue: JsonObject, classification: str) -> Condition | None:
    if classification not in ALERTABLE_CLASSIFICATIONS:
        return None
    issue_id = str(issue["issue_id"])
    reasons = {
        "confirmed_stopped": "provider session explicitly stopped",
        "owner_decision_required": "owner decision is explicitly required",
        "quality_failed": "a deterministic quality check explicitly failed",
        "status_unknown": "required attempt state is unavailable",
        "suspected_stalled": "active work has no recent recorded activity",
        "capacity_paused": "work is explicitly capacity paused",
    }
    severity = "critical" if classification in {"confirmed_stopped", "quality_failed"} else "warning"
    sha = str(issue.get("last_cloud_test_sha") or issue.get("last_ci_sha") or "")
    artifact_digest = str(
        issue.get("last_cloud_test_digest") or issue.get("last_ci_digest") or ""
    )
    check_url = str(
        issue.get("last_cloud_test_evidence_url")
        or issue.get("last_ci_evidence_url")
        or ""
    )
    if classification == "quality_failed":
        ci_failed = _state(issue.get("last_ci_state")) in FAILURE_STATES
        cloud_failed = _state(issue.get("last_cloud_test_state")) in FAILURE_STATES
        if ci_failed and cloud_failed:
            ci_at = _parse_timestamp(issue.get("last_ci_at"))
            cloud_at = _parse_timestamp(issue.get("last_cloud_test_at"))
            if cloud_at is not None and (ci_at is None or cloud_at > ci_at):
                sha = str(issue.get("last_cloud_test_sha") or "")
                artifact_digest = str(issue.get("last_cloud_test_digest") or "")
                check_url = str(issue.get("last_cloud_test_evidence_url") or "")
            else:
                sha = str(issue.get("last_ci_sha") or "")
                artifact_digest = str(issue.get("last_ci_digest") or "")
                check_url = str(issue.get("last_ci_evidence_url") or "")
        elif ci_failed:
            sha = str(issue.get("last_ci_sha") or "")
            artifact_digest = str(issue.get("last_ci_digest") or "")
            check_url = str(issue.get("last_ci_evidence_url") or "")
        elif cloud_failed:
            sha = str(issue.get("last_cloud_test_sha") or "")
            artifact_digest = str(issue.get("last_cloud_test_digest") or "")
            check_url = str(issue.get("last_cloud_test_evidence_url") or "")
    evidence_urls = _bounded_evidence_urls(
        check_url,
        [item.get("url") for item in issue.get("evidence", [])],
    )
    return Condition(
        classification=classification,
        severity=severity,
        subject=issue_id,
        reason=reasons.get(classification, classification.replace("_", " ")),
        attempt=int(issue.get("attempt") or 0),
        sha=sha,
        artifact_digest=artifact_digest,
        evidence_urls=evidence_urls,
    )


def _classify_issue(
    raw: JsonObject,
    *,
    now: datetime,
    stalled_minutes: int,
) -> tuple[JsonObject, str]:
    issue_id = str(raw.get("id") or "").strip()
    if not issue_id:
        raise SnapshotError("issue id is required")
    status = _state(raw.get("status"))
    labels = {_state(label) for label in raw.get("labels", []) if isinstance(label, str)}
    provider = raw.get("provider_session")
    provider = provider if isinstance(provider, dict) else {}
    provider_state = _state(provider.get("state"))
    last_activity = _parse_timestamp(
        provider.get("last_activity_at") or raw.get("last_activity_at")
    )
    ci = raw.get("ci") if isinstance(raw.get("ci"), dict) else {}
    cloud_test = (
        raw.get("cloud_test") if isinstance(raw.get("cloud_test"), dict) else {}
    )
    loop = raw.get("loop") if isinstance(raw.get("loop"), dict) else {}
    ci_state = _state(ci.get("state"))
    cloud_state = _state(cloud_test.get("state"))

    if "owner decision required" in labels:
        classification = "owner_decision_required"
    elif "quality failed" in labels or ci_state in FAILURE_STATES or cloud_state in FAILURE_STATES:
        classification = "quality_failed"
    elif provider_state in EXPLICIT_STOPPED_STATES:
        classification = "confirmed_stopped"
    elif "capacity paused" in labels:
        classification = "capacity_paused"
    elif status == "blocked":
        classification = "blocked"
    elif status in ACTIVE_STATUSES:
        if provider_state not in KNOWN_NONTERMINAL_PROVIDER_STATES:
            classification = "status_unknown"
        elif provider_state in EXPLICIT_RUNNING_STATES and last_activity is None:
            classification = "status_unknown"
        elif (
            provider_state in EXPLICIT_RUNNING_STATES
            and now - last_activity > timedelta(minutes=stalled_minutes)
        ):
            classification = "suspected_stalled"
        elif status == "in review":
            classification = "in_review"
        else:
            classification = "active"
    elif status == "ready for agent":
        classification = "ready"
    elif status in {"done", "canceled", "cancelled"}:
        classification = "done"
    else:
        classification = "idle"

    try:
        attempt = max(0, int(raw.get("attempt") or 0))
    except (TypeError, ValueError):
        attempt = 0
    evidence: list[JsonObject] = []
    for item in raw.get("evidence", []):
        if not isinstance(item, dict):
            continue
        url = _safe_url(item.get("url"))
        if not url:
            continue
        evidence.append(
            {
                "kind": str(item.get("kind") or "evidence")[:100],
                "url": url,
                "sha": str(item.get("sha") or "")[:100],
                "artifact_digest": str(item.get("artifact_digest") or "")[:200],
                "observed_at": str(item.get("observed_at") or "")[:100],
            }
        )
    decisions: list[JsonObject] = []
    for item in raw.get("owner_decisions", []):
        if not isinstance(item, dict):
            continue
        source_url = _safe_url(item.get("source_url"))
        decided_at = _parse_timestamp(item.get("decided_at"))
        if not source_url or decided_at is None:
            continue
        decisions.append(
            {
                "source_url": source_url,
                "decided_at": _iso(decided_at),
                "decision": str(item.get("decision") or "")[:500],
            }
        )
    normalized = {
        "issue_id": issue_id,
        "attempt": attempt,
        "component": str(raw.get("component") or "")[:100],
        "migration_owner": bool(raw.get("migration_owner")),
        "status": status,
        "provider_session_id": str(provider.get("id") or "")[:200],
        "provider_state": provider_state,
        "last_activity_at": _iso(last_activity) if last_activity else None,
        "last_ci_at": str(ci.get("completed_at") or ci.get("updated_at") or "")[:100],
        "last_ci_state": ci_state,
        "last_ci_sha": str(ci.get("sha") or "")[:100],
        "last_ci_digest": str(ci.get("artifact_digest") or "")[:200],
        "last_ci_evidence_url": _safe_url(ci.get("evidence_url")),
        "last_cloud_test_at": str(
            cloud_test.get("completed_at") or cloud_test.get("updated_at") or ""
        )[:100],
        "last_cloud_test_state": cloud_state,
        "last_cloud_test_sha": str(cloud_test.get("sha") or "")[:100],
        "last_cloud_test_digest": str(cloud_test.get("artifact_digest") or "")[:200],
        "last_cloud_test_evidence_url": _safe_url(cloud_test.get("evidence_url")),
        "last_loop_heartbeat_at": str(loop.get("heartbeat_at") or "")[:100],
        "classification": classification,
        "evidence": evidence,
        "owner_decisions": decisions,
    }
    return normalized, classification


def _classify_milestones(
    raw_milestones: Any,
    *,
    snapshot_observed_at: datetime,
) -> tuple[list[JsonObject], dict[str, int], list[Condition]]:
    if raw_milestones is None:
        return [], {}, []
    if not isinstance(raw_milestones, list):
        raise SnapshotError("snapshot milestones must be a list")
    milestones: list[JsonObject] = []
    counts: dict[str, int] = {}
    conditions: list[Condition] = []
    milestone_ids: set[str] = set()
    for raw in raw_milestones:
        if not isinstance(raw, dict):
            raise SnapshotError("snapshot milestone must be an object")
        milestone_id = str(raw.get("id") or "").strip()
        if not milestone_id:
            raise SnapshotError("milestone id is required")
        if milestone_id in milestone_ids:
            raise SnapshotError(f"duplicate milestone id: {milestone_id}")
        milestone_ids.add(milestone_id)
        expected_sha = str(raw.get("expected_sha") or "").strip()[:100]
        expected_digest = str(
            raw.get("expected_artifact_digest") or ""
        ).strip()[:200]
        candidate_valid = bool(
            _valid_source_sha(expected_sha)
            and _valid_artifact_digest(expected_digest)
        )

        checks = raw.get("checks_receipt")
        checks = checks if isinstance(checks, dict) else {}
        checks_state = _state(checks.get("state"))
        checks_sha = str(checks.get("sha") or "").strip()[:100]
        checks_digest = str(checks.get("artifact_digest") or "").strip()[:200]
        checks_observed_at = _parse_strict_timestamp(checks.get("observed_at"))
        checks_url = _safe_url(checks.get("evidence_url"))
        checks_valid = bool(
            candidate_valid
            and checks_state in SUCCESS_STATES
            and checks_sha == expected_sha
            and checks_digest == expected_digest
            and checks_observed_at is not None
            and checks_observed_at <= snapshot_observed_at
            and checks_url
        )

        normalized_evidence: list[JsonObject] = []
        evidence_timestamps: list[datetime] = []
        evidence_valid = True
        raw_evidence = raw.get("evidence", [])
        if not isinstance(raw_evidence, list) or not raw_evidence:
            evidence_valid = False
            raw_evidence = []
        for item in raw_evidence:
            if not isinstance(item, dict):
                evidence_valid = False
                continue
            url = _safe_url(item.get("url"))
            sha = str(item.get("sha") or "").strip()[:100]
            digest = str(item.get("artifact_digest") or "").strip()[:200]
            evidence_observed_at = _parse_strict_timestamp(item.get("observed_at"))
            if (
                not url
                or not candidate_valid
                or sha != expected_sha
                or digest != expected_digest
                or evidence_observed_at is None
                or evidence_observed_at > snapshot_observed_at
            ):
                evidence_valid = False
                continue
            evidence_timestamps.append(evidence_observed_at)
            normalized_evidence.append(
                {
                    "kind": str(item.get("kind") or "milestone")[:100],
                    "url": url,
                    "sha": sha,
                    "artifact_digest": digest,
                    "observed_at": _iso(evidence_observed_at),
                }
            )
        ready = bool(
            candidate_valid
            and checks_valid
            and raw.get("evidence_complete") is True
            and evidence_valid
            and normalized_evidence
            and len(normalized_evidence) == len(raw_evidence)
        )

        approval = raw.get("owner_approval")
        approval = approval if isinstance(approval, dict) else {}
        approval_url = _safe_url(approval.get("source_url"))
        approved_at = _parse_strict_timestamp(approval.get("approved_at"))
        approval_sha = str(approval.get("sha") or "").strip()[:100]
        approval_digest = str(
            approval.get("artifact_digest") or ""
        ).strip()[:200]
        receipt_timestamps = list(evidence_timestamps)
        if checks_observed_at is not None:
            receipt_timestamps.append(checks_observed_at)
        latest_receipt_at = max(receipt_timestamps) if receipt_timestamps else None
        approval_valid = bool(
            ready
            and approval.get("approved") is True
            and approval_url
            and approved_at is not None
            and approval_sha == expected_sha
            and approval_digest == expected_digest
            and latest_receipt_at is not None
            and approved_at >= latest_receipt_at
            and approved_at <= snapshot_observed_at
        )
        if ready and approval_valid:
            state = "milestone_complete"
        elif ready:
            state = "milestone_ready"
        else:
            state = "not_ready"
        if ready and raw.get("completion_requested") is True and not approval_valid:
            conditions.append(
                Condition(
                    "owner_decision_required",
                    "warning",
                    f"milestone:{milestone_id}",
                    "milestone completion requires explicit owner approval",
                    sha=expected_sha,
                    artifact_digest=expected_digest,
                    evidence_urls=_bounded_evidence_urls(
                        checks_url,
                        [item["url"] for item in normalized_evidence],
                        approval_url,
                    ),
                )
            )
        normalized = {
            "milestone_id": milestone_id,
            "state": state,
            "expected_sha": expected_sha,
            "expected_artifact_digest": expected_digest,
            "checks_state": checks_state,
            "checks_sha": checks_sha,
            "checks_artifact_digest": checks_digest,
            "checks_observed_at": (
                _iso(checks_observed_at) if checks_observed_at else None
            ),
            "checks_evidence_url": checks_url,
            "evidence_complete": ready,
            "evidence": normalized_evidence,
            "approval_source_url": approval_url,
            "approved_at": _iso(approved_at) if approved_at else None,
            "approval_sha": approval_sha,
            "approval_artifact_digest": approval_digest,
        }
        milestones.append(normalized)
        counts[state] = counts.get(state, 0) + 1
    return milestones, counts, conditions


def classify_snapshot(
    snapshot: JsonObject,
    config: ObserverConfig,
    *,
    now: datetime | None = None,
) -> ClassificationResult:
    now = now or _utc_now()
    if snapshot.get("schema_version") != 1:
        raise SnapshotError("unsupported snapshot schema")
    scope = snapshot.get("scope")
    if not isinstance(scope, dict):
        raise SnapshotError("snapshot scope is required")
    if str(scope.get("linear_team_id") or "") != config.linear_team_id:
        raise SnapshotError("snapshot Linear team is outside the MailSwarm allowlist")
    if str(scope.get("github_repository") or "").lower() != config.github_repository.lower():
        raise SnapshotError("snapshot GitHub repository is outside the MailSwarm allowlist")

    observed_at = _parse_timestamp(snapshot.get("observed_at"))
    if observed_at is None:
        return ClassificationResult(
            classification="status_unknown",
            source_state="invalid_timestamp",
            observed_at=None,
            active_work_count=0,
            issue_counts={},
            issues=[],
            milestone_counts={},
            milestones=[],
            conditions=[
                Condition(
                    "status_unknown",
                    "warning",
                    "observer-source",
                    "source snapshot timestamp is missing or invalid",
                )
            ],
        )
    age = now - observed_at
    if age > timedelta(minutes=config.source_max_age_minutes) or age < timedelta(minutes=-5):
        return ClassificationResult(
            classification="status_unknown",
            source_state="stale",
            observed_at=_iso(observed_at),
            active_work_count=0,
            issue_counts={},
            issues=[],
            milestone_counts={},
            milestones=[],
            conditions=[
                Condition(
                    "status_unknown",
                    "warning",
                    "observer-source",
                    "source snapshot is stale or from the future",
                )
            ],
        )

    raw_issues = snapshot.get("issues", [])
    if not isinstance(raw_issues, list):
        raise SnapshotError("snapshot issues must be a list")
    issues: list[JsonObject] = []
    issue_counts: dict[str, int] = {}
    conditions: list[Condition] = []
    issue_ids: set[str] = set()
    for raw in raw_issues:
        if not isinstance(raw, dict):
            raise SnapshotError("snapshot issue must be an object")
        issue, classification = _classify_issue(
            raw,
            now=now,
            stalled_minutes=config.stalled_minutes,
        )
        if issue["issue_id"] in issue_ids:
            raise SnapshotError(f"duplicate issue id: {issue['issue_id']}")
        issue_ids.add(issue["issue_id"])
        issues.append(issue)
        issue_counts[classification] = issue_counts.get(classification, 0) + 1
        condition = _condition_for_issue(issue, classification)
        if condition:
            conditions.append(condition)

    active = [issue for issue in issues if issue["status"] in ACTIVE_STATUSES]
    if len(active) > 3:
        conditions.append(
            Condition(
                "capacity_paused",
                "critical",
                "active-builders",
                f"{len(active)} active builder issues exceeds the limit of 3",
            )
        )
    components: dict[str, list[str]] = {}
    for issue in active:
        component = str(issue.get("component") or "")
        if component:
            components.setdefault(component, []).append(str(issue["issue_id"]))
    for component, issue_ids in sorted(components.items()):
        if len(issue_ids) > 1:
            conditions.append(
                Condition(
                    "capacity_paused",
                    "critical",
                    f"component:{component}",
                    "multiple active issues own the same component",
                )
            )
    migration_owners = [issue for issue in active if issue.get("migration_owner")]
    if len(migration_owners) > 1:
        conditions.append(
            Condition(
                "migration_conflict",
                "critical",
                "migration-owner",
                "multiple active issues claim migration ownership",
            )
        )

    provider_capacity = snapshot.get("provider_capacity", [])
    if isinstance(provider_capacity, list):
        for provider in provider_capacity:
            if not isinstance(provider, dict) or provider.get("expected") is False:
                continue
            state = _state(provider.get("state"))
            if state in {"exhausted", "full", "unavailable"}:
                conditions.append(
                    Condition(
                        "capacity_paused",
                        "warning",
                        f"provider:{str(provider.get('provider') or 'unknown')[:100]}",
                        "provider explicitly reports no available capacity",
                    )
                )

    actions = snapshot.get("github_actions_usage")
    if isinstance(actions, dict) and actions.get("expected", True) is not False:
        checked_at = _parse_timestamp(actions.get("checked_at"))
        try:
            used = float(actions.get("used"))
            limit = float(actions.get("limit"))
        except (TypeError, ValueError):
            used = limit = 0.0
        if checked_at is None or now - checked_at > timedelta(minutes=config.source_max_age_minutes):
            conditions.append(
                Condition(
                    "status_unknown",
                    "warning",
                    "github-actions-usage",
                    "GitHub Actions usage receipt is unavailable or stale",
                )
            )
        elif limit > 0 and used / limit * 100 >= config.actions_warning_percent:
            conditions.append(
                Condition(
                    "actions_usage_warning",
                    "warning",
                    "github-actions-usage",
                    "GitHub Actions usage crossed the configured warning threshold",
                )
            )

    loops = snapshot.get("loops", [])
    if isinstance(loops, list):
        for loop in loops:
            if not isinstance(loop, dict) or loop.get("expected") is not True:
                continue
            heartbeat = _parse_timestamp(loop.get("heartbeat_at"))
            if heartbeat is None or now - heartbeat > timedelta(minutes=config.loop_stale_minutes):
                conditions.append(
                    Condition(
                        "status_unknown",
                        "warning",
                        f"loop:{str(loop.get('name') or 'unknown')[:100]}",
                        "expected Loop heartbeat is unavailable or stale",
                    )
                )

    integration = snapshot.get("integration_test")
    if isinstance(integration, dict) and integration.get("expected") is True:
        state = _state(integration.get("state"))
        completed_at = _parse_timestamp(integration.get("completed_at"))
        if state in FAILURE_STATES:
            conditions.append(
                Condition(
                    "quality_failed",
                    "critical",
                    "external-integration-test",
                    "external integration test explicitly failed",
                )
            )
        elif (
            completed_at is None
            or now - completed_at > timedelta(minutes=config.integration_stale_minutes)
        ):
            conditions.append(
                Condition(
                    "status_unknown",
                    "warning",
                    "external-integration-test",
                    "external integration-test evidence is unavailable or stale",
                )
            )

    milestones, milestone_counts, milestone_conditions = _classify_milestones(
        snapshot.get("milestones"),
        snapshot_observed_at=observed_at,
    )
    conditions.extend(milestone_conditions)

    candidates = [issue["classification"] for issue in issues]
    candidates.extend(condition.classification for condition in conditions)
    classification = max(
        candidates or ["idle"],
        key=lambda value: CLASSIFICATION_PRIORITY.get(value, -1),
    )
    conditions.sort(
        key=lambda item: (
            -CLASSIFICATION_PRIORITY.get(item.classification, -1),
            item.subject,
            item.reason,
        )
    )
    return ClassificationResult(
        classification=classification,
        source_state="fresh",
        observed_at=_iso(observed_at),
        active_work_count=len(active),
        issue_counts=issue_counts,
        issues=issues,
        milestone_counts=milestone_counts,
        milestones=milestones,
        conditions=conditions,
    )


class ObserverStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.executescript(
                """
            CREATE TABLE IF NOT EXISTS observer_meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS issue_state (
              issue_id TEXT PRIMARY KEY,
              attempt INTEGER NOT NULL,
              provider_session_id TEXT,
              provider_state TEXT,
              last_activity_at TEXT,
              last_ci_at TEXT,
              last_ci_state TEXT,
              last_ci_sha TEXT,
              last_ci_digest TEXT,
              last_ci_evidence_url TEXT,
              last_cloud_test_at TEXT,
              last_cloud_test_state TEXT,
              last_cloud_test_sha TEXT,
              last_cloud_test_digest TEXT,
              last_cloud_test_evidence_url TEXT,
              last_loop_heartbeat_at TEXT,
              classification TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evidence (
              evidence_key TEXT PRIMARY KEY,
              issue_id TEXT NOT NULL,
              kind TEXT NOT NULL,
              url TEXT NOT NULL,
              sha TEXT,
              artifact_digest TEXT,
              observed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS owner_decisions (
              decision_key TEXT PRIMARY KEY,
              issue_id TEXT NOT NULL,
              source_url TEXT NOT NULL,
              decided_at TEXT NOT NULL,
              decision_text TEXT
            );
            CREATE TABLE IF NOT EXISTS milestone_state (
              milestone_id TEXT PRIMARY KEY,
              state TEXT NOT NULL,
              expected_sha TEXT,
              expected_artifact_digest TEXT,
              checks_state TEXT,
              checks_sha TEXT,
              checks_artifact_digest TEXT,
              checks_observed_at TEXT,
              checks_evidence_url TEXT,
              evidence_complete INTEGER NOT NULL,
              approval_source_url TEXT,
              approved_at TEXT,
              approval_sha TEXT,
              approval_artifact_digest TEXT,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS alert_history (
              fingerprint TEXT PRIMARY KEY,
              classification TEXT NOT NULL,
              subject TEXT NOT NULL,
              reason TEXT NOT NULL,
              attempt INTEGER NOT NULL DEFAULT 0,
              sha TEXT,
              artifact_digest TEXT,
              evidence_urls_json TEXT NOT NULL DEFAULT '[]',
              first_seen_at TEXT NOT NULL,
              last_seen_at TEXT NOT NULL,
              last_sent_at TEXT,
              send_count INTEGER NOT NULL DEFAULT 0,
              resolved_at TEXT,
              resolution_sent_at TEXT
            );
            CREATE TABLE IF NOT EXISTS milestone_update_history (
              fingerprint TEXT PRIMARY KEY,
              milestone_id TEXT NOT NULL,
              state TEXT NOT NULL,
              sha TEXT NOT NULL,
              artifact_digest TEXT NOT NULL,
              evidence_urls_json TEXT NOT NULL,
              first_seen_at TEXT NOT NULL,
              last_seen_at TEXT NOT NULL,
              delivery_attempts INTEGER NOT NULL DEFAULT 0,
              sent_at TEXT,
              superseded_at TEXT,
              superseded_by_state TEXT
            );
            CREATE TABLE IF NOT EXISTS observer_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              kind TEXT NOT NULL,
              recorded_at TEXT NOT NULL,
              payload_json TEXT NOT NULL
            );
            """
            )
            table_migrations = {
                "issue_state": {
                    "last_ci_digest": "TEXT",
                    "last_ci_evidence_url": "TEXT",
                    "last_cloud_test_evidence_url": "TEXT",
                },
                "milestone_state": {
                    "expected_artifact_digest": "TEXT",
                    "checks_sha": "TEXT",
                    "checks_artifact_digest": "TEXT",
                    "checks_observed_at": "TEXT",
                    "checks_evidence_url": "TEXT",
                    "approval_sha": "TEXT",
                    "approval_artifact_digest": "TEXT",
                },
                "alert_history": {
                    "attempt": "INTEGER NOT NULL DEFAULT 0",
                    "sha": "TEXT",
                    "artifact_digest": "TEXT",
                    "evidence_urls_json": "TEXT NOT NULL DEFAULT '[]'",
                    "resolution_sent_at": "TEXT",
                },
                "milestone_update_history": {
                    "superseded_at": "TEXT",
                    "superseded_by_state": "TEXT",
                },
            }
            for table, columns in table_migrations.items():
                existing = {
                    row[1]
                    for row in connection.execute(f"PRAGMA table_info({table})")
                }
                for name, declaration in columns.items():
                    if name not in existing:
                        connection.execute(
                            f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                        )
        except BaseException:
            connection.close()
            raise
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def persist_cycle(self, result: ClassificationResult, now: datetime) -> None:
        timestamp = _iso(now)
        with self._transaction() as connection:
            meta = {
                "heartbeat_at": timestamp,
                "last_success_at": timestamp if result.source_state == "fresh" else "",
                "classification": result.classification,
                "source_state": result.source_state,
                "active_work_count": str(result.active_work_count),
                "milestone_counts": json.dumps(result.milestone_counts, sort_keys=True),
            }
            for key, value in meta.items():
                if key == "last_success_at" and not value:
                    continue
                connection.execute(
                    """
                    INSERT INTO observer_meta(key, value, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                    """,
                    (key, value, timestamp),
                )
            if result.source_state == "fresh":
                connection.execute("DELETE FROM issue_state")
            for issue in result.issues:
                connection.execute(
                    """
                    INSERT INTO issue_state(
                      issue_id, attempt, provider_session_id, provider_state,
                      last_activity_at, last_ci_at, last_ci_state, last_ci_sha,
                      last_ci_digest, last_ci_evidence_url,
                      last_cloud_test_at, last_cloud_test_state,
                      last_cloud_test_sha, last_cloud_test_digest,
                      last_cloud_test_evidence_url,
                      last_loop_heartbeat_at, classification, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        issue["issue_id"],
                        issue["attempt"],
                        issue["provider_session_id"],
                        issue["provider_state"],
                        issue["last_activity_at"],
                        issue["last_ci_at"],
                        issue["last_ci_state"],
                        issue["last_ci_sha"],
                        issue["last_ci_digest"],
                        issue["last_ci_evidence_url"],
                        issue["last_cloud_test_at"],
                        issue["last_cloud_test_state"],
                        issue["last_cloud_test_sha"],
                        issue["last_cloud_test_digest"],
                        issue["last_cloud_test_evidence_url"],
                        issue["last_loop_heartbeat_at"],
                        issue["classification"],
                        timestamp,
                    ),
                )
                for evidence in issue["evidence"]:
                    key = _fingerprint(
                        issue["issue_id"],
                        evidence["kind"],
                        evidence["url"],
                        evidence["sha"],
                        evidence["artifact_digest"],
                    )
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO evidence(
                          evidence_key, issue_id, kind, url, sha,
                          artifact_digest, observed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            key,
                            issue["issue_id"],
                            evidence["kind"],
                            evidence["url"],
                            evidence["sha"],
                            evidence["artifact_digest"],
                            evidence["observed_at"],
                        ),
                    )
                for decision in issue["owner_decisions"]:
                    key = _fingerprint(
                        issue["issue_id"], decision["source_url"], decision["decided_at"]
                    )
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO owner_decisions(
                          decision_key, issue_id, source_url, decided_at, decision_text
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            key,
                            issue["issue_id"],
                            decision["source_url"],
                            decision["decided_at"],
                            decision["decision"],
                        ),
                    )
            if result.source_state == "fresh":
                connection.execute("DELETE FROM milestone_state")
            current_milestone_ids = {
                milestone["milestone_id"] for milestone in result.milestones
            }
            for milestone in result.milestones:
                if result.source_state == "fresh":
                    connection.execute(
                        """
                        UPDATE milestone_update_history
                        SET superseded_at = ?, superseded_by_state = ?
                        WHERE milestone_id = ? AND sent_at IS NULL
                          AND superseded_at IS NULL
                          AND NOT (
                            state = ? AND sha = ? AND artifact_digest = ?
                          )
                        """,
                        (
                            timestamp,
                            milestone["state"],
                            milestone["milestone_id"],
                            milestone["state"],
                            milestone["expected_sha"],
                            milestone["expected_artifact_digest"],
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO milestone_state(
                      milestone_id, state, expected_sha, expected_artifact_digest,
                      checks_state, checks_sha, checks_artifact_digest,
                      checks_observed_at, checks_evidence_url, evidence_complete,
                      approval_source_url, approved_at, approval_sha,
                      approval_artifact_digest, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        milestone["milestone_id"],
                        milestone["state"],
                        milestone["expected_sha"],
                        milestone["expected_artifact_digest"],
                        milestone["checks_state"],
                        milestone["checks_sha"],
                        milestone["checks_artifact_digest"],
                        milestone["checks_observed_at"],
                        milestone["checks_evidence_url"],
                        1 if milestone["evidence_complete"] else 0,
                        milestone["approval_source_url"],
                        milestone["approved_at"],
                        milestone["approval_sha"],
                        milestone["approval_artifact_digest"],
                        timestamp,
                    ),
                )
                for evidence in milestone["evidence"]:
                    subject = f"milestone:{milestone['milestone_id']}"
                    key = _fingerprint(
                        subject,
                        evidence["kind"],
                        evidence["url"],
                        evidence["sha"],
                        evidence["artifact_digest"],
                    )
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO evidence(
                          evidence_key, issue_id, kind, url, sha,
                          artifact_digest, observed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            key,
                            subject,
                            evidence["kind"],
                            evidence["url"],
                            evidence["sha"],
                            evidence["artifact_digest"],
                            evidence["observed_at"],
                        ),
                    )
                if milestone["state"] in {"milestone_ready", "milestone_complete"}:
                    update = MilestoneUpdate(
                        milestone_id=milestone["milestone_id"],
                        state=milestone["state"],
                        sha=milestone["expected_sha"],
                        artifact_digest=milestone["expected_artifact_digest"],
                        evidence_urls=_bounded_evidence_urls(
                            milestone["checks_evidence_url"],
                            [item["url"] for item in milestone["evidence"]],
                            milestone["approval_source_url"],
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO milestone_update_history(
                          fingerprint, milestone_id, state, sha, artifact_digest,
                          evidence_urls_json, first_seen_at, last_seen_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(fingerprint) DO UPDATE SET
                          last_seen_at=excluded.last_seen_at,
                          evidence_urls_json=excluded.evidence_urls_json,
                          superseded_at=NULL,
                          superseded_by_state=NULL
                        """,
                        (
                            update.fingerprint,
                            update.milestone_id,
                            update.state,
                            update.sha,
                            update.artifact_digest,
                            json.dumps(list(update.evidence_urls), sort_keys=True),
                            timestamp,
                            timestamp,
                        ),
                    )
            if result.source_state == "fresh":
                pending_ids = {
                    row["milestone_id"]
                    for row in connection.execute(
                        """
                        SELECT DISTINCT milestone_id
                        FROM milestone_update_history
                        WHERE sent_at IS NULL AND superseded_at IS NULL
                        """
                    ).fetchall()
                }
                for milestone_id in pending_ids - current_milestone_ids:
                    connection.execute(
                        """
                        UPDATE milestone_update_history
                        SET superseded_at = ?, superseded_by_state = 'absent'
                        WHERE milestone_id = ? AND sent_at IS NULL
                          AND superseded_at IS NULL
                        """,
                        (timestamp, milestone_id),
                    )
            event = {
                "classification": result.classification,
                "source_state": result.source_state,
                "active_work_count": result.active_work_count,
                "issue_counts": result.issue_counts,
                "milestone_counts": result.milestone_counts,
                "condition_count": len(result.conditions),
            }
            connection.execute(
                "INSERT INTO observer_events(kind, recorded_at, payload_json) VALUES (?, ?, ?)",
                ("observer_cycle", timestamp, json.dumps(event, sort_keys=True)),
            )

    def alert_due(
        self,
        condition: Condition,
        now: datetime,
        cooldown: timedelta,
    ) -> bool:
        timestamp = _iso(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT last_sent_at, resolved_at FROM alert_history WHERE fingerprint = ?",
                (condition.fingerprint,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO alert_history(
                      fingerprint, classification, subject, reason, attempt, sha,
                      artifact_digest, evidence_urls_json,
                      first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        condition.fingerprint,
                        condition.classification,
                        condition.subject,
                        condition.reason,
                        condition.attempt,
                        condition.sha,
                        condition.artifact_digest,
                        json.dumps(list(condition.evidence_urls), sort_keys=True),
                        timestamp,
                        timestamp,
                    ),
                )
                return True
            connection.execute(
                """
                UPDATE alert_history
                SET last_seen_at = ?, reason = ?, attempt = ?, sha = ?,
                    artifact_digest = ?, evidence_urls_json = ?,
                    resolved_at = NULL, resolution_sent_at = NULL
                WHERE fingerprint = ?
                """,
                (
                    timestamp,
                    condition.reason,
                    condition.attempt,
                    condition.sha,
                    condition.artifact_digest,
                    json.dumps(list(condition.evidence_urls), sort_keys=True),
                    condition.fingerprint,
                ),
            )
            if row["resolved_at"] is not None:
                return True
            last_sent = _parse_timestamp(row["last_sent_at"])
            return last_sent is None or now - last_sent >= cooldown

    def mark_alert_sent(self, condition: Condition, now: datetime) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE alert_history
                SET last_sent_at = ?, send_count = send_count + 1
                WHERE fingerprint = ?
                """,
                (_iso(now), condition.fingerprint),
            )

    def resolve_missing(self, current_fingerprints: set[str], now: datetime) -> None:
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT fingerprint FROM alert_history WHERE resolved_at IS NULL"
            ).fetchall()
            for row in rows:
                if row["fingerprint"] not in current_fingerprints:
                    connection.execute(
                        "UPDATE alert_history SET resolved_at = ? WHERE fingerprint = ?",
                        (_iso(now), row["fingerprint"]),
                    )

    def pending_resolutions(self) -> list[Condition]:
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT classification, subject, reason, attempt, sha,
                       artifact_digest, evidence_urls_json
                FROM alert_history
                WHERE resolved_at IS NOT NULL
                  AND resolution_sent_at IS NULL
                  AND last_sent_at IS NOT NULL
                ORDER BY resolved_at, fingerprint
                """
            ).fetchall()
        conditions: list[Condition] = []
        for row in rows:
            try:
                stored_urls = json.loads(row["evidence_urls_json"] or "[]")
            except (TypeError, ValueError):
                stored_urls = []
            conditions.append(
                Condition(
                    classification=row["classification"],
                    severity="info",
                    subject=row["subject"],
                    reason=row["reason"],
                    attempt=int(row["attempt"] or 0),
                    sha=str(row["sha"] or ""),
                    artifact_digest=str(row["artifact_digest"] or ""),
                    evidence_urls=_bounded_evidence_urls(stored_urls),
                )
            )
        return conditions

    def mark_resolution_sent(self, condition: Condition, now: datetime) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE alert_history
                SET resolution_sent_at = ?
                WHERE fingerprint = ? AND resolved_at IS NOT NULL
                """,
                (_iso(now), condition.fingerprint),
            )

    def pending_milestone_updates(
        self,
        *,
        max_delivery_attempts: int,
    ) -> list[MilestoneUpdate]:
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT history.milestone_id, history.state, history.sha,
                       history.artifact_digest, history.evidence_urls_json,
                       history.delivery_attempts
                FROM milestone_update_history AS history
                INNER JOIN milestone_state AS current
                  ON current.milestone_id = history.milestone_id
                 AND current.state = history.state
                 AND current.expected_sha = history.sha
                 AND current.expected_artifact_digest = history.artifact_digest
                WHERE history.sent_at IS NULL
                  AND history.superseded_at IS NULL
                  AND history.delivery_attempts < ?
                ORDER BY history.first_seen_at, history.fingerprint
                """,
                (max(1, max_delivery_attempts),),
            ).fetchall()
        updates: list[MilestoneUpdate] = []
        for row in rows:
            try:
                stored_urls = json.loads(row["evidence_urls_json"] or "[]")
            except (TypeError, ValueError):
                stored_urls = []
            updates.append(
                MilestoneUpdate(
                    milestone_id=row["milestone_id"],
                    state=row["state"],
                    sha=row["sha"],
                    artifact_digest=row["artifact_digest"],
                    evidence_urls=_bounded_evidence_urls(stored_urls),
                    delivery_attempts=int(row["delivery_attempts"] or 0),
                )
            )
        return updates

    def mark_milestone_update_attempted(
        self,
        update: MilestoneUpdate,
        now: datetime,
        *,
        delivered: bool,
        attempts_used: int,
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE milestone_update_history
                SET delivery_attempts = delivery_attempts + ?,
                    sent_at = CASE WHEN ? THEN ? ELSE sent_at END
                WHERE fingerprint = ? AND sent_at IS NULL
                """,
                (
                    max(1, attempts_used),
                    1 if delivered else 0,
                    _iso(now),
                    update.fingerprint,
                ),
            )

    def summary_snapshot(self) -> JsonObject | None:
        if not self.path.exists():
            return None
        with self._transaction() as connection:
            meta = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key, value FROM observer_meta"
                ).fetchall()
            }
            issue_counts = {
                row["classification"]: int(row["count"])
                for row in connection.execute(
                    """
                    SELECT classification, count(*) AS count
                    FROM issue_state GROUP BY classification
                    ORDER BY classification
                    """
                ).fetchall()
            }
            milestone_counts = {
                row["state"]: int(row["count"])
                for row in connection.execute(
                    """
                    SELECT state, count(*) AS count
                    FROM milestone_state GROUP BY state ORDER BY state
                    """
                ).fetchall()
            }
            evidence_count = int(
                connection.execute("SELECT count(*) FROM evidence").fetchone()[0]
            )
            evidence_urls = _bounded_evidence_urls([
                row["url"]
                for row in connection.execute(
                    "SELECT DISTINCT url FROM evidence ORDER BY rowid DESC LIMIT ?",
                    (MAX_EVIDENCE_LINKS,),
                ).fetchall()
            ])
        return {
            "heartbeat_at": meta.get("heartbeat_at"),
            "last_success_at": meta.get("last_success_at"),
            "classification": meta.get("classification", "unknown"),
            "source_state": meta.get("source_state", "unknown"),
            "active_work_count": int(meta.get("active_work_count") or 0),
            "issue_counts": issue_counts,
            "milestone_counts": milestone_counts,
            "evidence_count": evidence_count,
            "evidence_urls": evidence_urls,
        }

    def record_test_event(self, now: datetime) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO observer_events(kind, recorded_at, payload_json) VALUES (?, ?, ?)",
                (
                    "transport_test",
                    _iso(now),
                    json.dumps({"test_event": True}, sort_keys=True),
                ),
            )


def unknown_result(reason: str) -> ClassificationResult:
    condition = Condition(
        "status_unknown",
        "warning",
        "observer-source",
        reason[:300],
    )
    return ClassificationResult(
        classification="status_unknown",
        source_state="unavailable",
        observed_at=None,
        active_work_count=0,
        issue_counts={},
        issues=[],
        milestone_counts={},
        milestones=[],
        conditions=[condition],
    )


def _clean_message_field(value: Any, *, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


def _identity_fields(
    *,
    subject: str,
    attempt: int = 0,
    sha: str = "",
    artifact_digest: str = "",
    evidence_urls: tuple[str, ...] = (),
) -> str:
    fields = [f"subject={_clean_message_field(subject, limit=120)}"]
    if attempt > 0:
        fields.append(f"attempt={attempt}")
    if sha:
        fields.append(f"sha={_clean_message_field(sha, limit=100)}")
    if artifact_digest:
        fields.append(
            f"digest={_clean_message_field(artifact_digest, limit=200)}"
        )
    urls = _bounded_evidence_urls(evidence_urls)
    if urls:
        fields.append("evidence=" + ",".join(urls))
    return " | ".join(fields)


def format_condition_message(condition: Condition, *, resolution: bool) -> str:
    identity = _identity_fields(
        subject=condition.subject,
        attempt=condition.attempt,
        sha=condition.sha,
        artifact_digest=condition.artifact_digest,
        evidence_urls=condition.evidence_urls,
    )
    reason = _clean_message_field(condition.reason)
    if resolution:
        return (
            "MailSwarm Observer resolved: "
            f"{condition.classification.replace('_', ' ')} | {identity} | "
            f"reason={reason} | next_action=confirm the cited receipt is current. "
            "This is a resolution alert only; no agent or production action was taken."
        )
    return (
        "MailSwarm Observer: "
        f"{condition.classification.replace('_', ' ')} | {identity} | "
        f"reason={reason} | next_action=inspect the cited evidence and update the source receipt. "
        "This is an alert only; no agent or production action was taken."
    )


def format_milestone_update(update: MilestoneUpdate) -> str:
    identity = _identity_fields(
        subject=update.subject,
        sha=update.sha,
        artifact_digest=update.artifact_digest,
        evidence_urls=update.evidence_urls,
    )
    next_action = (
        "record candidate-bound owner approval before completion"
        if update.state == "milestone_ready"
        else "retain the approved candidate evidence"
    )
    return (
        "MailSwarm milestone update: "
        f"state={update.state} | {identity} | next_action={next_action}. "
        "This is a transition update only; no incident, agent, approval, or production action was created."
    )


def format_deterministic_summary(label: str, summary: JsonObject) -> str:
    evidence_urls = _bounded_evidence_urls(summary.get("evidence_urls", ()))
    fields = [
        f"classification={_clean_message_field(summary.get('classification'))}",
        f"source_state={_clean_message_field(summary.get('source_state'))}",
        f"active_work_count={int(summary.get('active_work_count') or 0)}",
        "issue_counts=" + json.dumps(summary.get("issue_counts") or {}, sort_keys=True),
        "milestone_counts="
        + json.dumps(summary.get("milestone_counts") or {}, sort_keys=True),
        f"evidence_count={int(summary.get('evidence_count') or 0)}",
    ]
    if evidence_urls:
        fields.append("evidence=" + ",".join(evidence_urls))
    return (
        f"[MailSwarm {label} Summary] deterministic=true | model_used=false | "
        + " | ".join(fields)
        + ". This explicit summary reads persisted evidence only and takes no work or production action."
    )


class MailSwarmObserver:
    def __init__(
        self,
        config: ObserverConfig,
        *,
        reader: SnapshotReader | None = None,
        alert_sender: AlertSender | None = None,
        client: httpx.AsyncClient | None = None,
        store: ObserverStore | None = None,
        now: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.config = config
        self._reader = reader
        self._alert_sender = alert_sender
        self._client = client
        self._owns_client = client is None
        self._store = store
        self._now = now
        self._sleep = sleep
        self._status: JsonObject = {
            "enabled": config.enabled,
            "configured": not config.missing_requirements(),
            "shadow_mode": config.shadow_mode,
            "alerts_enabled": config.alerts_enabled,
            "summary_mode": "deterministic",
            "summary_enabled": config.summary_enabled,
            "summary_shadow_mode": config.summary_shadow_mode,
            "heartbeat_max_age_seconds": config.interval_seconds * 2 + 60,
            "last_outcome": "not_started",
            "last_checked_at": None,
            "last_success_at": None,
            "heartbeat_at": None,
            "classification": "idle",
            "source_state": "not_started",
            "active_work_count": 0,
            "issue_counts": {},
            "milestone_counts": {},
            "condition_count": 0,
            "cycles_total": 0,
            "source_errors_total": 0,
            "persistence_errors_total": 0,
            "alerts_sent_total": 0,
            "milestone_updates_sent_total": 0,
        }

    def _database(self) -> ObserverStore:
        if self._store is None:
            self._store = ObserverStore(self.config.database_path)
        return self._store

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.config.timeout_seconds, connect=5.0),
                follow_redirects=False,
            )
        return self._client

    async def _read_snapshot(self) -> JsonObject:
        if self._reader is not None:
            return await self._reader()
        headers = {"Accept": "application/json"}
        if self.config.source_bearer_token:
            headers["Authorization"] = f"Bearer {self.config.source_bearer_token}"
        response = await self._http().get(self.config.source_url, headers=headers)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise SnapshotError("snapshot response must be an object")
        return payload

    async def _read_with_retries(self) -> JsonObject:
        last_error: Exception | None = None
        for attempt in range(self.config.max_attempts):
            try:
                return await self._read_snapshot()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                last_error = error
                if attempt + 1 < self.config.max_attempts:
                    await self._sleep(min(2 ** attempt, 5))
        raise SnapshotError(str(last_error or "snapshot read failed"))

    async def _send_telegram(self, message: str) -> bool:
        if not self.config.telegram_bot_token or not self.config.telegram_chat_id:
            return False
        body = urlencode(
            {"chat_id": self.config.telegram_chat_id, "text": message}
        ).encode("utf-8")
        response = await self._http().post(
            f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage",
            content=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code >= 400:
            return False
        try:
            payload = response.json()
        except ValueError:
            return False
        return isinstance(payload, dict) and payload.get("ok") is True

    def _redact_known_secrets(self, message: str) -> str:
        for secret in sorted(
            {
                self.config.source_bearer_token,
                self.config.telegram_bot_token,
                self.config.telegram_chat_id,
            },
            key=len,
            reverse=True,
        ):
            if secret:
                message = message.replace(secret, "[REDACTED]")
        return message

    async def _deliver_with_attempts(
        self,
        message: str,
        *,
        max_attempts: int | None = None,
    ) -> tuple[bool, int]:
        sender = self._alert_sender or self._send_telegram
        message = self._redact_known_secrets(message)
        attempt_limit = self.config.max_attempts
        if max_attempts is not None:
            attempt_limit = max(1, min(attempt_limit, max_attempts))
        attempts_used = 0
        for attempt in range(attempt_limit):
            attempts_used += 1
            try:
                if await sender(message):
                    return True, attempts_used
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            if attempt + 1 < attempt_limit:
                await self._sleep(min(2 ** attempt, 5))
        return False, attempts_used

    async def _deliver(self, message: str) -> bool:
        delivered, _attempts_used = await self._deliver_with_attempts(message)
        return delivered

    async def check_once(self, snapshot: JsonObject | None = None) -> str:
        checked_at = self._now()
        self._status["last_checked_at"] = _iso(checked_at)
        self._status["heartbeat_at"] = _iso(checked_at)
        self._status["cycles_total"] += 1
        if not self.config.enabled:
            self._status["last_outcome"] = "disabled"
            self._status["source_state"] = "disabled"
            return "disabled"
        missing = self.config.missing_requirements()
        if missing:
            self._status["configured"] = False
            self._status["last_outcome"] = "misconfigured"
            self._status["source_state"] = "misconfigured"
            print(
                "[mailswarm-observer] "
                + json.dumps(
                    {"event": "configuration_invalid", "missing": missing},
                    sort_keys=True,
                ),
                flush=True,
            )
            return "misconfigured"

        try:
            source = snapshot if snapshot is not None else await self._read_with_retries()
            result = classify_snapshot(source, self.config, now=checked_at)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._status["source_errors_total"] += 1
            result = unknown_result("observer source could not be read or validated")

        try:
            await asyncio.to_thread(
                self._database().persist_cycle,
                result,
                checked_at,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._status["persistence_errors_total"] += 1
            result = unknown_result("observer persistence could not be updated")
            self._status.update(
                {
                    "last_outcome": result.classification,
                    "classification": result.classification,
                    "source_state": result.source_state,
                    "active_work_count": 0,
                    "issue_counts": {},
                    "milestone_counts": {},
                    "condition_count": len(result.conditions),
                }
            )
            print(
                "[mailswarm-observer] "
                + json.dumps(
                    {
                        "event": "observer_persistence_unavailable",
                        "classification": result.classification,
                        "source_state": result.source_state,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            return result.classification
        current_fingerprints = {
            condition.fingerprint
            for condition in result.conditions
            if condition.classification in ALERTABLE_CLASSIFICATIONS
        }
        if result.source_state == "fresh":
            await asyncio.to_thread(
                self._database().resolve_missing,
                current_fingerprints,
                checked_at,
            )
        if self.config.alerts_enabled and not self.config.shadow_mode:
            cooldown = timedelta(minutes=self.config.alert_cooldown_minutes)
            due_conditions: list[Condition] = []
            for condition in result.conditions:
                if condition.classification not in ALERTABLE_CLASSIFICATIONS:
                    continue
                due = await asyncio.to_thread(
                    self._database().alert_due,
                    condition,
                    checked_at,
                    cooldown,
                )
                if due:
                    due_conditions.append(condition)
            for condition in due_conditions:
                delivered = await self._deliver(
                    format_condition_message(condition, resolution=False)
                )
                if delivered:
                    await asyncio.to_thread(
                        self._database().mark_alert_sent,
                        condition,
                        checked_at,
                    )
                    self._status["alerts_sent_total"] += 1
            resolutions = await asyncio.to_thread(
                self._database().pending_resolutions
            )
            for condition in resolutions:
                delivered = await self._deliver(
                    format_condition_message(condition, resolution=True)
                )
                if delivered:
                    await asyncio.to_thread(
                        self._database().mark_resolution_sent,
                        condition,
                        checked_at,
                    )
                    self._status["alerts_sent_total"] += 1
            if result.source_state == "fresh":
                max_transition_attempts = self.config.max_attempts * 2
                milestone_updates = await asyncio.to_thread(
                    self._database().pending_milestone_updates,
                    max_delivery_attempts=max_transition_attempts,
                )
                for update in milestone_updates:
                    remaining_attempts = (
                        max_transition_attempts - update.delivery_attempts
                    )
                    delivered, attempts_used = await self._deliver_with_attempts(
                        format_milestone_update(update),
                        max_attempts=remaining_attempts,
                    )
                    await asyncio.to_thread(
                        self._database().mark_milestone_update_attempted,
                        update,
                        checked_at,
                        delivered=delivered,
                        attempts_used=attempts_used,
                    )
                    if delivered:
                        self._status["milestone_updates_sent_total"] += 1

        self._status.update(
            {
                "last_outcome": result.classification,
                "classification": result.classification,
                "source_state": result.source_state,
                "active_work_count": result.active_work_count,
                "issue_counts": dict(result.issue_counts),
                "milestone_counts": dict(result.milestone_counts),
                "condition_count": len(result.conditions),
            }
        )
        if result.source_state == "fresh":
            self._status["last_success_at"] = _iso(checked_at)
        print(
            "[mailswarm-observer] "
            + json.dumps(
                {
                    "event": "observer_cycle",
                    "classification": result.classification,
                    "source_state": result.source_state,
                    "active_work_count": result.active_work_count,
                    "condition_count": len(result.conditions),
                    "milestone_counts": result.milestone_counts,
                    "shadow_mode": self.config.shadow_mode,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return result.classification

    async def send_test_alert(self) -> bool:
        delivered = await self._deliver(
            "[TEST] MailSwarm Observer alert transport is reachable. "
            "test_event=true; no work state or production state was changed."
        )
        if delivered and self.config.enabled and not self.config.missing_requirements():
            await asyncio.to_thread(self._database().record_test_event, self._now())
        return delivered

    async def send_summary(self, label: str = "Morning") -> str:
        label = "Evening" if label.strip().lower() == "evening" else "Morning"
        if not self.config.summary_enabled:
            print(
                "[mailswarm-observer] "
                + json.dumps(
                    {
                        "event": "summary_disabled",
                        "label": label.lower(),
                        "model_used": False,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            return "disabled"
        missing = self.config.summary_missing_requirements()
        if missing:
            print(
                "[mailswarm-observer] "
                + json.dumps(
                    {
                        "event": "summary_misconfigured",
                        "label": label.lower(),
                        "missing": missing,
                        "model_used": False,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            return "misconfigured"
        try:
            summary = await asyncio.to_thread(self._database().summary_snapshot)
        except Exception:
            summary = None
        if summary is None:
            print(
                "[mailswarm-observer] "
                + json.dumps(
                    {
                        "event": "summary_unavailable",
                        "label": label.lower(),
                        "model_used": False,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            return "unavailable"
        message = format_deterministic_summary(label, summary)
        if self.config.summary_shadow_mode or self.config.shadow_mode:
            print(self._redact_known_secrets(message), flush=True)
            return "shadow"
        return "sent" if await self._deliver(message) else "delivery_failed"

    def public_status(self) -> JsonObject:
        return dict(self._status)

    def metrics(self) -> dict[str, float]:
        return {
            "enabled": 1.0 if self.config.enabled else 0.0,
            "configured": 1.0 if self._status["configured"] else 0.0,
            "active_work_count": float(self._status["active_work_count"]),
            "condition_count": float(self._status["condition_count"]),
            "cycles_total": float(self._status["cycles_total"]),
            "source_errors_total": float(self._status["source_errors_total"]),
            "persistence_errors_total": float(
                self._status["persistence_errors_total"]
            ),
            "alerts_sent_total": float(self._status["alerts_sent_total"]),
            "milestone_updates_sent_total": float(
                self._status["milestone_updates_sent_total"]
            ),
        }

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def run_forever(self) -> None:
        try:
            while True:
                await self.check_once()
                await self._sleep(self.config.interval_seconds)
        finally:
            await self.close()


def render_prometheus(status: JsonObject, *, task_running: bool) -> str:
    milestone_counts = status.get("milestone_counts")
    milestone_counts = milestone_counts if isinstance(milestone_counts, dict) else {}
    metrics = {
        "mailswarm_observer_up": (
            1
            if status.get("enabled") is not True
            or (
                status.get("configured") is True
                and status.get("source_state") == "fresh"
                and task_running
            )
            else 0
        ),
        "mailswarm_observer_enabled": 1 if status.get("enabled") is True else 0,
        "mailswarm_observer_active_work_count": int(
            status.get("active_work_count") or 0
        ),
        "mailswarm_observer_conditions": int(status.get("condition_count") or 0),
        "mailswarm_observer_cycles_total": int(status.get("cycles_total") or 0),
        "mailswarm_observer_source_errors_total": int(
            status.get("source_errors_total") or 0
        ),
        "mailswarm_observer_persistence_errors_total": int(
            status.get("persistence_errors_total") or 0
        ),
        "mailswarm_observer_alerts_sent_total": int(
            status.get("alerts_sent_total") or 0
        ),
        "mailswarm_observer_milestone_updates_sent_total": int(
            status.get("milestone_updates_sent_total") or 0
        ),
        "mailswarm_observer_milestones_ready": int(
            milestone_counts.get("milestone_ready") or 0
        ),
        "mailswarm_observer_milestones_complete": int(
            milestone_counts.get("milestone_complete") or 0
        ),
    }
    return "".join(f"{name} {value}\n" for name, value in metrics.items())


async def _main_async(mode: str) -> int:
    observer = MailSwarmObserver(ObserverConfig.from_env())
    try:
        if mode == "transport-test":
            return 0 if await observer.send_test_alert() else 1
        if mode in {"summary", "morning-summary", "evening-summary"}:
            label = "Evening" if mode == "evening-summary" else "Morning"
            outcome = await observer.send_summary(label)
            return 0 if outcome in {"disabled", "shadow", "sent"} else 1
        return 0 if await observer.check_once() in {"disabled", "idle", "active", "ready", "in_review", "done"} else 1
    finally:
        await observer.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=(
            "check",
            "transport-test",
            "summary",
            "morning-summary",
            "evening-summary",
        ),
        default="check",
    )
    args = parser.parse_args()
    return asyncio.run(_main_async(args.mode))


if __name__ == "__main__":
    sys.exit(main())
