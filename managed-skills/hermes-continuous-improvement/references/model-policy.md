# Hermes model policy

## Default routes

- Main agent: `openai-codex / gpt-5.6-terra`, medium reasoning.
- Routine delegation and auxiliary work: `openai-codex / gpt-5.6-terra`, low reasoning, maximum two children and one nesting level.
- High-consequence synthesis: `openai-codex / gpt-5.6-sol`, explicit use only.
- Main emergency fallback: `openrouter / deepseek/deepseek-v4-flash-0731`, only while the exact key has a USD 5 monthly provider limit.

Do not route GPT, Claude, or Gemini models through OpenRouter. Do not use legacy GPT-5.4 routes. Treat GPT-5.6 Luna as unavailable until a production Codex OAuth canary proves model discovery, tool use, delegation, and completion.

## Narrow paid routes

- DeepSeek V4 Flash: large text extraction, normalization, deduplication, and emergency fallback.
- DeepSeek V4 Pro: one weekly or monthly independent critic pass; proposals only.
- Qwen3.7 Flash: visual or multimodal challenger evaluation only.
- GLM-4.7 Flash, GLM-5.2, and Kimi K2.6: fixed evaluation challengers only.

All OpenRouter traffic shares the USD 5 monthly ceiling. Report at USD 2.50, warn at USD 4.00, and treat USD 4.75 as critical. Suspend optional challenger work at warning level.

## Promotion rule

Promote a route only when it passes all critical cases, produces no privacy or tool regressions, and either improves verified task quality materially or lowers token/cost usage by at least 20 percent without a quality loss. Require three successful canaries before recurring use.
