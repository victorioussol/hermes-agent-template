# Hermes Railway reliability runbook

This wrapper intentionally keeps the Railway deployment reproducible and the
main inference route explicit.

## Required production settings

- `HERMES_MODEL_PROVIDER=openai-codex`
- `LLM_MODEL=gpt-5.6-terra`
- `ADMIN_PASSWORD` set to a strong value
- `HERMES_DASHBOARD_IDLE_SECONDS=300` in production (the code default is 1200;
  set `0` only when the native dashboard truly needs to stay resident)

An `OPENROUTER_API_KEY` may remain available only after that exact key reports a
USD 5 limit with a monthly reset. Hermes v0.20 managed scope pins the effective
routes even if stale values remain in the editable dashboard config:

- Main reasoning: `openai-codex / gpt-5.6-terra`, medium effort from the user config.
- Automatic main fallback: `openrouter / deepseek/deepseek-v4-flash-0731`.
- Delegated subagents and routine helpers: `openai-codex / gpt-5.6-terra`,
  low effort, at most two at once, one level deep, and 30 iterations.
- Vision: `openai-codex / gpt-5.6-terra`; approval checks use Terra at medium effort.
- Mixture-of-Agents: manual only, Flash plus Terra references, Sol synthesis,
  and a 1,536-token synthesis cap.
- DeepSeek Flash is otherwise limited to approved extraction, normalization,
  and deduplication jobs. DeepSeek Pro is limited to an independent weekly or
  monthly critic that can propose changes but cannot apply them.
- GPT-5.6 Luna, Qwen, GLM, and Kimi remain evaluation-only until fixed canaries
  show a material benefit. GPT, Claude, and Gemini never run through OpenRouter.

## Secrets

- Never store a literal credential in an MCP URL.
- Typefully is retired. Startup removes its MCP entry and any persisted
  credential; remove the matching Railway variable as part of deployment.
- Rotate any credential that has previously appeared in the dashboard or logs.
- The setup API only returns its managed allowlist; unrelated `.env` values are
  preserved server-side and never serialized to the browser.

## Before deployment

1. Confirm startup created the private, one-time pre-upgrade backup under
   `/data/.hermes/backups/pre-v2026.8.3` without printing its contents.
2. Read back the exact OpenRouter key metadata and require `limit=5` plus
   `limit_reset=monthly`. Do not deploy the managed fallback before both match.
3. Confirm the branch contains the currently deployed App Ops changes.
4. Run the unit suite and build the Docker image.
5. Verify the image reports Hermes `v0.20.0 (2026.8.3)` and SQLite `3.53.4`
   or newer.
6. Validate `hermes-continuous-improvement` and confirm the image exposes it.

## After deployment

Confirm all of the following before calling the deployment healthy:

- Health reports the gateway running.
- Main model is `gpt-5.6-terra` and provider is `openai-codex`.
- A normal Telegram message and Discord probe both complete exactly once.
- One fresh main-model request completes through OpenAI Codex OAuth.
- One bounded DeepSeek V4 Flash auxiliary call completes through OpenRouter,
  and the key still reports a USD 5 monthly limit afterwards.
- One model-backed scheduled-job canary and one script-only watchdog canary
  complete. Verify the expected provider for the model-backed job.
- Logs contain no raw session token, no repeated Raft dependency warning, no
  duplicate Telegram poller, and no gateway crash loop.
- Typefully is absent from MCP servers, Railway variables, and the setup page.
- The budget guard reports `limit_usd=5` and `limit_reset=monthly` without
  printing a credential.
- Curator is prune-only, keeps backups, and does not prune bundled skills.

The public `/health` response also becomes degraded when an enabled scheduled
job records a new failure after the current process started. It exposes only a
safe failure category, never the job, prompt, provider response, or credential.
It also reports whether the DeepSeek fallback has a fresh receipt proving that
the OpenRouter key still has its exact USD 5 monthly limit.

The scheduled GitHub workflow checks this endpoint every 15 minutes and sends
alerts through the existing Telegram bot. It requires encrypted repository
secrets named `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and `RAILWAY_TOKEN`.
The Railway token must be scoped only to this project's production environment.

Recovery is deliberately bounded:

- Codex authentication or model availability failure uses Hermes' existing
  request-level DeepSeek Flash fallback. A service restart is not attempted,
  because it cannot renew an expired Codex login.
- An unreachable service, stopped gateway, or stopped configured watchdog gets
  exactly one Railway redeploy for a new incident.
- An arbitrary scheduled-task failure or missing configuration alerts without
  a restart, so recovery does not hide the real problem.
- A second failed scheduled check sends the manual-intervention alert. Later
  failures stay silent until health recovers, preventing restart loops and
  Telegram spam.
- Recovery sends a separate Telegram confirmation. Terra always remains the
  primary model; the recovery workflow never changes model configuration.

The workflow's manual `alert-test` mode verifies the current Telegram bot.
`recovery-test` intentionally performs one redeploy and should be used only as
an explicit end-to-end canary. Manual tests do not affect scheduled incident
history.

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

## Excluded subscription CLI providers

OAuth credentials on Victor's Mac are not visible inside Railway. Gemini CLI
and Claude Code are intentionally out of scope for this deployment and are not
installed in the image. Revisit either only as a separate, measured provider
project; neither is an automatic fallback.

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
webhooks, built-in memory, and TTS remain online. External memory remains
optional until its provider canary is healthy. Compare 24-hour and 48-hour
average memory and projected monthly spend after deployment; the target is
below $13.50/month to leave room under the $15 ceiling. The local image smoke
used 185.3 MiB with the gateway alone and 274.7 MiB with the dashboard open,
so idling removed about 89 MiB in that controlled test.

OpenRouter has a separate USD 5 monthly hard ceiling. Run
`/data/.hermes/scripts/openrouter_budget_guard.py` on a schedule. The image
refreshes this persistent copy at every boot. The script always writes the
latest receipt to `/data/.hermes/openrouter-budget.json`, but prints only
non-`ok` results so a healthy 30-minute check stays silent. Its thresholds are
USD 2.50 notice, USD 4 warning, and USD 4.75 critical. Optional model
evaluations stop at warning. Normal expected model spend is under USD
1.25/month, but the provider-side limit is the real protection.
