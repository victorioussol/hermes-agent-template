---
name: hermes-continuous-improvement
description: Review Hermes task outcomes, recurring failures, model routing, memory quality, skills, scheduled jobs, and cost controls, then propose and verify reversible improvements. Use for daily health reviews, weekly usefulness audits, monthly model evaluations, repeated agent failures, model or provider changes, memory cleanup, skill creation or consolidation, and post-incident learning.
---

# Hermes Continuous Improvement

Improve Hermes from evidence while keeping routine work subscription-backed,
paid fallbacks bounded, and durable changes under human control.

## Guardrails

- Prefer observed outcomes, logs, and fixed evaluations over model reputation.
- Never expose credentials, raw private conversations, or full memory contents in reports.
- Do not change models, schedules, `SOUL.md`, `USER.md`, `MEMORY.md`, or skills permanently without Victor's approval.
- Make one bounded change at a time. Preserve a rollback and verify the real channel or job before calling it fixed.
- Do not install public skills merely to increase the skill count. Create or keep a skill only for a repeated, proven workflow.
- Read [model-policy.md](references/model-policy.md) before changing any model route.
- Read [evaluation-set.yaml](references/evaluation-set.yaml) before a monthly model comparison.

## Review workflow

1. Establish the review window and read only the relevant health, job, usage, and outcome records.
2. Separate provider failures, model failures, tool failures, configuration drift, bad task design, and user-correction signals.
3. Record meaningful outcomes under `~/.hermes/improvement/outcomes.jsonl`: task class, route, result, verification, tools or skills used, failure category, recovery, and whether the pattern is repeatable. Store summaries, not raw content.
4. Identify repeated patterns. One isolated failure is an incident; two similar failures justify watching; three verified repetitions justify an improvement proposal.
5. Propose the smallest reversible change with expected benefit, cost impact, risks, canary, and rollback.
6. Obtain approval for durable changes. Run the fixed evaluation or real-channel canary, then compare against the previous baseline.
7. Keep the change only when it improves the measured outcome without breaking a critical case. Otherwise roll back and record the result.

## Cadence

- Daily: check service health, fresh scheduled-job failures, Codex OAuth canary, OpenRouter budget status, channel delivery, and memory availability. Report exceptions only.
- Weekly: back up skills, run curator dry-run, review user corrections and repeated failures, and run one independent DeepSeek Pro critic pass. Terra adjudicates; the critic only proposes.
- Monthly: run the fixed routing evaluation with a total challenger budget of at most USD 1. Archive stale proposals and update the model map only after approval.
- After an incident: capture the root cause, detection gap, recovery proof, and a concrete prevention or earlier-alert change.

## Memory and skills

- Keep built-in Hermes memory as the durable source of truth. Treat external memory as optional until its availability is verified.
- Save stable preferences, decisions, corrections, and reusable procedures. Do not save temporary status, copied logs, or speculation.
- Run deterministic curator pruning with backups. Leave LLM consolidation off by default; use proposal-first review for merges.
- Preserve environment-specific operations, memory governance, usefulness auditing, and this skill from automated archival.

## Completion standard

Finish with a compact report containing the evidence window, verified findings,
change or proposal, cost impact, canary result, rollback, and unresolved blockers.
Passing configuration or a green local test is not production proof.
