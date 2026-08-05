#!/usr/bin/env python3
"""External health monitor and bounded recovery for Hermes production."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class ProbeResult:
    status_code: int | None
    payload: dict[str, object]
    error_type: str | None = None


@dataclass(frozen=True)
class Assessment:
    healthy: bool
    reason: str
    action: str = "none"


def probe_health(url: str, attempts: int = 3, timeout: int = 30) -> ProbeResult:
    last = ProbeResult(None, {}, "unreachable")
    for attempt in range(attempts):
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=timeout) as response:
                try:
                    payload = json.load(response)
                except json.JSONDecodeError:
                    return ProbeResult(response.status, {}, "invalid_json")
                if not isinstance(payload, dict):
                    payload = {}
                return ProbeResult(response.status, payload)
        except HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            last = ProbeResult(exc.code, payload, "http_error")
        except (URLError, OSError, TimeoutError, json.JSONDecodeError):
            last = ProbeResult(None, {}, "unreachable")
        if attempt + 1 < attempts:
            time.sleep(5)
    return last


def assess_health(probe: ProbeResult) -> Assessment:
    payload = probe.payload
    if probe.status_code == 200 and payload.get("status") == "ok":
        return Assessment(True, "Hermes is healthy")
    if probe.status_code is None or (probe.status_code >= 500 and not payload):
        return Assessment(False, "Hermes is unreachable", "redeploy")
    if not payload:
        return Assessment(False, "Hermes returned an invalid health response")
    if payload.get("configured") is False:
        return Assessment(False, "Hermes is missing required configuration")

    gateway = str(payload.get("gateway") or "unknown")
    if gateway != "running":
        return Assessment(False, f"Hermes gateway is {gateway}", "redeploy")

    watchdog = payload.get("coo_watchdog")
    if isinstance(watchdog, dict) and watchdog.get("enabled") is True:
        if watchdog.get("configured") is not True:
            return Assessment(False, "COO watchdog is misconfigured")
        if watchdog.get("task_running") is not True:
            return Assessment(False, "COO watchdog task stopped", "redeploy")

    scheduled = payload.get("scheduled_jobs")
    if isinstance(scheduled, dict) and scheduled.get("healthy") is False:
        kinds = scheduled.get("failure_kinds")
        kinds = kinds if isinstance(kinds, dict) else {}
        def count(name: str) -> int:
            try:
                return int(kinds.get(name) or 0)
            except (TypeError, ValueError):
                return 0

        other = count("other")
        provider = count("provider_auth")
        unavailable = count("model_unavailable")
        if other:
            return Assessment(False, "A scheduled Hermes task failed")
        if provider or unavailable:
            fallback = payload.get("fallback")
            ready = isinstance(fallback, dict) and fallback.get("ready") is True
            if not ready:
                return Assessment(
                    False,
                    "Main model failed and the capped DeepSeek fallback is not ready",
                )
            reason = (
                "Codex authentication failed; capped DeepSeek fallback is ready"
                if provider
                else "Main model is unavailable; capped DeepSeek fallback is ready"
            )
            return Assessment(False, reason, "fallback")
        return Assessment(False, "A scheduled Hermes task failed")
    return Assessment(False, "Hermes reported a degraded state")


def consecutive_failed_scheduled_runs(
    repository: str,
    token: str,
    current_run_id: str,
) -> int | None:
    if not repository or not token:
        return None
    endpoint = (
        f"https://api.github.com/repos/{repository}/actions/workflows/monitor.yml/runs"
        "?event=schedule&status=completed&per_page=5"
    )
    request = Request(
        endpoint,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except (HTTPError, URLError, OSError, json.JSONDecodeError):
        return None
    runs = payload.get("workflow_runs", []) if isinstance(payload, dict) else []
    failures = 0
    for run in runs:
        if not isinstance(run, dict) or str(run.get("id")) == current_run_id:
            continue
        conclusion = str(run.get("conclusion") or "")
        if conclusion == "success":
            break
        if conclusion in {"failure", "cancelled", "timed_out", "action_required", "stale"}:
            failures += 1
    return failures


def send_telegram(message: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram alert credentials are not configured", file=sys.stderr)
        return False
    body = urlencode({"chat_id": chat_id, "text": message}).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.load(response)
        return bool(isinstance(payload, dict) and payload.get("ok") is True)
    except (HTTPError, URLError, OSError, json.JSONDecodeError):
        print("Telegram alert delivery failed", file=sys.stderr)
        return False


def redeploy_railway() -> bool:
    token = os.environ.get("RAILWAY_TOKEN", "").strip()
    project = os.environ.get("RAILWAY_PROJECT_ID", "").strip()
    environment = os.environ.get("RAILWAY_ENVIRONMENT", "production").strip()
    service = os.environ.get("RAILWAY_SERVICE_ID", "").strip()
    if not token or not project or not service:
        print("Railway recovery credentials are not configured", file=sys.stderr)
        return False
    command = [
        "npx", "--yes", "@railway/cli@5.26.0", "redeploy",
        "--project", project,
        "--environment", environment,
        "--service", service,
        "--yes",
    ]
    try:
        completed = subprocess.run(
            command,
            env={**os.environ, "RAILWAY_TOKEN": token},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def wait_for_health(
    health_url: str,
    probe: Callable[[str, int, int], ProbeResult] = probe_health,
) -> bool:
    for _ in range(8):
        time.sleep(20)
        if assess_health(probe(health_url, 1, 20)).healthy:
            return True
    return False


def initial_message(assessment: Assessment) -> str:
    if assessment.action == "fallback":
        return (
            "⚠️ Hermes is unhealthy: " + assessment.reason + ". "
            "DeepSeek Flash may carry requests under the hard $5 monthly OpenRouter cap. "
            "No restart was attempted because a restart cannot renew Codex login."
        )
    if assessment.action == "redeploy":
        return (
            "⚠️ Hermes is unhealthy: " + assessment.reason + ". "
            "Starting the single allowed Railway recovery attempt for this incident."
        )
    return (
        "⚠️ Hermes is unhealthy: " + assessment.reason + ". "
        "There is no safe automatic repair for this failure."
    )


def run_check(
    health_url: str,
    probe: Callable[[str, int, int], ProbeResult] = probe_health,
    prior_failures: int | None = None,
    notify: Callable[[str], bool] = send_telegram,
    redeploy: Callable[[], bool] = redeploy_railway,
    wait: Callable[[str], bool] = wait_for_health,
) -> int:
    assessment = assess_health(probe(health_url, 3, 30))
    if prior_failures is None:
        prior_failures = consecutive_failed_scheduled_runs(
            os.environ.get("GITHUB_REPOSITORY", ""),
            os.environ.get("GITHUB_TOKEN", ""),
            os.environ.get("GITHUB_RUN_ID", ""),
        )
    if prior_failures is None:
        print("Could not verify monitor incident history; automatic recovery was skipped")
        return 1

    if assessment.healthy:
        if prior_failures:
            delivered = notify(
                "✅ Hermes recovered. GPT-5.6 Terra remains primary; "
                "DeepSeek Flash remains the $5-capped fallback."
            )
            return 0 if delivered else 1
        print("Hermes is healthy")
        return 0

    if prior_failures >= 2:
        print("Hermes remains unhealthy; alert and recovery were already attempted")
        return 1
    if prior_failures == 1:
        notify(
            "🛑 Hermes is still unhealthy after the bounded recovery window. "
            "Automatic action has stopped; manual intervention is required."
        )
        return 1

    delivered = notify(initial_message(assessment))
    if assessment.action != "redeploy":
        return 1
    attempted = redeploy()
    if not attempted:
        print("The single Railway recovery attempt could not be started")
        return 1
    recovered = wait(health_url)
    if not recovered:
        print("Hermes did not recover inside the bounded recovery window")
        return 1
    recovered_notice = notify(
        "✅ Hermes recovered after one Railway redeploy. GPT-5.6 Terra remains primary; "
        "DeepSeek Flash remains the $5-capped fallback."
    )
    return 0 if delivered and recovered_notice else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("check", "alert-test", "recovery-test"),
        default="check",
    )
    args = parser.parse_args()
    health_url = os.environ.get("HERMES_HEALTH_URL", "").strip()
    if not health_url:
        print("HERMES_HEALTH_URL is required", file=sys.stderr)
        return 2
    if args.mode == "alert-test":
        return 0 if send_telegram(
            "✅ Hermes monitoring test: Telegram alerts can reach the current bot."
        ) else 1
    if args.mode == "recovery-test":
        if not send_telegram(
            "🧪 Hermes recovery test: starting one intentional Railway redeploy."
        ):
            return 1
        if not redeploy_railway() or not wait_for_health(health_url):
            send_telegram("🛑 Hermes recovery test failed; manual intervention is required.")
            return 1
        return 0 if send_telegram("✅ Hermes recovery test passed.") else 1
    return run_check(health_url)


if __name__ == "__main__":
    sys.exit(main())
