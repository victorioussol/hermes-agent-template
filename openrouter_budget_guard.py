#!/usr/bin/env python3
"""Verify the Hermes OpenRouter key is capped and report safe usage metadata."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

KEY_ENDPOINT = "https://openrouter.ai/api/v1/key"
EXPECTED_LIMIT_USD = 5.0
EXPECTED_RESET = "monthly"


def evaluate_metadata(metadata: dict) -> tuple[dict, int]:
    try:
        limit = float(metadata.get("limit"))
    except (TypeError, ValueError):
        limit = None
    reset = str(metadata.get("limit_reset") or "").lower()
    usage_value = metadata.get("usage_monthly", metadata.get("usage", 0))
    remaining_value = metadata.get("limit_remaining")
    try:
        usage = round(float(usage_value or 0), 6)
    except (TypeError, ValueError):
        usage = 0.0
    try:
        remaining = round(float(remaining_value), 6)
    except (TypeError, ValueError):
        remaining = None

    if limit != EXPECTED_LIMIT_USD or reset != EXPECTED_RESET:
        status, exit_code = "unsafe_limit", 2
    elif usage >= 4.75:
        status, exit_code = "critical", 0
    elif usage >= 4.0:
        status, exit_code = "warning", 0
    elif usage >= 2.5:
        status, exit_code = "notice", 0
    else:
        status, exit_code = "ok", 0

    return {
        "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": status,
        "limit_usd": limit,
        "limit_reset": reset or None,
        "usage_monthly_usd": usage,
        "remaining_usd": remaining,
    }, exit_code


def fetch_metadata(key: str, timeout: int = 20) -> dict:
    request = Request(
        KEY_ENDPOINT,
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        raise ValueError("OpenRouter returned invalid key metadata")
    return data


def write_status(path: Path, status: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as handle:
        json.dump(status, handle, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.chmod(0o600)
    temporary.replace(path)


def main() -> int:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        print(json.dumps({"status": "not_configured"}, sort_keys=True))
        return 3
    try:
        metadata = fetch_metadata(key)
        status, exit_code = evaluate_metadata(metadata)
    except (HTTPError, URLError, OSError, ValueError) as exc:
        status = {
            "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "status": "unavailable",
            "error_type": type(exc).__name__,
        }
        exit_code = 1
    output_path = Path(
        os.environ.get(
            "HERMES_OPENROUTER_BUDGET_STATUS_FILE",
            "/data/.hermes/openrouter-budget.json",
        )
    )
    write_status(output_path, status)
    print(json.dumps(status, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
