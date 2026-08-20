# Agent Spec v1 — field reference

The spec is the durable artifact. Every producer (the agent-forge skill, a
future wizard CLI, your editor) writes it; every adapter consumes it. The
validator in `forge/spec.py` is the authority;
`schema/agent-spec.schema.json` mirrors it for editor completion.

## Top level

| Field | Type | Required | Meaning |
|---|---|---|---|
| `spec_version` | int | yes | Must be `1`. |
| `name` | string | yes | Kebab-case slug (`^[a-z][a-z0-9-]*$`). Used in file names, receipt defaults, scheduler labels. |
| `purpose` | string | yes | What the agent is for, in operator language. Rendered prominently into every system prompt. |
| `model` | string | yes | Runtime-native model id, e.g. `openai-codex/gpt-5.4-mini`. |
| `model_overrides` | object | no | Per-runtime model ids (`{"langgraph": "openai/gpt-5-mini"}`) that replace `model` for that runtime. Use when runtimes have different native providers — e.g. Codex OAuth (`openai-codex/…`) works in the pi CLI but has no LangChain integration. |
| `runtimes` | array | yes | Non-empty, unique subset of `["pimono", "langgraph"]`. |
| `description` | string | no | One-line human summary. |
| `system_prompt` | object | no | `{"inline": "…md…"}` or `{"file": "path/rel/to/spec"}`. |
| `skills` | array | no | `[{name, description, body\|file}]` — one SKILL.md dir per entry. |
| `mcp_servers` | object | no | `{name: {command, args?, env?}}` (stdio) xor `{name: {url, headers?}}` (remote). |
| `plugins` | array | no | `[{name, runtimes?, config?}]` — runtime-hinted freeform. Adapters ignore plugins not hinted for them. |
| `trigger` | object | no | `{"type": "manual"}` (default) or `{"type": "cron", "schedule": "M H DoM Mon DoW"}`. |
| `guardrails` | object | no | See below. |

## guardrails

| Field | Type | Default | Meaning |
|---|---|---|---|
| `allowed_tools` | string[] | all tools | Tool names the agent may invoke at all. Exact match, or prefix when the pattern ends in `/`. |
| `allowed_side_effects` | string[] | `[]` (read-only) | Side-effect action names the agent may perform, budgeted by `max_actions`. |
| `stop_file` | path | `<name>.stop` | Present file ⇒ agent pauses: writes a `paused` receipt, exits. |
| `receipt.path` | path | `receipts/last.json` | Where the run receipt is written. |
| `llm_optional` | bool | `true` | A run with nothing to do must complete without invoking the model. pi-mono cron sitters enforce this in `run.sh` (empty gather → quiet receipt, no `pi`). Sitters also take a 12-minute overlap lock and a 180s pi timeout (`SIT_LOCK_SEC` / `SIT_TIMEOUT_SEC`). pi-mono `--tools` is `read` when there are no side effects, `read,bash` for sitters (writes go through `guardrails.py put`), otherwise `read,bash,write`. |
| `max_actions` | int ≥ 0 | `3` | Per-run cap on side-effecting actions. |

## Receipt schema

Every run of a generated agent ends with one JSON receipt:

```json
{
  "verdict": "acted | quiet | paused | blocked",
  "actions": ["write-file:inbox/today.md"],
  "tool_calls": ["list_directory", "read_file"],
  "refused": ["write_file"],
  "note": "one line",
  "ts": 1787174525
}
```

- `actions` — side-effecting actions taken (allowlisted, budgeted).
- `tool_calls` / `refused` — every tool invocation and every guardrails
  refusal. Recorded **mechanically** by runtimes that wrap tools (LangGraph).
  pi-mono receipts are model-reported via `guardrails.py write-receipt` and
  carry `verdict`/`actions`/`note`/`ts`.

## Validation rules that bite

- Unknown top-level keys are rejected (`additionalProperties: false`).
- cron trigger requires `schedule`; manual trigger forbids it.
- MCP server: `command` xor `url`.
- Skill: `body` xor `file`.
- Duplicate skill names and duplicate runtimes are rejected.
- Validation collects **all** problems and reports them with JSON paths.

## Versioning

`spec_version` exists from day one. v1 is additive-only going forward;
breaking changes ship as v2 with both validators side by side.
