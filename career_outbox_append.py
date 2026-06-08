#!/usr/bin/env python3
"""
career_outbox_append.py — Append items to the Career-Ops outbox from Hermes tools.

Usage from Hermes agent (execute_code or terminal):
  python3 /app/career_outbox_append.py --type status_update --company "CloudTalk" --role "Director of Product" --status Interview --evidence "Recruiter replied confirming interview"

  python3 /app/career_outbox_append.py --type new_opportunity --company "Acme" --role "VP Design" --url "https://..." --source linkedin-email --location remote-eu

  python3 /app/career_outbox_append.py --type interview_event --company "CloudTalk" --role "Director of Product" --interviewer "Jane Doe" --datetime "2026-06-15T14:00:00Z" --meeting-url "https://zoom.us/..."

  python3 /app/career_outbox_append.py --type jd_enrichment --company "Acme" --role "VP Design" --url "https://..." --jd-text "Full JD text here..." --is-live true
"""

import argparse
import json
import sys
import os
from pathlib import Path

# Add the server directory to the path so we can import server functions
sys.path.insert(0, '/app')

# We need to call _outbox_append from server.py, but importing server.py
# would start the app. Instead, replicate the logic here.
import secrets
from datetime import datetime, timezone

HERMES_HOME = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
OUTBOX_DIR = Path(HERMES_HOME) / "career-ops"
OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
OUTBOX_FILE = OUTBOX_DIR / "outbox.jsonl"
OUTBOX_MAX = 5000
OUTBOX_KEEP = 4000


def outbox_append(item: dict) -> str:
    item_id = secrets.token_hex(16)
    record = {
        "id": item_id,
        "emitted_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "item": item,
    }
    with open(OUTBOX_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    # Rotate if needed
    try:
        lines = OUTBOX_FILE.read_text(encoding="utf-8").splitlines()
        if len(lines) > OUTBOX_MAX:
            OUTBOX_FILE.write_text("\n".join(lines[-OUTBOX_KEEP:]) + "\n", encoding="utf-8")
    except Exception:
        pass
    return item_id


def main():
    parser = argparse.ArgumentParser(description="Append Career-Ops outbox item")
    parser.add_argument("--type", required=True, choices=["status_update", "new_opportunity", "interview_event", "jd_enrichment"])
    parser.add_argument("--company", required=True)
    parser.add_argument("--role", required=True)
    # status_update
    parser.add_argument("--status", help="New status for status_update")
    parser.add_argument("--evidence", default="", help="Short evidence snippet")
    # new_opportunity
    parser.add_argument("--url", help="Job posting URL")
    parser.add_argument("--source", default="hermes-scout", help="Source of the opportunity")
    parser.add_argument("--location", default="", help="Location/remote info")
    parser.add_argument("--comp", default=None, help="Compensation if shown")
    # interview_event
    parser.add_argument("--interviewer", default="", help="Interviewer name")
    parser.add_argument("--datetime", help="Interview datetime ISO 8601")
    parser.add_argument("--meeting-url", default="", help="Meeting URL")
    # jd_enrichment
    parser.add_argument("--jd-text", default="", help="Full JD text from Firecrawl")
    parser.add_argument("--is-live", default=None, type=lambda x: x.lower() == "true", help="Whether posting is still live")

    args = parser.parse_args()

    item = {"type": args.type}

    if args.type == "status_update":
        item["company"] = args.company
        item["role"] = args.role
        item["new_status"] = args.status or "Unknown"
        item["evidence_snippet"] = args.evidence[:500] if args.evidence else ""
        item["received_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    elif args.type == "new_opportunity":
        item["company"] = args.company
        item["role"] = args.role
        item["url"] = args.url or ""
        item["source"] = args.source
        item["location"] = args.location
        item["comp_if_shown"] = args.comp

    elif args.type == "interview_event":
        item["company"] = args.company
        item["role"] = args.role
        item["interviewer_name"] = args.interviewer
        item["datetime"] = args.datetime or ""
        item["meeting_url"] = args.meeting_url

    elif args.type == "jd_enrichment":
        item["company"] = args.company
        item["role"] = args.role
        item["url"] = args.url or ""
        item["jd_text"] = args.jd_text[:10000] if args.jd_text else ""  # cap at 10KB
        item["is_live"] = args.is_live

    item_id = outbox_append(item)
    print(json.dumps({"ok": True, "id": item_id, "type": args.type}))


if __name__ == "__main__":
    main()
