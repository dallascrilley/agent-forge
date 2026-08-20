---
date: 2026-08-19
origin: agent-bundle-generator requirements brainstorm (2026-08-19)
---

# Agent bundle generator (agent-forge) — approved plan

**Summary:** Public repo: a versioned canonical JSON agent spec + stdlib-Python
generator core + two adapters (pi-mono, LangGraph) + an installable agent
skill that interviews and emits. An "agent" = harness/runtime config + system
prompt + skills + MCP servers + plugins. Guardrails (allowlisted side effects,
stop file, receipts) are a spec section each adapter enforces idiomatically.
MVP gate: spec-to-running-agent ≤30 min on a fresh machine for both adapters.

## Requirements (from the brainstorm, stable R-numbers)

- R1. Canonical versioned spec (`spec_version: 1`).
- R2. Agent-skill MVP producer; spec usable without the skill.
- R3. pi-mono + LangGraph adapters, each emitting the idiomatic shape.
- R4. Guardrails travel; removal from generated output requires an explicit act.
- R5. Zero private facts (enforced by `tests/test_no_private_facts.py`).
- R6. Public repo with stranger-proof docs (repo created private; flipped in U5).
- R7. ≤30-min time-to-working-agent gate, demonstrated per adapter.

## Key technical decisions

- **Generator core is Python, stdlib-only.** Zero-install for skill-driven use.
  Hand-rolled validator is the authority; `schema/agent-spec.schema.json` is
  the editor aid, kept in agreement by a dual-validation test.
- **`spec_version: 1` from day one** — versioning is cheap now, expensive later.
- **pi-mono adapter** emits the generalized harness vocabulary: `harness.json`,
  `SYSTEM.md`, `skills/`, `mcp.json` (or a NOTE when unsupported), `config.json`,
  `guardrails.py` (fresh generic code), `run.sh`, launchd plist on cron triggers.
- **LangGraph adapter** emits minimal-runnable: single-file `create_agent`
  graph, `MultiServerMCPClient` only when MCP servers exist, `langgraph.json`,
  `pyproject.toml`, `.env.example`, `run.py`, `SCHEDULING.md` (no LangSmith
  dependency — system cron / launchd / GH Actions instead).
- **Guardrails are generated code call sites**, not prose: adapters emit a
  guardrails helper the agent code imports; tests assert the call site exists.
- **Skill lives in-repo** at `skills/agent-forge/`; registry publication deferred.
- **Golden-file tests** per adapter; no live LLM calls in tests.

## Units

- U1. Repo + spec schema + validator + generator skeleton (this commit).
- U2. pi-mono adapter + example agent.
- U3. LangGraph adapter + example agent.
- U4. Agent skill (interview → spec → emit).
- U5. Public readiness + R7 timing gate, then flip public.

## Deferred

eve and hermes adapters (research 2026-08-19 shows both map cleanly onto the
v1 spec vocabulary; `docs/adapters.md` in U5 is their on-ramp), standalone
wizard CLI, skills-registry publication, spec conformance suite, npm/pypi
distribution.
