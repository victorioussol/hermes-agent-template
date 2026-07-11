# Hermes Railway reliability runbook

This wrapper intentionally keeps the Railway deployment reproducible and the
main inference route explicit.

## Required production settings

- `HERMES_MODEL_PROVIDER=openai-codex`
- `LLM_MODEL=gpt-5.4`
- `ADMIN_PASSWORD` set to a strong value
- `HERMES_DASHBOARD_IDLE_SECONDS=300` in production (the code default is 1200;
  set `0` only when the native dashboard truly needs to stay resident)

An `OPENROUTER_API_KEY` may remain available for optional models. It must not be
used for the main `gpt-5.4` route. On each gateway start, the wrapper preserves
or reapplies the explicit `openai-codex` provider and removes stale inline model
endpoints that could override it.

## Secrets

- Never store a literal credential in an MCP URL.
- Store the Typefully credential as `TYPEFULLY_API_KEY` and configure the MCP
  server at `https://mcp.typefully.com/mcp` with an `Authorization` header set
  to `Bearer ${TYPEFULLY_API_KEY}`. The v0.18.2 startup migration moves a
  pre-existing literal URL key into this protected form automatically.
- Rotate any credential that has previously appeared in the dashboard or logs.
- The setup API only returns its managed allowlist; unrelated `.env` values are
  preserved server-side and never serialized to the browser.

## Before deployment

1. Confirm startup created the private, one-time pre-upgrade backup under
   `/data/.hermes/backups/pre-v2026.7.7.2` without printing its contents.
2. Confirm the branch contains the currently deployed App Ops changes.
3. Run the unit suite and build the Docker image.
4. Verify the image reports Hermes `v0.18.2 (2026.7.7.2)`.

## After deployment

Confirm all of the following before calling the deployment healthy:

- Health reports the gateway running.
- Main model is `gpt-5.4` and provider is `openai-codex`.
- A normal Telegram message and Discord probe both complete exactly once.
- One cron canary completes without an OpenRouter 429.
- Logs contain no raw session token, no repeated Raft dependency warning, no
  duplicate Telegram poller, and no gateway crash loop.
- The Typefully MCP configuration contains a placeholder, not a literal key.

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
