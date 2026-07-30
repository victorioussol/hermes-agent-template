from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx


OPS_HUB_PROJECT_ID = "28653817-2657-4d8b-b3e8-c6ba38026e5c"
OPS_HUB_ENVIRONMENT_ID = "7fc32396-3715-4943-af9b-677196ed1912"
OPS_HUB_SERVICE_ID = "8d134c00-71f5-42cd-a13e-d4340bc4e193"
OPS_HUB_RUNTIME_KEY = "railway_guiri_ops_hub"
RAILWAY_GRAPHQL_URL = "https://backboard.railway.com/graphql/v2"


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
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


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class WatchdogConfig:
    enabled: bool
    supabase_url: str
    supabase_service_role_key: str
    railway_api_token: str
    hermes_home: Path
    interval_seconds: int = 900
    stale_minutes: int = 45
    no_contact_minutes: int = 1440
    recovery_cooldown_minutes: int = 60
    max_recovery_attempts: int = 2
    verify_timeout_seconds: int = 600
    verify_poll_seconds: int = 20

    @classmethod
    def from_env(cls) -> "WatchdogConfig":
        no_contact_minutes = max(
            60, _env_int("HERMES_COO_NO_CONTACT_MINUTES", 1440)
        )
        stale_minutes = min(
            max(5, _env_int("HERMES_COO_STALE_MINUTES", 45)),
            no_contact_minutes - 1,
        )
        return cls(
            enabled=_truthy(os.environ.get("HERMES_COO_WATCHDOG_ENABLED")),
            supabase_url=os.environ.get("SUPABASE_URL", "").rstrip("/"),
            supabase_service_role_key=os.environ.get("SUPABASE_SERVICE_ROLE_KEY", ""),
            railway_api_token=os.environ.get("RAILWAY_API_TOKEN", ""),
            hermes_home=Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))),
            interval_seconds=max(60, _env_int("HERMES_COO_WATCHDOG_INTERVAL_SECONDS", 900)),
            stale_minutes=stale_minutes,
            no_contact_minutes=no_contact_minutes,
            recovery_cooldown_minutes=max(
                5, _env_int("HERMES_COO_RECOVERY_COOLDOWN_MINUTES", 60)
            ),
            max_recovery_attempts=max(
                1, min(2, _env_int("HERMES_COO_MAX_RECOVERY_ATTEMPTS", 2))
            ),
            verify_timeout_seconds=max(
                30, _env_int("HERMES_COO_VERIFY_TIMEOUT_SECONDS", 600)
            ),
            verify_poll_seconds=max(
                5, _env_int("HERMES_COO_VERIFY_POLL_SECONDS", 20)
            ),
        )

    def missing_requirements(self) -> list[str]:
        if not self.enabled:
            return []
        required = {
            "SUPABASE_URL": self.supabase_url,
            "SUPABASE_SERVICE_ROLE_KEY": self.supabase_service_role_key,
            "RAILWAY_API_TOKEN": self.railway_api_token,
        }
        return [name for name, value in required.items() if not value]


class CooWatchdog:
    def __init__(
        self,
        config: WatchdogConfig,
        *,
        client: httpx.AsyncClient | None = None,
        now: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.config = config
        self._client = client
        self._owns_client = client is None
        self._now = now
        self._sleep = sleep
        self._log_path = config.hermes_home / "app-ops-action-inbox" / "coo-watchdog.jsonl"
        self._status: dict[str, Any] = {
            "enabled": config.enabled,
            "configured": not config.missing_requirements(),
            "last_outcome": "not_started",
            "last_checked_at": None,
        }

    def public_status(self) -> dict[str, Any]:
        return dict(self._status)

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=5.0),
                follow_redirects=False,
            )
        return self._client

    def _append_local(self, record: dict[str, Any]) -> None:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        material = dict(record)
        material.setdefault("recorded_at", _iso(self._now()))
        with self._log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(material, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _local_records(self) -> list[dict[str, Any]]:
        if not self._log_path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in self._log_path.read_text(encoding="utf-8").splitlines():
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
        return records

    async def _persist_receipt(self, receipt: dict[str, Any]) -> None:
        row = {
            "idempotency_key": receipt["idempotency_key"],
            "automation_key": None,
            "problem_fingerprint": f"hermes-coo-watchdog:{OPS_HUB_SERVICE_ID}",
            "stage": receipt["stage"],
            "status": receipt["status"],
            "actor": "hermes-coo-watchdog",
            "source_record_type": "railway_deployment",
            "source_record_id": receipt["source_record_id"],
            "evidence_links": receipt.get("evidence_links") or [],
            "payload": receipt.get("payload") or {},
        }
        response = await self._http().post(
            f"{self.config.supabase_url}/rest/v1/ops_learning_events"
            "?on_conflict=idempotency_key",
            headers={
                "apikey": self.config.supabase_service_role_key,
                "Authorization": f"Bearer {self.config.supabase_service_role_key}",
                "Content-Type": "application/json",
                "Prefer": "resolution=ignore-duplicates,return=minimal",
            },
            json=row,
        )
        response.raise_for_status()

    async def _sync_deferred_receipts(self) -> None:
        records = self._local_records()
        failed_keys = {
            record.get("receipt_idempotency_key")
            for record in records
            if record.get("action") == "remote_receipt_failed"
        }
        synced_keys = {
            record.get("receipt_idempotency_key")
            for record in records
            if record.get("action") == "remote_receipt_synced"
        }
        pending = failed_keys - synced_keys - {None}
        if not pending:
            return
        receipts = {
            record.get("idempotency_key"): record
            for record in records
            if record.get("idempotency_key") in pending
        }
        for receipt_key, receipt in receipts.items():
            try:
                await self._persist_receipt(receipt)
            except Exception:
                continue
            self._append_local({
                "action": "remote_receipt_synced",
                "status": "persisted",
                "receipt_idempotency_key": receipt_key,
            })

    async def _write_receipt(
        self,
        *,
        action: str,
        stage: str,
        status: str,
        source_record_id: str,
        payload: dict[str, Any],
        evidence_links: list[str] | None = None,
    ) -> None:
        recorded_at = self._now()
        idempotency_key = (
            f"hermes-coo-watchdog:{action}:{source_record_id}:{int(recorded_at.timestamp())}"
        )
        local = {
            "action": action,
            "stage": stage,
            "status": status,
            "source_record_id": source_record_id,
            "idempotency_key": idempotency_key,
            "payload": payload,
            "evidence_links": evidence_links or [],
            "recorded_at": _iso(recorded_at),
        }
        self._append_local(local)
        if not self.config.supabase_url or not self.config.supabase_service_role_key:
            return
        try:
            await self._persist_receipt(local)
        except Exception as error:
            self._append_local({
                "action": "remote_receipt_failed",
                "status": "deferred",
                "receipt_idempotency_key": idempotency_key,
                "error": str(error)[:500],
            })

    async def _read_heartbeat(self) -> dict[str, Any] | None:
        response = await self._http().get(
            f"{self.config.supabase_url}/rest/v1/app_ops_runtime_state",
            params={
                "key": f"eq.{OPS_HUB_RUNTIME_KEY}",
                "select": "key,value,updated_at",
                "limit": "1",
            },
            headers={
                "apikey": self.config.supabase_service_role_key,
                "Authorization": f"Bearer {self.config.supabase_service_role_key}",
            },
        )
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list) or not rows:
            return None
        row = rows[0] if isinstance(rows[0], dict) else {}
        raw_value = row.get("value")
        try:
            value = json.loads(raw_value) if isinstance(raw_value, str) else {}
        except json.JSONDecodeError:
            value = {}
        return {
            "updated_at": row.get("updated_at"),
            "completed_at": value.get("completed_at") if isinstance(value, dict) else None,
            "railway_deployment_id": (
                value.get("railway_deployment_id") if isinstance(value, dict) else None
            ),
        }

    @staticmethod
    def _heartbeat_time(heartbeat: dict[str, Any] | None) -> datetime | None:
        if not heartbeat:
            return None
        values = [
            _parse_timestamp(heartbeat.get("updated_at")),
            _parse_timestamp(heartbeat.get("completed_at")),
        ]
        available = [value for value in values if value is not None]
        return max(available) if available else None

    async def _latest_deployment(self) -> dict[str, Any] | None:
        query = """
          query HermesCooDeployments($input: DeploymentListInput!) {
            deployments(input: $input) {
              edges {
                node { id status createdAt canRedeploy }
              }
            }
          }
        """
        response = await self._http().post(
            RAILWAY_GRAPHQL_URL,
            headers={
                "Authorization": f"Bearer {self.config.railway_api_token}",
                "Content-Type": "application/json",
            },
            json={
                "query": query,
                "variables": {
                    "input": {
                        "projectId": OPS_HUB_PROJECT_ID,
                        "environmentId": OPS_HUB_ENVIRONMENT_ID,
                        "serviceId": OPS_HUB_SERVICE_ID,
                    }
                },
            },
        )
        response.raise_for_status()
        result = response.json()
        if result.get("errors"):
            raise RuntimeError(f"Railway deployment lookup failed: {result['errors']}")
        edges = result.get("data", {}).get("deployments", {}).get("edges", [])
        deployments = [
            edge.get("node")
            for edge in edges
            if isinstance(edge, dict) and isinstance(edge.get("node"), dict)
        ]
        deployments.sort(
            key=lambda item: _parse_timestamp(item.get("createdAt")) or datetime.min.replace(
                tzinfo=timezone.utc
            ),
            reverse=True,
        )
        return deployments[0] if deployments else None

    async def _redeploy(self, deployment_id: str) -> str:
        query = """
          mutation HermesRecoverCoo($id: String!) {
            deploymentRedeploy(id: $id) { id status }
          }
        """
        response = await self._http().post(
            RAILWAY_GRAPHQL_URL,
            headers={
                "Authorization": f"Bearer {self.config.railway_api_token}",
                "Content-Type": "application/json",
            },
            json={"query": query, "variables": {"id": deployment_id}},
        )
        response.raise_for_status()
        result = response.json()
        if result.get("errors"):
            raise RuntimeError(f"Railway redeploy failed: {result['errors']}")
        redeployed = result.get("data", {}).get("deploymentRedeploy") or {}
        return str(redeployed.get("id") or deployment_id)

    def _recent_records(self, action: str, since: datetime) -> list[dict[str, Any]]:
        return [
            record
            for record in self._local_records()
            if record.get("action") == action
            and (_parse_timestamp(record.get("recorded_at")) or datetime.min.replace(
                tzinfo=timezone.utc
            )) >= since
        ]

    def _recent_recovery_attempts(self, since: datetime) -> list[dict[str, Any]]:
        attempts = [
            record
            for record in self._local_records()
            if record.get("action") in {
                "recovery_attempt_started",
                "recovery_inspection_failed",
            }
            and (_parse_timestamp(record.get("recorded_at")) or datetime.min.replace(
                tzinfo=timezone.utc
            )) >= since
        ]
        return sorted(
            attempts,
            key=lambda record: _parse_timestamp(record.get("recorded_at"))
            or datetime.min.replace(tzinfo=timezone.utc),
        )

    def _heartbeat_missing_since(self) -> datetime:
        episode_records = [
            record
            for record in self._local_records()
            if record.get("action") in {
                "heartbeat_missing_observed",
                "heartbeat_missing_cleared",
            }
        ]
        if episode_records and episode_records[-1].get("action") == "heartbeat_missing_observed":
            observed_at = _parse_timestamp(episode_records[-1].get("recorded_at"))
            if observed_at:
                return observed_at
        observed_at = self._now()
        self._append_local({
            "action": "heartbeat_missing_observed",
            "status": "monitoring",
            "recorded_at": _iso(observed_at),
        })
        return observed_at

    def _clear_heartbeat_missing_episode(self) -> None:
        episode_records = [
            record
            for record in self._local_records()
            if record.get("action") in {
                "heartbeat_missing_observed",
                "heartbeat_missing_cleared",
            }
        ]
        if episode_records and episode_records[-1].get("action") == "heartbeat_missing_observed":
            self._append_local({
                "action": "heartbeat_missing_cleared",
                "status": "heartbeat_observed",
            })

    async def _notify_founder(
        self,
        *,
        outcome: str,
        heartbeat_time: datetime | None,
        latest_deployment: dict[str, Any] | None,
    ) -> bool:
        now = self._now()
        existing = self._recent_records("founder_alert_sent", now - timedelta(hours=24))
        if any(record.get("payload", {}).get("outcome") == outcome for record in existing):
            return True
        title = (
            "Guiri COO has not checked in for 24 hours"
            if outcome == "no_contact_24h"
            else "Hermes could not restore the Guiri COO"
        )
        recovery_exhausted = outcome == "recovery_exhausted"
        payload = {
            "source": "hermes-coo-watchdog",
            "event_type": "railway_ops_hub_stale",
            "severity": "p0",
            "fingerprint": f"hermes-coo-watchdog:{OPS_HUB_SERVICE_ID}:{outcome}",
            "title": title,
            "body": (
                "Hermes attempted the bounded recovery path. Founder attention is now required."
                if recovery_exhausted
                else "The COO has not produced a valid heartbeat for 24 hours. Founder attention is now required."
            ),
            "communication": {
                "status": "COO recovery needs attention",
                "summary": title,
                "victor_action": True,
                "owner": "Hermes",
                "action_underway": (
                    "Automatic recovery is paused after the safe attempt limit."
                    if recovery_exhausted
                    else "Hermes is preserving the incident evidence and waiting for operator review."
                ),
                "next_update": "After the COO service is restored",
                "evidence": [
                    f"Railway service {OPS_HUB_SERVICE_ID}",
                    f"Last COO contact: {_iso(heartbeat_time) if heartbeat_time else 'none recorded'}",
                ],
            },
            "payload": {
                "hermes_coo_recovery": {
                    "triaged_by": "hermes",
                    "outcome": outcome,
                    "target_service_id": OPS_HUB_SERVICE_ID,
                    "target_project_id": OPS_HUB_PROJECT_ID,
                    "latest_deployment_id": (
                        latest_deployment.get("id") if latest_deployment else None
                    ),
                }
            },
        }
        try:
            response = await self._http().post(
                f"{self.config.supabase_url}/functions/v1/app-ops-dispatcher",
                headers={
                    "apikey": self.config.supabase_service_role_key,
                    "Authorization": f"Bearer {self.config.supabase_service_role_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            accepted = response.json()
            legacy_dispatched = (
                accepted.get("dispatched")
                if isinstance(accepted, dict)
                else None
            )
            telegram_confirmed = (
                isinstance(accepted, dict)
                and (
                    accepted.get("telegram_delivery_confirmed") is True
                    or (
                        isinstance(legacy_dispatched, dict)
                        and legacy_dispatched.get("telegram") is True
                    )
                )
            )
            if (
                not isinstance(accepted, dict)
                or accepted.get("ok") is not True
                or not telegram_confirmed
            ):
                raise RuntimeError("dispatcher did not confirm founder delivery")
        except Exception as error:
            await self._write_receipt(
                action="founder_alert_failed",
                stage="run_failed",
                status="alert_delivery_failed",
                source_record_id=OPS_HUB_SERVICE_ID,
                payload={"outcome": outcome, "error": str(error)[:500]},
            )
            return False
        await self._write_receipt(
            action="founder_alert_sent",
            stage="verification_recorded",
            status="founder_notified",
            source_record_id=OPS_HUB_SERVICE_ID,
            payload={"outcome": outcome},
        )
        return True

    async def _wait_for_heartbeat(self, baseline: datetime | None) -> datetime | None:
        deadline = self._now() + timedelta(seconds=self.config.verify_timeout_seconds)
        while self._now() < deadline:
            await self._sleep(self.config.verify_poll_seconds)
            observed = self._heartbeat_time(await self._read_heartbeat())
            if observed and (baseline is None or observed > baseline):
                return observed
        return None

    async def _latest_execution_time(self, deployment_id: str) -> datetime | None:
        query = """
          query HermesCooDeploymentLogs($deploymentId: String!, $limit: Int) {
            deploymentLogs(deploymentId: $deploymentId, limit: $limit) {
              timestamp
            }
          }
        """
        response = await self._http().post(
            RAILWAY_GRAPHQL_URL,
            headers={
                "Authorization": f"Bearer {self.config.railway_api_token}",
                "Content-Type": "application/json",
            },
            json={
                "query": query,
                "variables": {"deploymentId": deployment_id, "limit": 10},
            },
        )
        response.raise_for_status()
        result = response.json()
        if result.get("errors"):
            raise RuntimeError(f"Railway deployment log lookup failed: {result['errors']}")
        timestamps = [
            _parse_timestamp(entry.get("timestamp"))
            for entry in result.get("data", {}).get("deploymentLogs", [])
            if isinstance(entry, dict)
        ]
        available = [value for value in timestamps if value is not None]
        return max(available) if available else None

    async def check_once(self) -> str:
        checked_at = self._now()
        heartbeat_time: datetime | None = None
        latest: dict[str, Any] | None = None
        recovery_needed = False
        attempt_recorded = False
        self._status["last_checked_at"] = _iso(checked_at)
        if not self.config.enabled:
            self._status["last_outcome"] = "disabled"
            return "disabled"
        missing = self.config.missing_requirements()
        if missing:
            self._status["configured"] = False
            self._status["last_outcome"] = "misconfigured"
            self._append_local({
                "action": "configuration_invalid",
                "status": "disabled",
                "missing": missing,
            })
            return "misconfigured"

        try:
            await self._sync_deferred_receipts()
            heartbeat = await self._read_heartbeat()
            heartbeat_time = self._heartbeat_time(heartbeat)
            if heartbeat_time is None:
                heartbeat_age = checked_at - self._heartbeat_missing_since()
            else:
                self._clear_heartbeat_missing_episode()
                heartbeat_age = checked_at - heartbeat_time
            if heartbeat_age >= timedelta(minutes=self.config.no_contact_minutes):
                delivered = await self._notify_founder(
                    outcome="no_contact_24h",
                    heartbeat_time=heartbeat_time,
                    latest_deployment=None,
                )
                outcome = (
                    "founder_attention_no_contact"
                    if delivered
                    else "founder_alert_pending_no_contact"
                )
                self._status["last_outcome"] = outcome
                return outcome

            if heartbeat_age <= timedelta(minutes=self.config.stale_minutes):
                outcome = "healthy" if heartbeat_time is not None else "heartbeat_missing_observation"
                self._status["last_outcome"] = outcome
                return outcome

            recovery_needed = True
            latest = await self._latest_deployment()
            latest_execution_time = (
                await self._latest_execution_time(str(latest["id"]))
                if latest and latest.get("id")
                else None
            )
            deployment_created_at = (
                _parse_timestamp(latest.get("createdAt"))
                if latest
                else None
            )
            deployment_status = str(latest.get("status") if latest else "").upper()
            active_or_recent_status = deployment_status in {
                "QUEUED",
                "INITIALIZING",
                "WAITING",
                "BUILDING",
                "DEPLOYING",
                "SUCCESS",
            }
            railway_activity_time = latest_execution_time
            if (
                railway_activity_time is None
                and deployment_created_at is not None
                and active_or_recent_status
            ):
                railway_activity_time = deployment_created_at
            execution_age = (
                checked_at - railway_activity_time
                if railway_activity_time is not None
                else timedelta.max
            )
            if execution_age <= timedelta(minutes=self.config.stale_minutes):
                outcome = "scheduler_recent_no_recovery"
                self._status["last_outcome"] = outcome
                return outcome

            since = checked_at - timedelta(hours=24)
            attempts = self._recent_recovery_attempts(since)
            if len(attempts) >= self.config.max_recovery_attempts:
                delivered = await self._notify_founder(
                    outcome="recovery_exhausted",
                    heartbeat_time=heartbeat_time,
                    latest_deployment=latest,
                )
                outcome = (
                    "founder_attention_recovery_exhausted"
                    if delivered
                    else "founder_alert_pending_recovery_exhausted"
                )
                self._status["last_outcome"] = outcome
                return outcome
            if attempts:
                last_attempt_at = _parse_timestamp(attempts[-1].get("recorded_at"))
                if (
                    last_attempt_at
                    and checked_at - last_attempt_at
                    < timedelta(minutes=self.config.recovery_cooldown_minutes)
                ):
                    outcome = "recovery_cooldown"
                    self._status["last_outcome"] = outcome
                    return outcome
            if not latest or not latest.get("id") or latest.get("canRedeploy") is not True:
                await self._write_receipt(
                    action="recovery_unavailable",
                    stage="run_failed",
                    status="no_safe_redeploy_target",
                    source_record_id=OPS_HUB_SERVICE_ID,
                    payload={
                        "latest_deployment_id": latest.get("id") if latest else None,
                        "can_redeploy": latest.get("canRedeploy") if latest else None,
                    },
                )
                delivered = await self._notify_founder(
                    outcome="recovery_exhausted",
                    heartbeat_time=heartbeat_time,
                    latest_deployment=latest,
                )
                outcome = (
                    "founder_attention_recovery_exhausted"
                    if delivered
                    else "founder_alert_pending_recovery_exhausted"
                )
                self._status["last_outcome"] = outcome
                return outcome

            deployment_id = str(latest["id"])
            await self._write_receipt(
                action="recovery_attempt_started",
                stage="attempt_started",
                status="redeploy_requested",
                source_record_id=deployment_id,
                payload={
                    "target_project_id": OPS_HUB_PROJECT_ID,
                    "target_environment_id": OPS_HUB_ENVIRONMENT_ID,
                    "target_service_id": OPS_HUB_SERVICE_ID,
                    "baseline_heartbeat": _iso(heartbeat_time) if heartbeat_time else None,
                },
            )
            attempt_recorded = True
            recovered_deployment_id = await self._redeploy(deployment_id)
            recovered_heartbeat = await self._wait_for_heartbeat(heartbeat_time)
            if recovered_heartbeat:
                await self._write_receipt(
                    action="recovery_verified",
                    stage="verification_recorded",
                    status="heartbeat_restored",
                    source_record_id=recovered_deployment_id,
                    payload={
                        "heartbeat_at": _iso(recovered_heartbeat),
                        "silent_recovery": True,
                    },
                )
                outcome = "recovered"
                self._status["last_outcome"] = outcome
                return outcome

            await self._write_receipt(
                action="recovery_attempt_failed",
                stage="attempt_finished",
                status="heartbeat_not_restored",
                source_record_id=recovered_deployment_id,
                payload={"verify_timeout_seconds": self.config.verify_timeout_seconds},
            )
            attempts_after = self._recent_recovery_attempts(since)
            if len(attempts_after) >= self.config.max_recovery_attempts:
                delivered = await self._notify_founder(
                    outcome="recovery_exhausted",
                    heartbeat_time=heartbeat_time,
                    latest_deployment=latest,
                )
                outcome = (
                    "founder_attention_recovery_exhausted"
                    if delivered
                    else "founder_alert_pending_recovery_exhausted"
                )
            else:
                outcome = "recovery_unverified"
            self._status["last_outcome"] = outcome
            return outcome
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._append_local({
                "action": "watchdog_check_failed",
                "status": "deferred",
                "error": str(error)[:500],
            })
            if recovery_needed:
                if not attempt_recorded:
                    await self._write_receipt(
                        action="recovery_inspection_failed",
                        stage="run_failed",
                        status="railway_inspection_failed",
                        source_record_id=OPS_HUB_SERVICE_ID,
                        payload={"error": str(error)[:500]},
                    )
                since = checked_at - timedelta(hours=24)
                attempts = self._recent_recovery_attempts(since)
                if len(attempts) >= self.config.max_recovery_attempts:
                    delivered = await self._notify_founder(
                        outcome="recovery_exhausted",
                        heartbeat_time=heartbeat_time,
                        latest_deployment=latest,
                    )
                    outcome = (
                        "founder_attention_recovery_exhausted"
                        if delivered
                        else "founder_alert_pending_recovery_exhausted"
                    )
                else:
                    outcome = "recovery_inspection_failed"
                self._status["last_outcome"] = outcome
                return outcome
            self._status["last_outcome"] = "check_failed"
            return "check_failed"

    async def run_forever(self) -> None:
        try:
            while True:
                await self.check_once()
                await self._sleep(self.config.interval_seconds)
        finally:
            await self.close()
