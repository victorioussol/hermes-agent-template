# Hermes Railway reliability runbook

This wrapper intentionally keeps the Railway deployment reproducible and the
main inference route explicit.

## Required production settings

- `HERMES_MODEL_PROVIDER=openai-codex`
- `LLM_MODEL=gpt-5.6-terra`
- `ADMIN_PASSWORD` set to a strong value
- `HERMES_DASHBOARD_IDLE_SECONDS=300` in production (the code default is 1200;
  set `0` only when the native dashboard truly needs to stay resident)

An `OPENROUTER_API_KEY` may remain available for deliberate, manual use. It is
not an automatic main-model fallback and must not be used for background helper
work. Hermes v0.20 managed scope pins the effective routes even if stale values
remain in the editable dashboard config:

- Main reasoning: `openai-codex / gpt-5.6-terra`, medium effort from the user config.
- Delegated subagents: `openai-codex / gpt-5.4-mini`, low effort, at most two at once.
- Auxiliary tasks: `openai-codex / gpt-5.4-mini`, except approval checks on `gpt-5.4`.
- Mixture-of-Agents: manual only, all OpenAI Codex models, 2,048-token synthesis cap.
- Automatic OpenRouter fallback: disabled until the OpenRouter key has a hard
  provider-side monthly spending limit approved by the owner.

## Secrets

- Never store a literal credential in an MCP URL.
- Store the Typefully credential as `TYPEFULLY_API_KEY` and configure the MCP
  server at `https://mcp.typefully.com/mcp` with an `Authorization` header set
  to `Bearer ${TYPEFULLY_API_KEY}`. The startup migration moves a
  pre-existing literal URL key into this protected form automatically.
- Rotate any credential that has previously appeared in the dashboard or logs.
- The setup API only returns its managed allowlist; unrelated `.env` values are
  preserved server-side and never serialized to the browser.

## Before deployment

1. Confirm startup created the private, one-time pre-upgrade backup under
   `/data/.hermes/backups/pre-v2026.8.3` without printing its contents.
2. Confirm the branch contains the currently deployed App Ops changes.
3. Run the unit suite and build the Docker image.
4. Verify the image reports Hermes `v0.20.0 (2026.8.3)`, Claude Code `2.1.221`,
   and SQLite `3.53.4` or newer.

## After deployment

Confirm all of the following before calling the deployment healthy:

- Health reports the gateway running.
- Main model is `gpt-5.6-terra` and provider is `openai-codex`.
- A normal Telegram message and Discord probe both complete exactly once.
- One fresh main-model request and one scheduled-job canary complete through
  OpenAI Codex OAuth. Neither may touch OpenRouter.
- Logs contain no raw session token, no repeated Raft dependency warning, no
  duplicate Telegram poller, and no gateway crash loop.
- The Typefully MCP configuration contains a placeholder, not a literal key.

The public `/health` response also becomes degraded when an enabled scheduled
job records a new failure after the current process started. The scheduled
GitHub workflow checks this endpoint every four hours, so a working web page
with broken model authentication no longer looks healthy for a week.

## Profiles and dashboards

- `default` is the always-on personal Hermes profile. Its gateway owns normal
  chats, channels, scheduled jobs, and the main dashboard.
- `app-ops` is an isolated, read-only worker profile for explicit Guiri founder
  filtering. It is started only for a matching handoff and intentionally has no
  permanently running gateway. `Gateway stopped` is normal for this profile.
- The wrapper setup page manages Railway-facing secrets and gateway lifecycle.
  The native Hermes dashboard manages chats, profiles, schedules, skills, and
  effective model information. Cost-sensitive model leaves are deployment
  managed and cannot be silently changed there.

## Subscription CLI providers

OAuth credentials on Victor's Mac are not visible inside Railway. The image
includes Claude Code so its Railway login can be completed and persisted under
the `/data` volume later. It is an explicit subagent lane, not an automatic
fallback. The old Gemini CLI consumer OAuth route is not used as a Hermes
provider; Google directs third-party agents to supported API/Vertex routes, and
the former individual Google AI Pro CLI route is not dependable for this setup.

### Guiri App Ops founder filter

- A Guiri Action Inbox item with `audience: victor` is eligible for Hermes
  analysis only when the enclosing payload explicitly sets
  `requires_hermes: true`.
- Routine COO or agent work must not set `requires_hermes`; Hermes is a founder
  filter, not the work coordinator.
- A successful handoff must return `202` with `status=accepted`, followed by
  `agent_started` and `agent_finished` records in
  `/outbox/app-ops-action-inbox`. HTTP `200` with `status=ignored` means no
  agent ran and must not be reported as completed processing.

### Guiri COO failover

Hermes runs a deterministic wrapper watchdog when
`HERMES_COO_WATCHDOG_ENABLED=true`. This recovery code is outside the model:
the Hermes agent never receives the Railway or Supabase service credentials.

Required protected variables:

- `RAILWAY_API_TOKEN`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

The target is hard-coded to the Guiri Ops Control Plane project, production
environment, and `guiri-ops-hub-cron` service. Environment variables cannot
redirect recovery to another Railway service.

The watchdog checks every 15 minutes by default. A fresh COO database heartbeat
means healthy. A stale heartbeat with a recent Railway run means the scheduler
is alive and Hermes does not restart it. Only a stale heartbeat plus stale
Railway activity starts recovery. Hermes redeploys the latest exact deployment,
waits for a newer database heartbeat, and allows at most two attempts per
rolling 24 hours with a 60-minute cooldown.

Successful recovery is silent. Hermes asks for founder attention only after the
safe recovery path is exhausted or no valid COO heartbeat has existed for 24
hours. Material attempts and verification results are appended locally under
`$HERMES_HOME/app-ops-action-inbox/coo-watchdog.jsonl` and written
idempotently to `ops_learning_events`. A temporary Supabase failure leaves the
local receipt queued for a later sync.

Keep the previous successful Railway deployment available for rollback. If the
gateway or channels regress, roll back the image first, then restore the three
backed-up Hermes files only if the upgrade changed their contents.

## Cost check

The native dashboard stops after its idle window and restarts on the next
authenticated dashboard request. The gateway, messaging channels, cron jobs,
webhooks, Honcho memory, and TTS remain online. Compare 24-hour and 48-hour
average memory and projected monthly spend after deployment; the target is
below $13.50/month to leave room under the $15 ceiling. The local image smoke
used 185.3 MiB with the gateway alone and 274.7 MiB with the dashboard open,
so idling removed about 89 MiB in that controlled test.
