# Repository Guidelines

One versioned JSON spec → idiomatic, runnable agent bundles for multiple runtimes.

## Project Overview

agent-forge is a **code generator**, not a runtime. You declare an agent once
(harness config, system prompt, skills, MCP servers, plugins, trigger,
guardrails) in a versioned JSON spec; adapters emit whatever each target
runtime considers native.

| Runtime | Key | Output shape |
|---|---|---|
| pi-mono | `pimono` | harness folder: `harness.json`, `SYSTEM.md`, `run.sh`, `guardrails.py`, `mcp.json`+`mcp.ts`, `launchd/*.plist` |
| LangGraph | `langgraph` | runnable Python project: `my_agent/agent.py`, `langgraph.json`, `pyproject.toml`, `run.py` |
| eve (Vercel) | `eve` | filesystem agent: `agent/agent.ts`, `agent/instructions.md`, `agent/skills/`, `agent/schedules/` |

The generator core is **stdlib-only Python 3.10+** — zero runtime dependencies,
zero install. A planned `hermes` runtime exists in docs but is **not**
implemented and not in `KNOWN_RUNTIMES`.

Package constants live in `forge/__init__.py`: `__version__ = "0.1.0"`,
`SPEC_VERSION = 1`, `KNOWN_RUNTIMES = ("pimono", "langgraph", "eve")`.

## Architecture & Data Flow

Five layers, all synchronous, no DI container, no adapter registry.

```
CLI                Spec                          Adapters                 Output
python3 -m forge → forge/spec.py            →    forge/adapters/*.py  →   bundle on disk
 (cli.main)        load() + validate()           generate(spec, dir)      guardrails as
                   → normalized Spec             via common.Emitter       call sites
                   raises SpecError(problems)
```

1. **Front ends** — `python3 -m forge` (`forge/__main__.py` → `forge.cli.main`),
   or direct execution `python3 forge/cli.py` via the `sys.path`/`__package__`
   shim at `forge/cli.py:17-19`.
2. **Spec layer** — `forge/spec.py`. `load(path)` → `validate(data, spec_dir)` →
   normalized `Spec` dataclass with defaults applied in `__post_init__`.
   The **hand-rolled validator is the authority**; `schema/agent-spec.schema.json`
   is an editor/documentation aid kept in agreement by dual-validation tests in
   `tests/test_spec.py`. Validation **collects every problem** and raises one
   `SpecError(problems)` carrying all of them.
3. **Dispatch** — `forge/cli.py:_cmd_generate` uses a hardcoded `if/elif` import
   of the adapter module. `args.runtime` must appear in `spec.runtimes` or it
   raises `AdapterError`. There is no plugin registry; adding a runtime means
   editing code (see below).
4. **Adapter layer** — one module per runtime in `forge/adapters/`, each exposing
   `generate(spec, out_dir) -> list[str]`, with private `_<file>_(spec)` helpers
   returning whole-file strings. All file writes go through
   `forge/adapters/common.py:Emitter` (creates parents, tracks relative paths,
   optional `chmod 0o755`).
5. **Emitted bundles** — the guardrails contract is the cross-cutting concern:
   every adapter must emit stop-file handling, `allowed_tools` /
   `allowed_side_effects` gates, a per-run `max_actions` budget, and a receipt
   write **as call sites in generated code**, so removing enforcement is an
   explicit act rather than a silent omission. Each adapter re-implements this
   in the target language: `_GUARDRAILS_PY` embedded in both `pimono.py` and
   `langgraph.py`, `_guardrails_ts` in `eve.py`.

**Concurrency:** the generator itself is single-threaded and blocking — no
asyncio, threads, or locks in `forge/`. Asynchrony exists only in *emitted*
code where the host runtime requires it (LangGraph's `async def graph()`
factory, eve's `async function runGuarded`, pi's generated `mcp.ts`).

**Example flow (pi-mono cron sitter):** spec `trigger.cron` +
`guardrails.llm_optional` → `_is_sit(spec)` true → adapter emits `gatherer.py`
and a launchd plist → `run.sh` takes an overlap lock, runs `gatherer.py`
(writes `brief.md` / `allow.json` / `llm.txt`), skips `pi` entirely and writes a
`quiet` receipt when gathering found nothing, otherwise calls
`guardrails.py run-pi` (which strips `GH_TOKEN`/`GITHUB_TOKEN` and enforces
`SIT_TIMEOUT_SEC`).

**Example flow (LangGraph + MCP):** `spec.mcp_servers` non-empty → `_agent_py`
emits an `async def graph()` factory instead of a module-level graph, builds a
`MultiServerMCPClient` at runtime, and wraps every tool with
`GUARDRAILS.wrap()` (refusals are returned to the model as text, not raised).

## Key Directories

| Path | Purpose |
|---|---|
| `forge/` | Generator package: `cli.py`, `spec.py`, `errors.py`, `adapters/` |
| `forge/adapters/` | One module per runtime + `common.py` (`Emitter`) |
| `schema/agent-spec.schema.json` | JSON Schema draft 2020-12 mirror of the spec (not the authority) |
| `examples/` | Committed spec inputs + generated example trees that must stay in lockstep with golden output |
| `tests/` | pytest suite; `tests/golden/<runtime>/<name>/` are checked-in expected trees |
| `skills/agent-forge/` | Installable agent skill (interview → spec → validate → generate → smoke-check) |
| `docs/` | `spec-v1.md` (field reference), `adapters.md` (adapter on-ramp), `plans/` |
| `.beads/` | Beads issue tracker state (embedded Dolt). Never commit `.beads/embeddeddolt/`, `.beads/dolt/`, or `.beads/redirect` |

`forge/orchestrator/` is an **empty, unreferenced directory** — not part of the
architecture.

## Development Commands

Generator (no install step — stdlib only; run from the repo root):

```bash
python3 -m forge validate examples/sitter-spec.json
python3 -m forge generate examples/sitter-spec.json --runtime pimono --out /tmp/hn-sitter
python3 -m forge new --name daily-summarizer --purpose "Summarize the daily inbox." \
  --model openai/gpt-5-mini --runtime pimono --out /tmp/daily-summarizer-spec.json
python3 -m forge --version
```

`new` flags: `--name --purpose --model --runtime` (repeatable) `--out`, plus
optional `--cron`, `--mcp NAME=COMMAND|NAME=URL` (repeatable),
`--side-effect` (repeatable), `--system-prompt`. Exit code is `0` on success and
`1` for `SpecError`/`AdapterError` (message on stderr).

Tests and lint:

```bash
pip install -r requirements-dev.txt   # pytest>=8, jsonschema>=4
python3 -m pytest -q
ruff check forge tests
```

Use the `python3 -m pytest` form from the repo root — there is no root
`pyproject.toml`/`conftest.py`, so `forge` is imported from the CWD.

After an **intended** generator change:

```bash
python3 tests/bless_golden.py          # re-bless all tests/golden/<runtime>/ trees
# then regenerate the affected committed example tree, e.g.
python3 -m forge generate examples/assistant-spec.json --runtime langgraph --out examples/assistant-langgraph
python3 forge/cli.py generate examples/sitter-spec.json --runtime pimono --out examples/sitter-pimono
```

Running generated bundles:

```bash
# pimono
bash /tmp/hn-sitter/run.sh --dry-run          # print the pi argv, execute nothing
bash /tmp/hn-sitter/run.sh                    # one sitting
touch hn-ai-sitter.stop                       # pause; rm to resume
SIT_LOCK_SEC=… SIT_TIMEOUT_SEC=… SITTER_ITEMS=/path/roster.json ./run.sh

# langgraph
cd my-assistant && python3 -m venv .venv && source .venv/bin/activate
pip install -e . && cp .env.example .env
python3 run.py "what docs do we have?"
langgraph dev

# eve
npm install && npm run dev
```

### Issue tracking

Three surfaces, documented in `CONTRIBUTING.md` and `.beads/README.md`:
**Beads (`bd`) is the execution source of truth**, Linear is the human product
board, GitHub Issues is public intake.

- `bd ready --json`, `bd update <id> --claim --json`, `bd close <id> --reason "…" --json`.
- Always pass `--json`; never use interactive `bd edit`.
- Do **not** create or update Linear issues from coding sessions. To surface an
  outcome, label a bead: `bd update <id> --add-label promote:linear`.
- Never commit `.beads/embeddeddolt/`, `.beads/dolt/`, or `.beads/redirect`;
  issue data syncs with `bd dolt`, not Git.

## Code Conventions & Common Patterns

- **Formatting/lint** — `ruff.toml`: `target-version = "py310"`,
  `extend-exclude = ["tests/golden"]`, `[lint] select = ["E4", "E7", "E9", "F"]`.
  No formatter, import-sorter, or type checker is configured.
- **Python style** — `from __future__ import annotations` in every module;
  builtin generics (`list[str]`, `str | None`); light annotations; `@dataclass`
  for the `Spec` IR. No `ABC`/`Protocol` for adapters — the contract is
  duck-typed `generate(spec, out_dir)`.
- **Naming** — `snake_case` modules and functions, `_leading_underscore` for
  private helpers; generated-template constants are `_UPPER_SNAKE` triple-quoted
  string literals (`_GUARDRAILS_PY`, `_MCP_TS`); agent/skill names are
  kebab-case matching `^[a-z][a-z0-9-]*$`.
- **Code emission** — whole-file f-string/triple-quoted templates passed to
  `Emitter.write(rel, content, executable=False)`. No templating engine. JSON is
  emitted as `json.dumps(..., indent=2) + "\n"`. Adapters never touch the
  filesystem directly.
- **Error handling** — only two error types, both in `forge/errors.py`:
  `SpecError` (accumulates all problems; never fail on the first) and
  `AdapterError` (adapter could not generate for a valid spec, e.g.
  `openai-codex*` model on LangGraph without `model_overrides`, or cron too
  dense for launchd). Adapters raise; the CLI catches both once in
  `forge.cli.main` and returns `1`. Never swallow or downgrade to a warning.
- **CLI pattern** — argparse with `add_subparsers(dest="verb", required=True)`,
  each subparser binding `set_defaults(fn=...)`, dispatched in `main`. Generated
  micro-CLIs (e.g. `guardrails.py`) deliberately use hand-rolled `sys.argv`
  dispatch instead, to stay dependency-free.
- **Generated output is never hand-edited** — change the spec and regenerate,
  then re-bless golden and regenerate the matching `examples/` tree. Adapters
  must fail soft on plugins not hinted for their runtime, and must have **no
  import-time side effects** in emitted code.
- **Portability of output** — generated bundles must contain no machine-specific
  absolute paths or personal facts; launchd templates use an `__INSTALL_DIR__`
  placeholder. Enforced by `tests/test_no_private_facts.py`.

### Adding a runtime

Per `docs/adapters.md` and `forge/adapters/__init__.py`:

1. Add the name to `KNOWN_RUNTIMES` in `forge/__init__.py` and to the `runtimes`
   enums in `schema/agent-spec.schema.json`.
2. Add `forge/adapters/<runtime>.py` with `generate(spec, out_dir) -> list[str]`
   and a matching `elif` in `forge/cli.py:_cmd_generate`.
3. Enforce the full guardrails contract mechanically in the emitted code.
4. Add fixtures under `tests/golden/<runtime>/`, an entry in
   `tests/bless_golden.py:TARGETS`, and `tests/test_adapter_<runtime>.py`
   asserting the guardrail call sites exist in the generated output.

## Important Files

| File | Why it matters |
|---|---|
| `forge/__init__.py` | Version, `SPEC_VERSION`, `KNOWN_RUNTIMES` — the runtime registry |
| `forge/cli.py` | Entire command surface: `validate`, `generate`, `new`; hardcoded adapter dispatch |
| `forge/spec.py` | `Spec` dataclass, `load()`, `validate()` — the **validation authority**; guardrail defaults |
| `forge/errors.py` | `SpecError(problems)`, `AdapterError` |
| `forge/adapters/common.py` | `Emitter` — the only shared adapter helper |
| `forge/adapters/pimono.py` | Largest adapter (~1.1k lines); embeds `guardrails.py`, `run.sh`, `gatherer.py`, `mcp.ts`, launchd plist |
| `forge/adapters/langgraph.py` | Emits the Python project; provider→package inference; codex-model guard |
| `forge/adapters/eve.py` | Emits TypeScript agent + `runGuarded<T>` guardrail wrapper |
| `schema/agent-spec.schema.json` | Machine-readable spec shape (documentation aid, not the authority) |
| `docs/spec-v1.md` | Canonical field reference: top level, guardrails, receipt schema, validation rules |
| `docs/adapters.md` | The 7-point adapter contract |
| `examples/sitter-spec.json`, `examples/assistant-spec.json` | The two fixtures every adapter test uses |
| `tests/bless_golden.py` | Golden regeneration script |
| `skills/agent-forge/SKILL.md` | The intended producer path for users; mirrors the workflow the CLI automates |

## Runtime/Tooling Preferences

- **Python 3.10+**, stdlib only for the generator. No `pyproject.toml`,
  `setup.py`, `Makefile`, `package.json`, or `bin/`/`scripts/` at the repo root —
  there is **no build step and no package manager** for the generator.
- Run from the checkout root so `forge` is importable, or use
  `python3 forge/cli.py` which bootstraps `sys.path` itself.
- Dev-only dependencies: `pytest>=8`, `jsonschema>=4` (`requirements-dev.txt`).
  `ruff` is installed ad hoc by CI and is **not** in that file. Do not add
  runtime dependencies to the generator.
- CI (`.github/workflows/test.yml`) runs on pushes to `main` and all PRs, on
  Python `3.10` and `3.12`: `pip install -r requirements-dev.txt ruff` →
  `ruff check forge tests` → `python -m pytest -q`. A second job literally named
  `pytest` exists only because branch protection requires that check name.
- Runtime dependencies appear only *inside* generated bundles
  (`examples/assistant-langgraph/pyproject.toml` for LangGraph,
  `package.json` with `eve: latest` for eve; pi-mono needs the `pi` CLI, bash,
  and python3).

## Testing & QA

- **Framework:** pytest only, no plugins, no `conftest.py`, no pytest config
  file. Layout is flat `tests/`, files named `test_<area>.py`, adapters named
  `test_adapter_<runtime>.py`. 98 tests collected.
- **Localisation:** the two real spec files in `examples/` are the shared
  fixtures; tests mutate parsed specs in memory for invalid-input cases and
  write generated trees only under pytest's `tmp_path`.
- **Golden-file tests:** `tests/golden/<runtime>/<name>/` is compared
  byte-for-byte (`filecmp.cmp(..., shallow=False)`) against fresh adapter output.
  A failure message names the remedy: `python3 tests/bless_golden.py`. Tests also
  assert `examples/sitter-pimono` and `examples/assistant-langgraph` stay equal to
  golden output, so a generator change requires re-blessing *and* regenerating
  those trees.
- **Integration style:** generated bundles are exercised as subprocesses with
  `timeout=15` — `bash run.sh`, `python3 guardrails.py require|put|check-tool`,
  `python3 -m forge …` — never by importing generated code into the test process.
- **Determinism:** no network and no live LLM calls in tests. External binaries
  are faked: `_fake_pi()` writes a `#!/bin/sh` stub named `pi` onto `PATH`;
  `tests/fake_mcp_server.py` is a dependency-free newline JSON-RPC MCP server.
  Behavioural knobs are env vars (`SITTER_ITEMS`, `SIT_TIMEOUT_SEC`,
  `SIT_LOCK_SEC`).
- **Coverage:** none. No coverage provider, no thresholds, no gate. Do not
  claim coverage numbers.
- **Repo-policy test:** `tests/test_no_private_facts.py` drives `git ls-files`
  and scans every text file git would publish (tracked, plus untracked and not
  ignored) for absolute home paths, secret-manager references, and other
  personal markers. Generated output and docs — including this file — must pass
  it: do not quote the forbidden literals anywhere in the repo.
- **Schema agreement:** `tests/test_spec.py` dual-validates example specs against
  both the hand-rolled validator and `jsonschema` (skipped when `jsonschema` is
  absent via `importorskip`). Any spec-field change requires schema + validator +
  `docs/spec-v1.md` + test **in one PR**.
- **Manual QA:** `tests/test_skill_smoke.md` is a human checklist (skill
  interview → spec on disk → validate → generate → `run.sh --dry-run` /
  langgraph `compileall`, with the ≤30-minute time-to-working-agent gate). It is
  not collected by pytest and is not referenced by CI.
- **Guardrails are load-bearing:** PRs that weaken enforcement (fewer call sites,
  opt-out defaults) need explicit rationale in the PR description, and adapter
  tests assert the call sites exist.
