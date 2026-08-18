# Hermes Wrapper Engineering Instructions

## Shared operating contract

- On Victor's Mac, read `/Users/victorsolares/.agents/engineering-policy/POLICY.md` before non-trivial work.
- Keep durable instructions capability-based. Model and provider identifiers belong only in volatile runtime routing and review receipts.
- Proceed autonomously through reversible, in-scope work. Ask only for a genuine product decision or a protected external effect.
- The root `AGENTS.md` is the repository source of truth. Tool-specific files are thin adapters only.

## Repository purpose and stack

- Python and Starlette wrapper for the Hermes runtime, setup and administration UI, monitoring, pairing, operational inboxes, and Railway deployment.
- Persistent runtime state lives under the configured Hermes home directory. Treat environment files, auth JSON, pairing records, outboxes, and status files as sensitive state.
- Preserve the existing Python stack, managed configuration contract, and deployment topology. A new runtime dependency or provider needs a short reason, maintenance and permission review, and focused proof.

## Task isolation and preservation

- Administrative checkout: `/Users/victorsolares/Developer/GitHub/hermes-agent-template`.
- New task worktree: `/Users/victorsolares/Developer/GitHub/_worktrees/hermes-agent-template-<ticket-or-date>-<slug>`.
- New task branch: `task/<ticket-or-date>-<slug>`.
- The administrative checkout contains staged user work. Do not edit, clean, reset, or repurpose it from another task.
- Existing branch names and locations are grandfathered for preservation. New work uses the neutral layout.
- Never implement on `main`, `master`, a detached checkout, or the administrative checkout.
- Start with `taskctl start <ticket-or-date>-<slug> --repo /Users/victorsolares/Developer/GitHub/hermes-agent-template`.
- At the first meaningful change, before switching coding environments, or before ending a session, run `taskctl checkpoint`.
- A secret-scanned draft checkpoint may disclose failing checks. Green checks are required before merge or deployment, not before preservation.
- Use `taskctl audit` before handoff or cleanup. Use `taskctl park` only after the exact commit is remotely preserved and linked to an open pull request; recreate it with `taskctl resume <ticket-or-date>-<slug>`.
- Never delete a worktree folder directly, bulk-prune, delete by branch name or age, rewrite history, or treat a stash as backup.

Storage modes are automatic: 40 GiB free is the healthy target; below 20 GiB serializes disk-heavy work; below 5 GiB blocks new worktrees, dependency installs, and builds.

## Security boundaries

- Run `python3 scripts/security/secret-scan.py --staged` before checkpointing; CI scans newly added lines again without printing values.
- Credentials are injected at runtime from an approved store. Never commit, paste, log, screenshot, archive, or broadly export them.
- Do not read or copy raw environment files, private keys, auth files, or pairing records unless the exact task requires it. Check only named presence and report no values.
- The wrapper must pass only the minimum required environment into child processes. Do not forward platform, deployment, monitoring, or wrapper credentials into the managed runtime.
- Keep authentication cookies secure, same-origin protections active, secrets masked in the UI, and personal or credential data out of logs.
- Pairing approval, config mutation, restart, reset, provider authentication, outbox acknowledgement, and recovery actions must remain authenticated and auditable.
- A real QA account is acceptable only when isolated from production and customer data, minimally scoped, spend and send capped, auditable, expiring, and revocable. It must have no administrator, deployment, billing, or unrestricted-send authority.
- Removing a committed credential does not revoke it. Rotation, session invalidation, log review, and history remediation are separate approval-gated actions.

## Reliability and production

- Read `docs/reliability-runbook.md` before monitoring, recovery, or deployment work.
- Scheduled monitoring has only the standing authority documented in that runbook. Do not broaden it, change production identifiers, trigger alert tests, redeploy, or alter recovery thresholds without the applicable approval.
- A health endpoint response is not sufficient proof by itself. Distinguish process reachability, configured readiness, downstream health, recovery action, and sustained service health.
- Preserve bounded retries, backoff, concurrency limits, idempotency, redaction, and explicit timeout behavior.
- Never weaken a safety gate to make a monitor green.

## Verification

For ordinary code changes:

```bash
python -m py_compile server.py career_outbox_append.py hermes_monitor.py
python -m unittest discover -s tests -v
```

For container changes, use the checked-in image smoke test after a successful build.

- Run focused tests first, then the applicable checked-in workflow evidence before merge.
- In pressure mode, serialize dependency installs, Docker builds, and other disk-heavy checks through `taskctl heavy`.
- Documentation-only changes may use syntax, policy, workflow, and secret-scan proof instead of installing dependencies or building an image; disclose that limit in the pull request.
- A local pass is not deployment authority or observed production health.

## Completion

Report the exact branch and candidate commit, checks run, remaining uncertainty, and the actual completion level: implemented, locally verified, integrated, deployed, recovered, or observed healthy.
