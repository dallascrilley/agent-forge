---
name: agent-forge
description: Scaffold a self-contained agent (harness config, system prompt, skills, MCP servers, plugins, trigger, guardrails) from a short interview or a partial JSON spec. Emits idiomatic bundles for pi-mono, LangGraph, and Eve runtimes. Use when the user wants to create, generate, or scaffold a new agent.
---

# agent-forge

Turn a conversation into a running agent bundle. The durable artifact is the
**spec JSON** — everything else regenerates from it.

## Prerequisites

- The agent-forge repo is cloned locally. If the user doesn't have it:
  `git clone https://github.com/dallascrilley/agent-forge.git` into a
  location of their choosing; remember the path for this session.
- `python3` (3.10+). The generator is stdlib-only — never pip-install
  anything for generation itself.
- When `forge` is importable (for example, from the repository root), prefer
  `python3 -m forge`; otherwise use `python3 <repo>/forge/cli.py`.

## Procedure

1. **Check for an existing spec.** If the user hands you a spec file (even
   partial), copy it to the working location and skip to step 3 with only
   the questions needed to fill gaps.

2. **Interview — one question at a time.** Prefer the harness's blocking
   question tool with single-select options where a fixed set exists.
   Collect, in this order:
   - **name + purpose** — what should this agent do, in one or two
     sentences? (Derive a kebab-case name; confirm it.)
   - **runtime** — `pimono`, `langgraph`, `eve`, or a combination. Ask what they
     run today.
   - **model** — the runtime-native model id (e.g. `anthropic/claude-sonnet-4-5`,
     `openai/gpt-5-mini`). Offer 2-3 sensible defaults.
   - **tools & MCP servers** — does it need MCP? If yes, which servers
     (stdio command or remote url)? Set `guardrails.allowed_tools` to the
     tool names the agent may invoke.
   - **side effects** — what may it *change* (write files where, send
     messages, post)? Each becomes an `allowed_side_effects` entry. Nothing
     means read-only — say so and confirm.
   - **skip condition** — when must a run complete without the model
     (empty inbox, no matching posts, all checks green)? That is
     `llm_optional: true` plus the gatherer. If every tick needs the
     model, set `llm_optional: false`.
   - **trigger** — manual or cron (5-field expression).
   - **system prompt** — draft it yourself from the answers; show it.

   See `reference/spec-cheatsheet.md` for field shapes and defaults.

3. **Write the spec to disk** at a user-chosen path (default:
   `./<name>-spec.json`). The spec on disk is the checkpoint — write it
   *before* generating, so an interrupted session loses nothing.

4. **Show the spec, get one confirmation.** A compact summary (name,
   runtimes, model, side effects, trigger), not a JSON dump unless asked.

5. **Validate, then generate:**

   ```bash
   python3 -m forge validate <spec>
   python3 -m forge generate <spec> --runtime <rt> --out <dir>
   ```

   If `forge` is not importable, replace `python3 -m forge` with
   `python3 <repo>/forge/cli.py`. For a non-interactive producer path, write
   the spec first with `forge new`:

   ```bash
   python3 -m forge new --name <slug> --purpose "<purpose>" \
     --model <model> --runtime <rt> --out <spec>
   ```

   Validation errors: fix the spec, re-validate. Do not hand-edit generated
   output to compensate — the spec is the truth.

6. **Smoke check the bundle** (from the generated README):
   - pimono: `bash <dir>/run.sh --dry-run` prints the pi argv.
   - langgraph: `python3 -m compileall <dir>` and point the user at
     `<dir>/README.md` setup (venv + `pip install -e .`).

7. **Report:** spec path, output dir per runtime, how to run, how to pause
   (the stop file), where receipts land.

## Rules

- Guardrails are not optional decoration. Never suggest removing the generated
  guardrails module, the stop file, or the receipt contract to make something
  work — change the spec and regenerate.
- If the user asks for a runtime with no adapter (hermes): say it is not
  supported yet, point at `docs/adapters.md`, and still write the spec with
  that runtime omitted from `runtimes` (spec is future-proof; the `runtimes`
  list only names what to emit now).
- Keep the spec free of machine-specific absolute paths unless the user
  explicitly wants them.
