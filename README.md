# agent-forge

**One JSON spec → idiomatic agent scaffolds for many runtimes.**

An "agent" here is the whole bundle: harness/runtime config, system prompt,
skills, MCP servers, plugins, trigger, and guardrails. You declare it once in
a versioned spec; agent-forge emits whatever each runtime considers native —
a pi-mono harness folder, a LangGraph project, and (soon) eve and hermes
layouts.

```
spec.json ──┬── runtime: pimono    → harness.json, SYSTEM.md, skills/, guardrails.py, run.sh, launchd plist
            └── runtime: langgraph → my_agent/agent.py, langgraph.json, pyproject.toml, guardrails wrapper
```

## Status

Early. The spec is v1 and two adapters work: **pi-mono** and **LangGraph**.
Docs are being filled in; see `docs/plans/` for the roadmap.

## Quickstart

```bash
python3 forge/cli.py validate examples/sitter-spec.json
python3 forge/cli.py generate examples/sitter-spec.json --runtime pimono --out /tmp/my-agent
```

No dependencies: the generator is stdlib-only Python 3.10+.

## The spec

See `schema/agent-spec.schema.json` and `examples/`. Full field reference:
`docs/spec-v1.md` (being written).

## The skill

`skills/agent-forge/` is an installable agent skill that interviews you,
writes the spec, and runs the generator — you never have to hand-author JSON.

## Tests

```bash
pip install -r requirements-dev.txt
python3 -m pytest -q
```

## License

MIT
