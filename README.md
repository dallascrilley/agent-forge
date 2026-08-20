# agent-forge

**One JSON spec → idiomatic agent scaffolds for many runtimes.**

An "agent" here is the whole bundle: harness/runtime config, system prompt,
skills, MCP servers, plugins, trigger, and guardrails. You declare it once in
a versioned spec; agent-forge emits whatever each runtime considers native.

```
spec.json ──┬── --runtime pimono    → harness.json, SYSTEM.md, skills/, mcp.json,
            │                         guardrails.py, run.sh, launchd plist
            └── --runtime langgraph → my_agent/agent.py, langgraph.json,
                                      pyproject.toml, run.py, SCHEDULING.md
```

The generator is **stdlib-only Python 3.10+** — zero install for generation
itself.

## Status

Verified 2026-08-19 on macOS: clone → validated spec → smoke-checked pi-mono
bundle in ~1s; clone → LangGraph bundle → `pip install -e .` → live graph
build against a real MCP server in ~15s warm cache (a cold dependency
install adds a few minutes — still far under the 30-minute MVP gate).

Live-run verified 2026-08-20 with both committed example specs verbatim:
the generated pi-mono sitter ran a real sitting on its declared model (read
a pre-fetched HN brief, stayed inside its allowlisted side effect, wrote its
receipt); the generated LangGraph assistant answered from a real filesystem
MCP server with citations, its receipt mechanically recording every tool
call, and its guardrails wrapper refused a non-allowlisted `write_file`.

## Why not `langgraph new` / `npx eve init` / Oracle's Agent Spec?

Framework CLIs scaffold but accept no cross-runtime spec. Spec standards
(Oracle Agent Spec, open-agent-spec) standardize the declaration but couple
to one runtime/SDK. agent-forge is the missing middle: one canonical spec,
idiomatic output per runtime, and a guardrails contract (allowlisted side
effects, stop file, receipts) that every adapter enforces mechanically.

## Install

```bash
git clone https://github.com/dallascrilley/agent-forge.git
cd agent-forge
```

There is nothing to install. `forge/cli.py` runs on any python3.

## Quickstart

### The 60-second version

```bash
python3 forge/cli.py validate examples/sitter-spec.json
python3 forge/cli.py generate examples/sitter-spec.json --runtime pimono --out /tmp/hn-sitter
bash /tmp/hn-sitter/run.sh --dry-run   # prints the pi argv
```

### With the skill (the intended way)

`skills/agent-forge/` is an installable agent skill. Copy or symlink it into
your agent's skills directory (e.g. `~/.agents/skills/agent-forge`), then
just ask: *"make me an agent that checks the HN front page daily and
summarizes AI posts."* The skill interviews you, writes the spec to disk,
validates, generates, and smoke-checks the bundle.

You never have to hand-author JSON — but the spec is the durable artifact,
and everything in this repo works without the skill.

### Running a generated LangGraph agent

```bash
python3 forge/cli.py generate examples/assistant-spec.json --runtime langgraph --out my-assistant
cd my-assistant
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env   # fill in the key for your model provider
python3 run.py "what docs do we have?"
langgraph dev          # optional: local dev server with graph UI
```

## The guardrails contract

Every generated bundle carries the spec's `guardrails` section as *code*:

- **stop file** — `touch <name>.stop` pauses the agent; next run writes a
  `paused` receipt and exits
- **allowed_tools** — which tools may be invoked at all
- **allowed_side_effects + max_actions** — what the agent may change, and a
  per-run budget; enforcement is a code call site, not a prompt plea
- **receipt** — every run ends with a JSON receipt: verdict, actions, note
- **llm_optional** — a run with nothing to do completes without the model

Removing enforcement requires deleting call sites — an explicit act, not an
omission.

## The spec

v1 reference: [`docs/spec-v1.md`](docs/spec-v1.md). Machine-readable schema:
[`schema/agent-spec.schema.json`](schema/agent-spec.schema.json). Examples:
[`examples/`](examples/).

## Runtimes

| Runtime | Status | Output shape |
|---|---|---|
| pi-mono | ✅ | harness folder (`harness.json`, `SYSTEM.md`, `run.sh`, …) |
| LangGraph | ✅ | runnable Python project (`langgraph.json`, `create_agent` graph) |
| eve (Vercel) | planned | directory agent (`agent.ts`, `instructions.md`, `skills/`) |
| hermes (Nous) | planned | SOUL.md + SKILL.md skills + cron |

Writing an adapter is a single Python module — see
[`docs/adapters.md`](docs/adapters.md).

## Tests

```bash
pip install -r requirements-dev.txt
python3 -m pytest -q
```

Adapter output is golden-file tested; after an intended generator change,
re-bless with `python3 tests/bless_golden.py`.

## License

MIT
