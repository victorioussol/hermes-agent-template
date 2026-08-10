import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from mailswarm_observer import (
    Condition,
    MailSwarmObserver,
    ObserverConfig,
    ObserverStore,
    classify_snapshot,
    render_prometheus,
)


NOW = datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)
SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST_A = "sha256:" + "1" * 64
DIGEST_B = "sha256:" + "2" * 64


def iso(value=NOW):
    return value.isoformat().replace("+00:00", "Z")


class ObserverFixture:
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name)
        self.config = ObserverConfig(
            enabled=True,
            shadow_mode=True,
            alerts_enabled=False,
            source_url="https://observer-source.example/snapshot",
            source_bearer_token="source-secret",
            linear_team_id="team-mailswarm",
            github_repository="victorioussol/mailswarm",
            hermes_home=self.home,
            interval_seconds=900,
            max_attempts=2,
            timeout_seconds=10,
            source_max_age_minutes=30,
            stalled_minutes=120,
            loop_stale_minutes=150,
            integration_stale_minutes=1560,
            actions_warning_percent=80,
            alert_cooldown_minutes=360,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def snapshot(self, *, issues=None, **extra):
        payload = {
            "schema_version": 1,
            "observed_at": iso(),
            "scope": {
                "linear_team_id": "team-mailswarm",
                "github_repository": "victorioussol/mailswarm",
            },
            "issues": issues or [],
        }
        payload.update(extra)
        return payload

    @staticmethod
    def issue(issue_id="MSW-1", **overrides):
        payload = {
            "id": issue_id,
            "status": "In Progress",
            "labels": ["agent/codex"],
            "component": "component/mailbox-db",
            "attempt": 1,
            "migration_owner": False,
            "provider_session": {
                "id": "session-1",
                "state": "running",
                "last_activity_at": iso(),
            },
            "ci": {"state": "success", "completed_at": iso(), "sha": "abc123"},
            "cloud_test": {
                "state": "success",
                "completed_at": iso(),
                "sha": "abc123",
                "artifact_digest": "sha256:digest",
            },
        }
        payload.update(overrides)
        return payload

    @staticmethod
    def milestone(
        milestone_id="M1",
        *,
        sha=SHA_A,
        digest=DIGEST_A,
        approved=False,
        **overrides,
    ):
        payload = {
            "id": milestone_id,
            "expected_sha": sha,
            "expected_artifact_digest": digest,
            "checks_receipt": {
                "state": "success",
                "sha": sha,
                "artifact_digest": digest,
                "observed_at": iso(NOW - timedelta(minutes=10)),
                "evidence_url": "https://evidence.example/checks/M1",
            },
            "evidence_complete": True,
            "evidence": [{
                "kind": "cloud-test",
                "url": "https://evidence.example/artifacts/M1",
                "sha": sha,
                "artifact_digest": digest,
                "observed_at": iso(NOW - timedelta(minutes=5)),
            }],
        }
        if approved:
            payload["owner_approval"] = {
                "approved": True,
                "source_url": "https://linear.app/example/decision/M1",
                "approved_at": iso(),
                "sha": sha,
                "artifact_digest": digest,
            }
        payload.update(overrides)
        return payload


class ClassificationTests(ObserverFixture, unittest.TestCase):
    def test_no_implementation_issues_is_idle_without_alerts(self):
        result = classify_snapshot(self.snapshot(), self.config, now=NOW)
        self.assertEqual(result.classification, "idle")
        self.assertEqual(result.active_work_count, 0)
        self.assertEqual(result.conditions, [])

    def test_silence_is_unknown_not_confirmed_stopped(self):
        issue = self.issue(provider_session={})
        result = classify_snapshot(self.snapshot(issues=[issue]), self.config, now=NOW)
        self.assertEqual(result.classification, "status_unknown")
        self.assertNotIn("confirmed_stopped", result.issue_counts)

    def test_running_session_without_activity_is_unknown(self):
        issue = self.issue(provider_session={"id": "session-1", "state": "running"})
        result = classify_snapshot(self.snapshot(issues=[issue]), self.config, now=NOW)
        self.assertEqual(result.classification, "status_unknown")

    def test_unrecognized_provider_state_is_unknown(self):
        issue = self.issue(provider_session={
            "id": "session-1",
            "state": "probably-running",
            "last_activity_at": iso(),
        })
        result = classify_snapshot(self.snapshot(issues=[issue]), self.config, now=NOW)
        self.assertEqual(result.classification, "status_unknown")

    def test_failed_ci_alert_uses_ci_sha_not_successful_cloud_test_sha(self):
        issue = self.issue(
            ci={"state": "failed", "completed_at": iso(), "sha": "new-failing-sha"},
            cloud_test={
                "state": "success",
                "completed_at": iso(NOW - timedelta(minutes=5)),
                "sha": "old-success-sha",
                "artifact_digest": "sha256:old-success",
            },
        )
        first = classify_snapshot(
            self.snapshot(issues=[issue]), self.config, now=NOW
        )
        condition = next(
            item for item in first.conditions if item.classification == "quality_failed"
        )
        self.assertEqual(condition.sha, "new-failing-sha")

        changed = dict(issue)
        changed["ci"] = dict(issue["ci"], sha="next-failing-sha")
        second = classify_snapshot(
            self.snapshot(issues=[changed]), self.config, now=NOW
        )
        next_condition = next(
            item for item in second.conditions if item.classification == "quality_failed"
        )
        self.assertNotEqual(condition.fingerprint, next_condition.fingerprint)

    def test_only_explicit_terminal_provider_state_is_confirmed_stopped(self):
        for provider_state in ("failed", "canceled", "cancelled"):
            with self.subTest(provider_state=provider_state):
                issue = self.issue(provider_session={
                    "id": "session-1",
                    "state": provider_state,
                    "last_activity_at": iso(NOW - timedelta(minutes=5)),
                })
                result = classify_snapshot(
                    self.snapshot(issues=[issue]),
                    self.config,
                    now=NOW,
                )
                self.assertEqual(result.classification, "confirmed_stopped")

    def test_duplicate_issue_and_milestone_ids_are_rejected(self):
        duplicate_issue = self.snapshot(
            issues=[self.issue(), self.issue()],
        )
        with self.assertRaisesRegex(ValueError, "duplicate issue id"):
            classify_snapshot(duplicate_issue, self.config, now=NOW)

        duplicate_milestone = self.snapshot(
            milestones=[self.milestone(), self.milestone()],
        )
        with self.assertRaisesRegex(ValueError, "duplicate milestone id"):
            classify_snapshot(duplicate_milestone, self.config, now=NOW)

    def test_old_activity_on_explicit_active_session_is_only_suspected(self):
        issue = self.issue(provider_session={
            "id": "session-1",
            "state": "running",
            "last_activity_at": iso(NOW - timedelta(minutes=121)),
        })
        result = classify_snapshot(self.snapshot(issues=[issue]), self.config, now=NOW)
        self.assertEqual(result.classification, "suspected_stalled")

    def test_stale_source_is_unknown_and_does_not_classify_issues(self):
        snapshot = self.snapshot(issues=[self.issue()])
        snapshot["observed_at"] = iso(NOW - timedelta(minutes=31))
        result = classify_snapshot(snapshot, self.config, now=NOW)
        self.assertEqual(result.classification, "status_unknown")
        self.assertEqual(result.issues, [])

    def test_scope_outside_mailswarm_is_rejected(self):
        snapshot = self.snapshot()
        snapshot["scope"]["linear_team_id"] = "team-guiri"
        with self.assertRaisesRegex(ValueError, "outside the MailSwarm allowlist"):
            classify_snapshot(snapshot, self.config, now=NOW)

    def test_builder_component_and_migration_limits_are_deterministic(self):
        issues = [
            self.issue(
                f"MSW-{index}",
                provider_session={
                    "id": f"session-{index}",
                    "state": "running",
                    "last_activity_at": iso(),
                },
                migration_owner=index <= 2,
            )
            for index in range(1, 5)
        ]
        result = classify_snapshot(self.snapshot(issues=issues), self.config, now=NOW)
        kinds = {condition.classification for condition in result.conditions}
        self.assertEqual(result.active_work_count, 4)
        self.assertIn("capacity_paused", kinds)
        self.assertIn("migration_conflict", kinds)
        self.assertEqual(result.classification, "migration_conflict")

    def test_disabled_future_loop_does_not_create_false_alert(self):
        result = classify_snapshot(
            self.snapshot(loops=[{"name": "Daily Operations", "expected": False}]),
            self.config,
            now=NOW,
        )
        self.assertEqual(result.classification, "idle")
        self.assertEqual(result.conditions, [])

    def test_expected_stale_loop_is_unknown_not_failed(self):
        result = classify_snapshot(
            self.snapshot(loops=[{
                "name": "Quality Controller",
                "expected": True,
                "heartbeat_at": iso(NOW - timedelta(minutes=151)),
            }]),
            self.config,
            now=NOW,
        )
        self.assertEqual(result.classification, "status_unknown")

    def test_fresh_actions_receipt_crossing_threshold_warns(self):
        result = classify_snapshot(
            self.snapshot(github_actions_usage={
                "checked_at": iso(),
                "used": 81,
                "limit": 100,
            }),
            self.config,
            now=NOW,
        )
        self.assertEqual(result.classification, "actions_usage_warning")

    def test_milestone_requires_exact_sha_digest_and_explicit_approval(self):
        milestone = self.milestone()
        ready = classify_snapshot(
            self.snapshot(milestones=[milestone]), self.config, now=NOW
        )
        self.assertEqual(ready.milestone_counts, {"milestone_ready": 1})

        wrong_sha = dict(milestone)
        wrong_sha["evidence"] = [dict(milestone["evidence"][0], sha="different")]
        not_ready = classify_snapshot(
            self.snapshot(milestones=[wrong_sha]), self.config, now=NOW
        )
        self.assertEqual(not_ready.milestone_counts, {"not_ready": 1})

        approved = self.milestone(approved=True)
        complete = classify_snapshot(
            self.snapshot(milestones=[approved]), self.config, now=NOW
        )
        self.assertEqual(complete.milestone_counts, {"milestone_complete": 1})

    def test_milestone_rejects_noncanonical_candidate_identity(self):
        candidates = (
            self.milestone(sha="abc123"),
            self.milestone(digest="sha256:abcd"),
            self.milestone(digest="sha256:" + "A" * 64),
        )
        for milestone in candidates:
            with self.subTest(milestone=milestone):
                result = classify_snapshot(
                    self.snapshot(milestones=[milestone]), self.config, now=NOW
                )
                self.assertEqual(result.milestone_counts, {"not_ready": 1})

    def test_milestone_checks_receipt_is_bound_to_candidate(self):
        base = self.milestone()
        invalid_receipts = (
            dict(base["checks_receipt"], sha=SHA_B),
            dict(base["checks_receipt"], artifact_digest=DIGEST_B),
            dict(base["checks_receipt"], observed_at=""),
            dict(
                base["checks_receipt"],
                observed_at="2026-08-10T09:50:00",
            ),
            dict(base["checks_receipt"], evidence_url=""),
            dict(
                base["checks_receipt"],
                observed_at=iso(NOW + timedelta(minutes=1)),
            ),
        )
        for receipt in invalid_receipts:
            with self.subTest(receipt=receipt):
                milestone = dict(base, checks_receipt=receipt)
                result = classify_snapshot(
                    self.snapshot(milestones=[milestone]), self.config, now=NOW
                )
                self.assertEqual(result.milestone_counts, {"not_ready": 1})

    def test_every_milestone_evidence_item_must_match_candidate_and_time(self):
        base = self.milestone()
        invalid_items = (
            dict(base["evidence"][0], sha=SHA_B),
            dict(base["evidence"][0], artifact_digest=DIGEST_B),
            dict(base["evidence"][0], observed_at=""),
            dict(
                base["evidence"][0],
                observed_at="2026-08-10T09:55:00",
            ),
            dict(
                base["evidence"][0],
                observed_at=iso(NOW + timedelta(minutes=1)),
            ),
        )
        for item in invalid_items:
            with self.subTest(item=item):
                milestone = dict(
                    base,
                    evidence=[base["evidence"][0], item],
                )
                result = classify_snapshot(
                    self.snapshot(milestones=[milestone]), self.config, now=NOW
                )
                self.assertEqual(result.milestone_counts, {"not_ready": 1})

    def test_old_approval_never_completes_a_new_candidate(self):
        old = self.milestone(approved=True)
        self.assertEqual(
            classify_snapshot(
                self.snapshot(milestones=[old]), self.config, now=NOW
            ).milestone_counts,
            {"milestone_complete": 1},
        )

        new_candidate = self.milestone(sha=SHA_B, digest=DIGEST_B)
        new_candidate["owner_approval"] = old["owner_approval"]
        result = classify_snapshot(
            self.snapshot(milestones=[new_candidate]), self.config, now=NOW
        )
        self.assertEqual(result.milestone_counts, {"milestone_ready": 1})

    def test_approval_must_follow_latest_candidate_receipt(self):
        for approved_at in (
            iso(NOW - timedelta(minutes=6)),
            iso(NOW + timedelta(minutes=1)),
            "2026-08-10T10:00:00",
        ):
            with self.subTest(approved_at=approved_at):
                milestone = self.milestone(approved=True)
                milestone["owner_approval"] = dict(
                    milestone["owner_approval"],
                    approved_at=approved_at,
                )
                result = classify_snapshot(
                    self.snapshot(milestones=[milestone]), self.config, now=NOW
                )
                self.assertEqual(
                    result.milestone_counts,
                    {"milestone_ready": 1},
                )

    def test_milestone_completion_request_without_approval_requires_owner(self):
        milestone = self.milestone(completion_requested=True)
        result = classify_snapshot(
            self.snapshot(milestones=[milestone]), self.config, now=NOW
        )
        self.assertEqual(result.classification, "owner_decision_required")
        self.assertEqual(result.milestone_counts, {"milestone_ready": 1})

    def test_environment_cannot_reduce_poll_interval_below_fifteen_minutes(self):
        with patch.dict(os.environ, {"MAILSWARM_OBSERVER_INTERVAL_SECONDS": "60"}):
            config = ObserverConfig.from_env()
        self.assertEqual(config.interval_seconds, 900)

    def test_summary_environment_defaults_disabled_and_shadow_only(self):
        with patch.dict(os.environ, {}, clear=True):
            config = ObserverConfig.from_env()
        self.assertFalse(config.summary_enabled)
        self.assertTrue(config.summary_shadow_mode)


class PersistenceTests(ObserverFixture, unittest.TestCase):
    def test_cycle_persists_attempt_session_checks_evidence_and_decision(self):
        issue = self.issue(
            loop={"heartbeat_at": iso()},
            ci={
                "state": "success",
                "completed_at": iso(),
                "sha": SHA_A,
                "artifact_digest": DIGEST_A,
                "evidence_url": "https://github.com/victorioussol/mailswarm/actions/runs/1",
            },
            evidence=[{
                "kind": "ci",
                "url": "https://github.com/victorioussol/mailswarm/actions/runs/1",
                "sha": "abc123",
                "artifact_digest": "sha256:digest",
                "observed_at": iso(),
            }],
            owner_decisions=[{
                "source_url": "https://linear.app/example/issue/MSW-1",
                "decided_at": iso(),
                "decision": "Proceed after Sol review",
            }],
        )
        result = classify_snapshot(self.snapshot(
            issues=[issue],
            milestones=[self.milestone()],
        ), self.config, now=NOW)
        store = ObserverStore(self.config.database_path)
        store.persist_cycle(result, NOW)

        connection = sqlite3.connect(self.config.database_path)
        connection.row_factory = sqlite3.Row
        persisted = connection.execute("SELECT * FROM issue_state").fetchone()
        self.assertEqual(persisted["attempt"], 1)
        self.assertEqual(persisted["provider_session_id"], "session-1")
        self.assertEqual(persisted["last_ci_sha"], SHA_A)
        self.assertEqual(persisted["last_ci_digest"], DIGEST_A)
        self.assertEqual(
            persisted["last_ci_evidence_url"],
            "https://github.com/victorioussol/mailswarm/actions/runs/1",
        )
        self.assertEqual(persisted["last_cloud_test_digest"], "sha256:digest")
        self.assertEqual(persisted["last_loop_heartbeat_at"], iso())
        self.assertEqual(connection.execute("SELECT count(*) FROM evidence").fetchone()[0], 2)
        milestone = connection.execute("SELECT * FROM milestone_state").fetchone()
        self.assertEqual(milestone["state"], "milestone_ready")
        self.assertEqual(milestone["expected_sha"], SHA_A)
        self.assertEqual(milestone["expected_artifact_digest"], DIGEST_A)
        self.assertEqual(milestone["checks_sha"], SHA_A)
        self.assertEqual(milestone["checks_artifact_digest"], DIGEST_A)
        transition = connection.execute(
            "SELECT * FROM milestone_update_history"
        ).fetchone()
        self.assertEqual(transition["state"], "milestone_ready")
        self.assertEqual(transition["sha"], SHA_A)
        self.assertEqual(transition["artifact_digest"], DIGEST_A)
        self.assertEqual(
            connection.execute("SELECT count(*) FROM owner_decisions").fetchone()[0],
            1,
        )
        connection.close()
        self.assertEqual(self.config.database_path.stat().st_mode & 0o777, 0o600)

    def test_alert_history_deduplicates_until_cooldown(self):
        store = ObserverStore(self.config.database_path)
        condition = Condition("status_unknown", "warning", "MSW-1", "state missing")
        self.assertTrue(store.alert_due(condition, NOW, timedelta(hours=6)))
        store.mark_alert_sent(condition, NOW)
        self.assertFalse(
            store.alert_due(condition, NOW + timedelta(hours=1), timedelta(hours=6))
        )
        self.assertTrue(
            store.alert_due(condition, NOW + timedelta(hours=7), timedelta(hours=6))
        )

    def test_alert_fingerprint_uses_attempt_and_exact_sha_not_reason_prose(self):
        first = Condition(
            "quality_failed",
            "critical",
            "MSW-1",
            "first wording",
            attempt=1,
            sha=SHA_A,
            artifact_digest=DIGEST_A,
        )
        reworded = Condition(
            "quality_failed",
            "critical",
            "MSW-1",
            "different wording",
            attempt=1,
            sha=SHA_A,
            artifact_digest=DIGEST_A,
        )
        next_attempt = Condition(
            "quality_failed",
            "critical",
            "MSW-1",
            "first wording",
            attempt=2,
            sha=SHA_B,
            artifact_digest=DIGEST_B,
        )
        self.assertEqual(first.fingerprint, reworded.fingerprint)
        self.assertNotEqual(first.fingerprint, next_attempt.fingerprint)

    def test_store_closes_connections_after_commit_and_rollback(self):
        opened = []
        real_connect = sqlite3.connect

        def tracked_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            opened.append(connection)
            return connection

        store = ObserverStore(self.config.database_path)
        result = classify_snapshot(self.snapshot(), self.config, now=NOW)
        with patch(
            "mailswarm_observer.sqlite3.connect",
            side_effect=tracked_connect,
        ):
            store.persist_cycle(result, NOW)
            with self.assertRaisesRegex(RuntimeError, "rollback check"):
                with store._transaction() as connection:
                    connection.execute(
                        "INSERT INTO observer_meta(key, value, updated_at) VALUES (?, ?, ?)",
                        ("rollback", "value", iso()),
                    )
                    raise RuntimeError("rollback check")

        self.assertGreaterEqual(len(opened), 2)
        for connection in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
        verification = sqlite3.connect(self.config.database_path)
        self.assertIsNone(
            verification.execute(
                "SELECT value FROM observer_meta WHERE key = 'rollback'"
            ).fetchone()
        )
        verification.close()


class RuntimeTests(ObserverFixture, unittest.IsolatedAsyncioTestCase):
    async def test_disabled_observer_does_not_create_state_or_call_source(self):
        calls = []

        async def reader():
            calls.append(True)
            return self.snapshot()

        observer = MailSwarmObserver(replace(self.config, enabled=False), reader=reader)
        self.assertEqual(await observer.check_once(), "disabled")
        self.assertEqual(calls, [])
        self.assertFalse(self.config.database_path.exists())

    async def test_source_read_is_bounded_to_two_attempts(self):
        attempts = []

        async def reader():
            attempts.append(True)
            raise RuntimeError("unavailable")

        async def no_wait(_seconds):
            return None

        observer = MailSwarmObserver(
            self.config,
            reader=reader,
            now=lambda: NOW,
            sleep=no_wait,
        )
        self.assertEqual(await observer.check_once(), "status_unknown")
        self.assertEqual(len(attempts), 2)

    async def test_source_outage_after_restart_preserves_last_known_issue_state(self):
        first = MailSwarmObserver(self.config, now=lambda: NOW)
        await first.check_once(self.snapshot(issues=[self.issue()]))
        await first.close()

        async def unavailable():
            raise RuntimeError("source offline")

        async def no_wait(_seconds):
            return None

        restarted = MailSwarmObserver(
            self.config,
            reader=unavailable,
            now=lambda: NOW + timedelta(minutes=15),
            sleep=no_wait,
        )
        self.assertEqual(await restarted.check_once(), "status_unknown")
        connection = sqlite3.connect(self.config.database_path)
        connection.row_factory = sqlite3.Row
        persisted = connection.execute("SELECT * FROM issue_state").fetchone()
        connection.close()
        self.assertEqual(persisted["issue_id"], "MSW-1")
        self.assertEqual(persisted["provider_session_id"], "session-1")
        self.assertEqual(persisted["last_ci_sha"], "abc123")

    async def test_duplicate_ids_after_restart_degrade_without_losing_state(self):
        first = MailSwarmObserver(self.config, now=lambda: NOW)
        await first.check_once(self.snapshot(
            issues=[self.issue()],
            milestones=[self.milestone()],
        ))
        await first.close()

        restarted = MailSwarmObserver(self.config, now=lambda: NOW)
        duplicate_issues = self.snapshot(
            issues=[self.issue(), self.issue()],
        )
        self.assertEqual(
            await restarted.check_once(duplicate_issues),
            "status_unknown",
        )
        duplicate_milestones = self.snapshot(
            milestones=[self.milestone(), self.milestone()],
        )
        self.assertEqual(
            await restarted.check_once(duplicate_milestones),
            "status_unknown",
        )
        self.assertEqual(restarted.public_status()["source_state"], "unavailable")

        connection = sqlite3.connect(self.config.database_path)
        issue_id = connection.execute(
            "SELECT issue_id FROM issue_state"
        ).fetchone()[0]
        milestone_id = connection.execute(
            "SELECT milestone_id FROM milestone_state"
        ).fetchone()[0]
        connection.close()
        self.assertEqual(issue_id, "MSW-1")
        self.assertEqual(milestone_id, "M1")

    async def test_persistence_failure_degrades_cycle_and_run_forever_continues(self):
        real_store = ObserverStore(self.config.database_path)

        class FlakyStore:
            def __init__(self):
                self.persist_calls = 0

            def persist_cycle(self, result, now):
                self.persist_calls += 1
                if self.persist_calls == 1:
                    raise sqlite3.OperationalError("simulated persistence outage")
                real_store.persist_cycle(result, now)

            def __getattr__(self, name):
                return getattr(real_store, name)

        flaky_store = FlakyStore()
        first_cycle_status = []

        async def reader():
            return self.snapshot(issues=[self.issue()])

        async def bounded_sleep(_seconds):
            if flaky_store.persist_calls == 1:
                first_cycle_status.append(observer.public_status())
                return None
            raise asyncio.CancelledError()

        observer = MailSwarmObserver(
            self.config,
            reader=reader,
            store=flaky_store,
            now=lambda: NOW,
            sleep=bounded_sleep,
        )
        with self.assertRaises(asyncio.CancelledError):
            await observer.run_forever()

        self.assertEqual(flaky_store.persist_calls, 2)
        self.assertEqual(first_cycle_status[0]["classification"], "status_unknown")
        self.assertEqual(first_cycle_status[0]["source_state"], "unavailable")
        self.assertEqual(first_cycle_status[0]["persistence_errors_total"], 1)
        self.assertEqual(observer.public_status()["source_state"], "fresh")
        connection = sqlite3.connect(self.config.database_path)
        self.assertEqual(
            connection.execute("SELECT count(*) FROM issue_state").fetchone()[0],
            1,
        )
        connection.close()

    async def test_shadow_mode_never_sends_alert(self):
        notices = []

        async def sender(message):
            notices.append(message)
            return True

        observer = MailSwarmObserver(
            replace(self.config, alerts_enabled=True),
            alert_sender=sender,
            now=lambda: NOW,
        )
        await observer.check_once(self.snapshot(issues=[self.issue(provider_session={})]))
        self.assertEqual(notices, [])

    async def test_live_alert_is_sent_once_and_contains_no_secret(self):
        notices = []

        async def sender(message):
            notices.append(message)
            return True

        config = replace(
            self.config,
            shadow_mode=False,
            alerts_enabled=True,
            telegram_bot_token="bot-secret",
            telegram_chat_id="chat-secret",
        )
        observer = MailSwarmObserver(config, alert_sender=sender, now=lambda: NOW)
        snapshot = self.snapshot(issues=[self.issue(provider_session={})])
        await observer.check_once(snapshot)
        await observer.check_once(snapshot)
        self.assertEqual(len(notices), 1)
        self.assertNotIn("bot-secret", notices[0])
        self.assertNotIn("chat-secret", json.dumps(observer.public_status()))

    async def test_new_attempt_sha_alerts_and_emits_resolution_for_prior_alert(self):
        notices = []

        async def sender(message):
            notices.append(message)
            return True

        config = replace(
            self.config,
            shadow_mode=False,
            alerts_enabled=True,
            telegram_bot_token="bot-secret",
            telegram_chat_id="chat-secret",
        )
        observer = MailSwarmObserver(config, alert_sender=sender, now=lambda: NOW)
        first = self.issue(provider_session={}, attempt=1)
        second = self.issue(
            provider_session={},
            attempt=2,
            ci={"state": "success", "completed_at": iso(), "sha": "def456"},
            cloud_test={
                "state": "success",
                "completed_at": iso(),
                "sha": "def456",
                "artifact_digest": "sha256:def456",
            },
        )
        await observer.check_once(self.snapshot(issues=[first]))
        await observer.check_once(self.snapshot(issues=[second]))
        self.assertEqual(
            len([message for message in notices if message.startswith("MailSwarm Observer:")]),
            2,
        )
        self.assertEqual(
            len([message for message in notices if message.startswith("MailSwarm Observer resolved:")]),
            1,
        )

    async def test_returning_incident_reopens_before_pending_resolution_delivery(self):
        notices = []
        fail_resolution = False

        async def sender(message):
            notices.append(message)
            return not (
                fail_resolution
                and message.startswith("MailSwarm Observer resolved:")
            )

        async def no_wait(_seconds):
            return None

        config = replace(
            self.config,
            shadow_mode=False,
            alerts_enabled=True,
            telegram_bot_token="bot-secret",
            telegram_chat_id="chat-secret",
        )
        observer = MailSwarmObserver(
            config,
            alert_sender=sender,
            now=lambda: NOW,
            sleep=no_wait,
        )
        incident = self.snapshot(issues=[self.issue(provider_session={})])
        await observer.check_once(incident)

        fail_resolution = True
        await observer.check_once(self.snapshot())
        notices.clear()

        fail_resolution = False
        await observer.check_once(incident)
        self.assertEqual(
            len([
                message
                for message in notices
                if message.startswith("MailSwarm Observer:")
            ]),
            1,
        )
        self.assertFalse(any(
            message.startswith("MailSwarm Observer resolved:")
            for message in notices
        ))

    async def test_actionable_incident_and_resolution_preserve_identity_without_secrets(self):
        notices = []

        async def sender(message):
            notices.append(message)
            return True

        config = replace(
            self.config,
            shadow_mode=False,
            alerts_enabled=True,
            telegram_bot_token="bot-secret",
            telegram_chat_id="chat-secret",
        )
        issue = self.issue(
            attempt=7,
            ci={
                "state": "failed",
                "completed_at": iso(),
                "sha": SHA_A,
                "artifact_digest": DIGEST_A,
                "evidence_url": "https://evidence.example/check?token=leak#fragment",
            },
            evidence=[
                {
                    "url": "https://user:password@evidence.example/private",
                    "sha": SHA_A,
                    "artifact_digest": DIGEST_A,
                },
                *[
                    {
                    "url": f"https://evidence.example/run/{index}?secret=value-{index}",
                    "sha": SHA_A,
                    "artifact_digest": DIGEST_A,
                    }
                    for index in range(4)
                ],
            ],
        )
        observer = MailSwarmObserver(config, alert_sender=sender, now=lambda: NOW)
        await observer.check_once(self.snapshot(issues=[issue]))
        await observer.check_once(self.snapshot())

        self.assertEqual(len(notices), 2)
        for message in notices:
            self.assertIn("subject=MSW-1", message)
            self.assertIn("attempt=7", message)
            self.assertIn(f"sha={SHA_A}", message)
            self.assertIn(f"digest={DIGEST_A}", message)
            self.assertIn("next_action=", message)
            self.assertNotIn("token=", message)
            self.assertNotIn("secret=", message)
            self.assertNotIn("fragment", message)
            self.assertNotIn("password", message)
            self.assertNotIn("bot-secret", message)
            self.assertNotIn("chat-secret", message)
            self.assertLessEqual(message.count("https://evidence.example/"), 3)
        self.assertTrue(notices[0].startswith("MailSwarm Observer:"))
        self.assertTrue(notices[1].startswith("MailSwarm Observer resolved:"))

    async def test_transport_redacts_known_secrets_from_snapshot_controlled_fields(self):
        notices = []
        known_secret = "SOURCE-BEARER-SECRET"

        async def sender(message):
            notices.append(message)
            return True

        config = replace(
            self.config,
            shadow_mode=False,
            alerts_enabled=True,
            source_bearer_token=known_secret,
            telegram_bot_token="bot-secret",
            telegram_chat_id="chat-secret",
        )
        issue = self.issue(
            issue_id=f"MSW-{known_secret}",
            provider_session={},
            evidence=[{
                "url": f"https://evidence.example/{known_secret}/receipt",
                "sha": SHA_A,
                "artifact_digest": DIGEST_A,
            }],
        )
        result = classify_snapshot(
            self.snapshot(issues=[issue]), self.config, now=NOW
        )
        self.assertNotIn(known_secret, result.conditions[0].fingerprint)

        observer = MailSwarmObserver(config, alert_sender=sender, now=lambda: NOW)
        await observer.check_once(self.snapshot(issues=[issue]))
        self.assertEqual(len(notices), 1)
        self.assertNotIn(known_secret, notices[0])
        self.assertIn("[REDACTED]", notices[0])

    async def test_milestone_transition_sends_once_across_cycles_and_restart(self):
        notices = []

        async def sender(message):
            notices.append(message)
            return True

        config = replace(
            self.config,
            shadow_mode=False,
            alerts_enabled=True,
            telegram_bot_token="bot-secret",
            telegram_chat_id="chat-secret",
        )
        snapshot = self.snapshot(milestones=[self.milestone()])
        observer = MailSwarmObserver(config, alert_sender=sender, now=lambda: NOW)
        await observer.check_once(snapshot)
        await observer.check_once(snapshot)
        await observer.close()

        restarted = MailSwarmObserver(config, alert_sender=sender, now=lambda: NOW)
        await restarted.check_once(snapshot)
        milestone_messages = [
            message
            for message in notices
            if message.startswith("MailSwarm milestone update:")
        ]
        self.assertEqual(len(milestone_messages), 1)
        self.assertIn("state=milestone_ready", milestone_messages[0])
        self.assertIn("subject=milestone:M1", milestone_messages[0])
        self.assertIn(f"sha={SHA_A}", milestone_messages[0])
        self.assertIn(f"digest={DIGEST_A}", milestone_messages[0])

    async def test_pending_milestone_transition_waits_for_fresh_current_source(self):
        notices = []

        async def sender(message):
            notices.append(message)
            return True

        shadow = MailSwarmObserver(self.config, now=lambda: NOW)
        fresh_snapshot = self.snapshot(milestones=[self.milestone()])
        await shadow.check_once(fresh_snapshot)

        live_config = replace(
            self.config,
            shadow_mode=False,
            alerts_enabled=True,
            telegram_bot_token="bot-secret",
            telegram_chat_id="chat-secret",
        )
        live = MailSwarmObserver(
            live_config,
            alert_sender=sender,
            now=lambda: NOW,
        )
        stale_snapshot = self.snapshot(milestones=[self.milestone()])
        stale_snapshot["observed_at"] = iso(NOW - timedelta(minutes=31))
        self.assertEqual(
            await live.check_once(stale_snapshot),
            "status_unknown",
        )
        self.assertFalse(any(
            message.startswith("MailSwarm milestone update:")
            for message in notices
        ))

        async def unavailable():
            raise RuntimeError("source unavailable")

        async def no_wait(_seconds):
            return None

        outage = MailSwarmObserver(
            live_config,
            reader=unavailable,
            alert_sender=sender,
            now=lambda: NOW,
            sleep=no_wait,
        )
        self.assertEqual(await outage.check_once(), "status_unknown")
        self.assertFalse(any(
            message.startswith("MailSwarm milestone update:")
            for message in notices
        ))
        connection = sqlite3.connect(self.config.database_path)
        attempts = connection.execute(
            "SELECT delivery_attempts FROM milestone_update_history"
        ).fetchone()[0]
        connection.close()
        self.assertEqual(attempts, 0)

        await live.check_once(fresh_snapshot)
        milestone_messages = [
            message
            for message in notices
            if message.startswith("MailSwarm milestone update:")
        ]
        self.assertEqual(len(milestone_messages), 1)
        self.assertIn("state=milestone_ready", milestone_messages[0])

    async def test_failed_milestone_update_retries_to_persisted_bound(self):
        notices = []
        fail = True

        async def sender(message):
            notices.append(message)
            return not fail

        async def no_wait(_seconds):
            return None

        config = replace(
            self.config,
            shadow_mode=False,
            alerts_enabled=True,
            telegram_bot_token="bot-secret",
            telegram_chat_id="chat-secret",
        )
        snapshot = self.snapshot(milestones=[self.milestone()])
        observer = MailSwarmObserver(
            config,
            alert_sender=sender,
            now=lambda: NOW,
            sleep=no_wait,
        )
        await observer.check_once(snapshot)
        fail = False
        await observer.check_once(snapshot)
        await observer.check_once(snapshot)

        self.assertEqual(len(notices), 3)
        connection = sqlite3.connect(self.config.database_path)
        row = connection.execute(
            "SELECT delivery_attempts, sent_at FROM milestone_update_history"
        ).fetchone()
        connection.close()
        self.assertEqual(row[0], 3)
        self.assertIsNotNone(row[1])

    async def test_failed_milestone_update_stops_after_hard_attempt_bound(self):
        notices = []

        async def sender(message):
            notices.append(message)
            return False

        async def no_wait(_seconds):
            return None

        config = replace(
            self.config,
            shadow_mode=False,
            alerts_enabled=True,
            telegram_bot_token="bot-secret",
            telegram_chat_id="chat-secret",
        )
        observer = MailSwarmObserver(
            config,
            alert_sender=sender,
            now=lambda: NOW,
            sleep=no_wait,
        )
        snapshot = self.snapshot(milestones=[self.milestone()])
        await observer.check_once(snapshot)
        await observer.check_once(snapshot)
        await observer.check_once(snapshot)
        self.assertEqual(len(notices), 4)
        connection = sqlite3.connect(self.config.database_path)
        attempts = connection.execute(
            "SELECT delivery_attempts FROM milestone_update_history"
        ).fetchone()[0]
        connection.close()
        self.assertEqual(attempts, 4)

    async def test_new_candidate_and_ready_to_complete_are_distinct_updates(self):
        notices = []

        async def sender(message):
            notices.append(message)
            return True

        config = replace(
            self.config,
            shadow_mode=False,
            alerts_enabled=True,
            telegram_bot_token="bot-secret",
            telegram_chat_id="chat-secret",
        )
        observer = MailSwarmObserver(config, alert_sender=sender, now=lambda: NOW)
        await observer.check_once(
            self.snapshot(milestones=[self.milestone()])
        )
        await observer.check_once(
            self.snapshot(milestones=[self.milestone(sha=SHA_B, digest=DIGEST_B)])
        )
        await observer.check_once(
            self.snapshot(milestones=[
                self.milestone(
                    sha=SHA_B,
                    digest=DIGEST_B,
                    approved=True,
                )
            ])
        )
        milestone_messages = [
            message
            for message in notices
            if message.startswith("MailSwarm milestone update:")
        ]
        self.assertEqual(len(milestone_messages), 3)
        self.assertEqual(
            ["state=milestone_ready" in message for message in milestone_messages],
            [True, True, False],
        )
        self.assertIn("state=milestone_complete", milestone_messages[-1])
        self.assertFalse(any("Observer resolved:" in message for message in notices))

    async def test_not_ready_milestone_never_creates_transition_update(self):
        notices = []

        async def sender(message):
            notices.append(message)
            return True

        config = replace(
            self.config,
            shadow_mode=False,
            alerts_enabled=True,
            telegram_bot_token="bot-secret",
            telegram_chat_id="chat-secret",
        )
        observer = MailSwarmObserver(config, alert_sender=sender, now=lambda: NOW)
        await observer.check_once(
            self.snapshot(milestones=[self.milestone(sha="short")])
        )
        self.assertEqual(notices, [])
        connection = sqlite3.connect(self.config.database_path)
        count = connection.execute(
            "SELECT count(*) FROM milestone_update_history"
        ).fetchone()[0]
        connection.close()
        self.assertEqual(count, 0)

    async def test_obsolete_shadow_transition_is_superseded_before_live_mode(self):
        notices = []

        async def sender(message):
            notices.append(message)
            return True

        shadow_config = replace(self.config, alerts_enabled=True)
        shadow = MailSwarmObserver(
            shadow_config,
            alert_sender=sender,
            now=lambda: NOW,
        )
        await shadow.check_once(
            self.snapshot(milestones=[self.milestone()])
        )
        not_ready = self.milestone()
        not_ready["evidence_complete"] = False
        await shadow.check_once(self.snapshot(milestones=[not_ready]))
        self.assertEqual(notices, [])

        live_config = replace(
            self.config,
            shadow_mode=False,
            alerts_enabled=True,
            telegram_bot_token="bot-secret",
            telegram_chat_id="chat-secret",
        )
        live = MailSwarmObserver(
            live_config,
            alert_sender=sender,
            now=lambda: NOW,
        )
        await live.check_once(self.snapshot(milestones=[not_ready]))
        self.assertEqual(notices, [])
        connection = sqlite3.connect(self.config.database_path)
        row = connection.execute(
            "SELECT superseded_at, superseded_by_state FROM milestone_update_history"
        ).fetchone()
        connection.close()
        self.assertIsNotNone(row[0])
        self.assertEqual(row[1], "not_ready")

        await live.check_once(
            self.snapshot(milestones=[self.milestone()])
        )
        self.assertEqual(len(notices), 1)
        self.assertIn("state=milestone_ready", notices[0])

    async def test_disabled_summary_touches_no_source_database_or_transport(self):
        notices = []

        async def sender(message):
            notices.append(message)
            return True

        observer = MailSwarmObserver(
            self.config,
            alert_sender=sender,
            now=lambda: NOW,
        )
        with patch("builtins.print") as output:
            outcome = await observer.send_summary("Morning")
        self.assertEqual(outcome, "disabled")
        self.assertEqual(notices, [])
        self.assertFalse(self.config.database_path.exists())
        self.assertIn("summary_disabled", output.call_args.args[0])

    async def test_shadow_summary_reads_persisted_aggregate_without_sending(self):
        notices = []

        async def sender(message):
            notices.append(message)
            return True

        config = replace(
            self.config,
            summary_enabled=True,
            summary_shadow_mode=False,
        )
        observer = MailSwarmObserver(config, alert_sender=sender, now=lambda: NOW)
        milestone = self.milestone()
        milestone["evidence"] = [dict(
            milestone["evidence"][0],
            url="https://evidence.example/source-secret/artifact",
        )]
        await observer.check_once(
            self.snapshot(
                issues=[self.issue()],
                milestones=[milestone],
            )
        )
        with patch("builtins.print") as output:
            outcome = await observer.send_summary("Morning")
        self.assertEqual(outcome, "shadow")
        self.assertEqual(notices, [])
        message = output.call_args.args[0]
        self.assertIn("[MailSwarm Morning Summary]", message)
        self.assertIn("deterministic=true", message)
        self.assertIn("model_used=false", message)
        self.assertIn("active_work_count=1", message)
        self.assertNotIn("source-secret", message)
        self.assertIn("[REDACTED]", message)

    async def test_live_summary_fails_closed_without_credentials(self):
        notices = []

        async def sender(message):
            notices.append(message)
            return True

        config = replace(
            self.config,
            shadow_mode=False,
            summary_enabled=True,
            summary_shadow_mode=False,
            telegram_bot_token="",
            telegram_chat_id="",
        )
        observer = MailSwarmObserver(config, alert_sender=sender, now=lambda: NOW)
        with patch("builtins.print") as output:
            outcome = await observer.send_summary("Morning")
        self.assertEqual(outcome, "misconfigured")
        self.assertEqual(notices, [])
        self.assertFalse(self.config.database_path.exists())
        self.assertIn("summary_misconfigured", output.call_args.args[0])

    async def test_explicit_live_summary_is_clearly_labeled_and_non_ai(self):
        notices = []

        async def sender(message):
            notices.append(message)
            return True

        config = replace(
            self.config,
            shadow_mode=False,
            summary_enabled=True,
            summary_shadow_mode=False,
            telegram_bot_token="bot-secret",
            telegram_chat_id="chat-secret",
        )
        observer = MailSwarmObserver(config, alert_sender=sender, now=lambda: NOW)
        await observer.check_once(self.snapshot(issues=[self.issue()]))
        self.assertEqual(await observer.send_summary("Evening"), "sent")
        self.assertEqual(len(notices), 1)
        self.assertIn("[MailSwarm Evening Summary]", notices[0])
        self.assertIn("deterministic=true", notices[0])
        self.assertIn("model_used=false", notices[0])
        self.assertNotIn("bot-secret", notices[0])
        self.assertNotIn("chat-secret", notices[0])

    async def test_transport_test_is_separate_from_live_alert_history(self):
        notices = []

        async def sender(message):
            notices.append(message)
            return True

        observer = MailSwarmObserver(self.config, alert_sender=sender, now=lambda: NOW)
        self.assertTrue(await observer.send_test_alert())
        self.assertIn("[TEST]", notices[0])
        connection = sqlite3.connect(self.config.database_path)
        self.assertEqual(
            connection.execute("SELECT count(*) FROM alert_history").fetchone()[0],
            0,
        )
        self.assertEqual(
            connection.execute(
                "SELECT count(*) FROM observer_events WHERE kind = 'transport_test'"
            ).fetchone()[0],
            1,
        )
        connection.close()

    async def test_metrics_are_aggregate_only(self):
        observer = MailSwarmObserver(self.config, now=lambda: NOW)
        await observer.check_once(self.snapshot(issues=[self.issue()]))
        status = observer.public_status()
        rendered = render_prometheus(status, task_running=True)
        self.assertIn("mailswarm_observer_active_work_count 1", rendered)
        self.assertNotIn("MSW-1", rendered)
        self.assertNotIn("session-1", rendered)


if __name__ == "__main__":
    unittest.main()
