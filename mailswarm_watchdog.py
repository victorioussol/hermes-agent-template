#!/usr/bin/env python3
"""Independent, non-AI, alert-only watchdog for Hermes and MailSwarm."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def _truthy(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


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


@dataclass(frozen=True)
class Target:
    name: str
    url: str
    expected: bool
    kind: str
    max_age_minutes: int = 45


@dataclass(frozen=True)
class WatchdogConfig:
    targets: tuple[Target, ...]
    telegram_bot_token: str
    telegram_chat_id: str
    max_attempts: int = 2
    timeout_seconds: int = 10

    @classmethod
    def from_env(cls) -> "WatchdogConfig":
        hermes_url = os.environ.get("HERMES_HEALTH_URL", "").strip()
        observer_url = os.environ.get("MAILSWARM_OBSERVER_HEALTH_URL", "").strip()
        daily_url = os.environ.get("MAILSWARM_DAILY_OPERATIONS_HEALTH_URL", "").strip()
        quality_url = os.environ.get("MAILSWARM_QUALITY_CONTROLLER_HEALTH_URL", "").strip()
        integration_url = os.environ.get(
            "MAILSWARM_INTEGRATION_TEST_HEALTH_URL", ""
        ).strip()
        targets = (
            Target("Hermes", hermes_url, True, "hermes", 10),
            Target(
                "MailSwarm Observer",
                observer_url,
                bool(observer_url),
                "heartbeat",
                max(10, _env_int("MAILSWARM_WATCHDOG_OBSERVER_MAX_AGE_MINUTES", 45)),
            ),
            Target(
                "Daily Operations",
                daily_url,
                _truthy(os.environ.get("MAILSWARM_WATCHDOG_EXPECT_DAILY_OPERATIONS")),
                "heartbeat",
                max(30, _env_int("MAILSWARM_WATCHDOG_DAILY_MAX_AGE_MINUTES", 1560)),
            ),
            Target(
                "Quality Controller",
                quality_url,
                _truthy(os.environ.get("MAILSWARM_WATCHDOG_EXPECT_QUALITY_CONTROLLER")),
                "heartbeat",
                max(30, _env_int("MAILSWARM_WATCHDOG_QUALITY_MAX_AGE_MINUTES", 150)),
            ),
            Target(
                "External integration test",
                integration_url,
                _truthy(os.environ.get("MAILSWARM_WATCHDOG_EXPECT_INTEGRATION_TEST")),
                "integration",
                max(
                    60,
                    _env_int("MAILSWARM_WATCHDOG_INTEGRATION_MAX_AGE_MINUTES", 1560),
                ),
            ),
        )
        return cls(
            targets=targets,
            telegram_bot_token=os.environ.get(
                "MAILSWARM_WATCHDOG_TELEGRAM_BOT_TOKEN", ""
            ).strip(),
            telegram_chat_id=os.environ.get(
                "MAILSWARM_WATCHDOG_TELEGRAM_CHAT_ID", ""
            ).strip(),
            max_attempts=max(
                1, min(2, _env_int("MAILSWARM_WATCHDOG_MAX_ATTEMPTS", 2))
            ),
            timeout_seconds=max(
                5, min(30, _env_int("MAILSWARM_WATCHDOG_TIMEOUT_SECONDS", 10))
            ),
        )


@dataclass(frozen=True)
class ProbeResult:
    status_code: int | None
    payload: dict[str, Any]
    error_type: str | None = None


@dataclass(frozen=True)
class Assessment:
    name: str
    state: str
    reason: str

    @property
    def alert(self) -> bool:
        return self.state in {"unknown", "unhealthy"}


def probe_json(
    url: str,
    *,
    attempts: int = 2,
    timeout: int = 10,
    pause: Callable[[float], None] = time.sleep,
) -> ProbeResult:
    last = ProbeResult(None, {}, "unreachable")
    bounded_attempts = max(1, min(2, attempts))
    for attempt in range(bounded_attempts):
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=timeout) as response:
                try:
                    payload = json.load(response)
                except json.JSONDecodeError:
                    return ProbeResult(response.status, {}, "invalid_json")
                if not isinstance(payload, dict):
                    return ProbeResult(response.status, {}, "invalid_json")
                return ProbeResult(response.status, payload)
        except HTTPError as error:
            try:
                payload = json.loads(error.read().decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
            last = ProbeResult(
                error.code,
                payload if isinstance(payload, dict) else {},
                "http_error",
            )
        except (URLError, OSError, TimeoutError):
            last = ProbeResult(None, {}, "unreachable")
        if attempt + 1 < bounded_attempts:
            pause(min(2 ** attempt, 5))
    return last


def assess_target(
    target: Target,
    probe: ProbeResult,
    *,
    now: datetime | None = None,
) -> Assessment:
    now = now or datetime.now(timezone.utc)
    if not target.expected:
        return Assessment(target.name, "not_expected", "target is not active")
    if not target.url:
        if target.kind == "hermes":
            return Assessment(
                target.name,
                "unknown",
                "HERMES_HEALTH_URL is not configured",
            )
        return Assessment(target.name, "unknown", "expected target URL is not configured")
    if probe.status_code is None:
        return Assessment(target.name, "unknown", "target is unreachable")
    if not probe.payload:
        return Assessment(target.name, "unknown", "target returned invalid state")

    payload = probe.payload
    state = str(payload.get("status") or payload.get("state") or "").strip().lower()
    if target.kind == "hermes":
        if probe.status_code == 200 and state == "ok":
            return Assessment(target.name, "healthy", "Hermes health endpoint is live")
        if state in {"degraded", "failed", "unhealthy"}:
            return Assessment(target.name, "unhealthy", "Hermes explicitly reported degraded state")
        return Assessment(target.name, "unknown", "Hermes state could not be confirmed")

    if target.kind == "integration":
        completed_at = _parse_timestamp(
            payload.get("completed_at") or payload.get("heartbeat_at")
        )
        if state in {"error", "failed", "failure", "timed_out"}:
            return Assessment(
                target.name,
                "unhealthy",
                "integration test explicitly reported failure",
            )
        if completed_at is None:
            return Assessment(target.name, "unknown", "integration receipt timestamp is missing")
        if now - completed_at > timedelta(minutes=target.max_age_minutes):
            return Assessment(target.name, "unknown", "integration receipt is stale")
        if state in {"ok", "passed", "success", "succeeded"}:
            return Assessment(target.name, "healthy", "integration receipt is fresh")
        return Assessment(target.name, "unknown", "integration result is not explicit")

    if (
        probe.status_code >= 500
        or state in {"degraded", "failed", "unhealthy"}
        or payload.get("configured") is False
        or payload.get("task_running") is False
    ):
        return Assessment(target.name, "unhealthy", "target explicitly reported degraded state")
    if payload.get("enabled") is False:
        return Assessment(target.name, "unknown", "expected heartbeat target is disabled")
    heartbeat = _parse_timestamp(
        payload.get("heartbeat_at")
        or payload.get("updated_at")
        or payload.get("last_success_at")
    )
    if heartbeat is None:
        return Assessment(target.name, "unknown", "heartbeat timestamp is missing")
    if now - heartbeat > timedelta(minutes=target.max_age_minutes):
        return Assessment(target.name, "unknown", "heartbeat is stale")
    return Assessment(target.name, "healthy", "heartbeat is fresh")


def send_telegram(config: WatchdogConfig, message: str) -> bool:
    if not config.telegram_bot_token or not config.telegram_chat_id:
        print("MailSwarm watchdog alert credentials are not configured", file=sys.stderr)
        return False
    body = urlencode({"chat_id": config.telegram_chat_id, "text": message}).encode(
        "utf-8"
    )
    request = Request(
        f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=config.timeout_seconds) as response:
            payload = json.load(response)
    except (HTTPError, URLError, OSError, TimeoutError, json.JSONDecodeError):
        print("MailSwarm watchdog alert delivery failed", file=sys.stderr)
        return False
    return isinstance(payload, dict) and payload.get("ok") is True


def run_check(
    config: WatchdogConfig,
    *,
    probe: Callable[..., ProbeResult] = probe_json,
    notify: Callable[[WatchdogConfig, str], bool] = send_telegram,
    now: datetime | None = None,
) -> int:
    now = now or datetime.now(timezone.utc)
    assessments: list[Assessment] = []
    for target in config.targets:
        if not target.expected or not target.url:
            assessments.append(
                assess_target(target, ProbeResult(None, {}), now=now)
            )
            continue
        result = probe(
            target.url,
            attempts=config.max_attempts,
            timeout=config.timeout_seconds,
        )
        assessments.append(assess_target(target, result, now=now))
    alerts = [assessment for assessment in assessments if assessment.alert]
    print(
        "[mailswarm-watchdog] "
        + json.dumps(
            {
                "assessments": [assessment.__dict__ for assessment in assessments],
                "alert_count": len(alerts),
                "action": "alert_only",
            },
            sort_keys=True,
        )
    )
    if not alerts:
        return 0
    message = "MailSwarm watchdog alert: " + "; ".join(
        f"{assessment.name}: {assessment.reason}" for assessment in alerts
    )
    message += ". No repair, dispatch, merge, or production action was taken."
    return 1 if notify(config, message) else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("check", "transport-test"), default="check")
    args = parser.parse_args()
    config = WatchdogConfig.from_env()
    if args.mode == "transport-test":
        return 0 if send_telegram(
            config,
            "[TEST] MailSwarm watchdog alert transport is reachable. "
            "test_event=true; no repair or production action was taken.",
        ) else 1
    return run_check(config)


if __name__ == "__main__":
    sys.exit(main())
