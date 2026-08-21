# Writing an adapter

An adapter is one Python module in `forge/adapters/` exposing:

```python
def generate(spec, out_dir) -> list[str]:
    """Emit a runnable agent bundle for this runtime. Return relative paths."""
```

Use `forge.adapters.common.Emitter` to write files (it creates parents,
tracks paths, sets the executable bit).

## The contract

1. **Register the runtime name** in `forge/__init__.py` (`KNOWN_RUNTIMES`)
   and in `schema/agent-spec.schema.json` (`runtimes` enum, plugins
   `runtimes` enum).
2. **Emit the idiomatic shape.** Config-native runtimes (eve:
   `agent.ts`/`instructions.md`/`skills/`/`schedules/`; hermes: `SOUL.md` +
   SKILL.md dirs + cron) get config trees. Code-native runtimes (LangGraph)
   get minimal runnable code. Do not force one runtime's shape onto another.
   pi-mono MCP: `mcp.json` plus a generated `mcp.ts` loaded with
   `--extension mcp.ts` under `--no-extensions` (no `--mcp-config` on pi).
   LangGraph skills: `skills/<name>/SKILL.md` plus a name/description listing
   in the system prompt (no native LangGraph skill loader).
3. **Enforce the guardrails section mechanically.** Every adapter must emit:
   stop-file handling, the `allowed_tools`/`allowed_side_effects` gates, a
   per-run action budget, and the receipt write. Enforcement must be a call
   site in generated code, so removing it is an explicit act.
4. **Fail soft on unknown plugins** not hinted for your runtime; ignore them.
5. **No import-time side effects.** Importing generated code must not open
   network connections or spawn processes (the LangGraph adapter uses an
   async graph factory for exactly this reason).
6. **Golden tests.** Add fixtures to `tests/golden/<runtime>/`, extend
   `tests/bless_golden.py`, and write `tests/test_adapter_<runtime>.py`
   asserting the guardrails call sites exist in the output.
7. **No private facts.** `tests/test_no_private_facts.py` scans the repo;
   generated output must not contain machine-specific absolute paths.

## Implemented and deferred runtimes

- **eve** (github.com/vercel/eve): implemented by `forge/adapters/eve.py`.
  `system_prompt` maps to `agent/instructions.md`, `skills` to
  `agent/skills/*.md`, `trigger.cron` to `agent/schedules/`, and `mcp_servers`
  to `agent/tools/mcp-servers.json` for external-tool wiring.
- **hermes** (github.com/NousResearch/hermes-agent) remains deferred:
  `system_prompt` → `SOUL.md`, `skills` → SKILL.md dirs (agentskills.io
  standard), MCP and cron are first-class.

Eve is the first extra runtime; add another adapter only after confirming its
current on-disk layout and adding golden fixtures plus guardrail call-site
tests.
