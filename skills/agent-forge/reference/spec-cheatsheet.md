# Spec v1 cheatsheet

Minimal spec:

```json
{
  "spec_version": 1,
  "name": "my-agent",
  "purpose": "What it does, in operator language.",
  "model": "anthropic/claude-sonnet-4-5",
  "runtimes": ["pimono"]
}
```

Everything else is optional; defaults are safe.

| Field | Shape | Default |
|---|---|---|
| `description` | string | `""` |
| `system_prompt` | `{"inline": md}` or `{"file": path}` | none (purpose only) |
| `skills` | `[{name, description, body\|file}]` | `[]` |
| `mcp_servers` | `{name: {command, args?, env?} \| {url, headers?}}` | `{}` |
| `plugins` | `[{name, runtimes?, config?}]` | `[]` |
| `model_overrides` | `{runtime: model-id}` | `{}` — `model` applies to all runtimes |
| `trigger` | `{type: manual\|cron, schedule?}` | `manual` |
| `guardrails.allowed_tools` | `[tool-name patterns]` (prefix match when ending `/`) | all tools |
| `guardrails.allowed_side_effects` | `[action patterns]` | none (read-only) |
| `guardrails.stop_file` | path | `<name>.stop` |
| `guardrails.receipt.path` | path | `receipts/last.json` |
| `guardrails.llm_optional` | bool | `true` (quiet runs skip the model) |
| `guardrails.max_actions` | int ≥ 0 | `3` |

Rules that bite:

- `name` and skill names: `^[a-z][a-z0-9-]*$`.
- cron `trigger` requires `schedule`; manual must not have one.
- MCP server: `command` xor `url`.
- skill: `body` xor `file`.
- `runtimes` entries: `pimono`, `langgraph`. Generation targets one runtime
  per `--runtime` flag and it must be listed here.

Runtime notes:

- **pimono**: emits `harness.json`, `SYSTEM.md`, `skills/`, `mcp.json`,
  `config.json`, `guardrails.py`, `run.sh` (`--dry-run` prints argv),
  launchd plist when cron. Model id is passed to `pi --model` verbatim;
  `openai-codex/...` models use Codex OAuth (no API key).
- **langgraph**: emits a minimal project (`my_agent/agent.py`,
  `langgraph.json`, `pyproject.toml`, `.env.example`, `run.py`,
  `SCHEDULING.md`). Model id `provider/model` becomes `provider:model` for
  `init_chat_model`; the provider package is inferred into pyproject.
  `openai-codex` has no LangChain integration — set
  `model_overrides.langgraph` (the generator errors clearly if you forget).
  With MCP servers the graph is an async factory (run-time connect).
