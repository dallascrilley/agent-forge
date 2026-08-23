# Agent Forge Conductor — working specification

- Status: accepted design baseline; Phase 1 implementation in progress
- Version: 0.4-draft
- Captured: 2026-08-23
- Tracking: `af-ezi`

## Revision log

- `0.1-draft`: captured the complete accepted brainstorm before further research.
- `0.2-draft`: incorporated current Pi SDK/RPC and Orca orchestration evidence. Chose companion orchestration schemas, bound the Orca backend to native Run/Task/Dispatch state, added context-file provenance, and recorded gaps in current Pi launch and Agent Forge guardrail/MCP behavior.
- `0.3-draft`: chose PyYAML as a catalog-compiler-only optional dependency while preserving the stdlib-only Agent Spec v1 generator path.
- `0.4-draft`: established closed companion orchestration contract v1 schemas and authoritative stdlib validators without revising Agent Spec v1.

## Change discipline

This document durably records the decisions accepted during the design conversation. It is the source of truth after chat compaction or process restart.

Accepted decisions are not silently replaced after implementation research. A revision must:

1. name the affected decision;
2. state the new evidence;
3. describe the compatibility and migration impact;
4. update this document and the roadmap together.

Open questions are labeled as such. Current Agent Forge v1 behavior is evidence, not a constraint: the project is young enough to improve or replace an existing contract when the conductor exposes a better abstraction.

## Vision

Agent Forge Conductor is a lean Pi agent whose primary job is delegation. It translates a task into capability requirements, resolves approved resources from a catalog, compiles a reproducible worker manifest, launches an isolated worker, observes it, and integrates only evidence-backed results.

```text
Task intent
  → WorkerRequest
  → catalog resolver
  → deterministic compiler
  → policy validation
  → immutable WorkerManifest
  → backend adapter
  → worker result
  → independent review when required
  → disposition
```

The conductor does not preload the catalog's skills, extensions, prompts, MCP tools, or worker system prompts. Those resources belong to workers assembled for a specific task.

## Goals

- Compose specialized workers from audited resources at spawn time.
- Keep the conductor's prompt and tool surface small.
- Make every launch reproducible, inspectable, bounded, and recoverable.
- Support Orca-managed workers first and local Pi subprocesses through the same manifest later.
- Reuse Agent Forge's cross-runtime agent vocabulary where it remains sound.
- Enforce permissions and lifecycle constraints in code, not only in prompts.
- Preserve enough durable state to resume after Pi compaction, restart, crash, or backend interruption.

## Non-goals for v1

- Installing packages, skills, extensions, or MCP servers during worker launch.
- Nested worker delegation.
- Deployment, messaging, credential changes, destructive operations, or arbitrary outward side effects.
- Arbitrary model-authored shell commands, filesystem paths, MCP endpoints, or system prompts in a manifest.
- Multiple competing implementations of the same capability in one normal worker.
- A second worker backend in the first vertical slice.

## Relationship to Agent Forge

Agent Forge v1 describes a self-contained deployable agent and generates runtime-native bundles. The conductor adds a dynamic layer:

- a catalog of approved resource references;
- capability-driven resolution;
- task-scoped worker requests and manifests;
- scheduling, observation, review, integration, and recovery.

The implementation must determine whether these contracts become additive Agent Spec v1 fields, Agent Spec v2, or companion orchestration schemas. It must not duplicate semantically identical fields under different names merely to avoid changing v1.

A useful separation is:

```text
AgentSpec       durable declaration of an agent definition
CatalogEntry    trusted, locked resource or recipe
WorkerRequest   model-authored task intent within a closed schema
WorkerManifest  fully resolved executable instance
RunLedger       append-only runtime history and artifacts
```

### Schema decision after interface review

The MVP will use companion orchestration schemas rather than adding conductor concerns to Agent Spec v1. Agent Spec remains the portable declaration of a reusable agent definition. Catalog entries may reference an Agent Spec as recipe input; compilation normalizes it into the same locked resources used by other recipes.

This avoids making task attempts, backend handles, DAG state, and runtime budgets part of a reusable agent declaration. Agent Spec itself may still receive a separately migrated v2 where existing safety or portability contracts need correction.

## Accepted design decisions

1. One worker manifest format supports backend adapters. Orca is first; local Pi is second.
2. Runtime composition uses only audited, preinstalled catalog resources. Launch never installs code.
3. Requests name capabilities first. Exact approved resource IDs are optional overrides.
4. The model emits `WorkerRequest`; deterministic code emits and validates `WorkerManifest`.
5. Only the conductor delegates in v1. Workers have delegation depth zero.
6. Read-only workers may share a source workspace. Mutating workers require isolation.
7. Only the conductor may accept or integrate worker changes.
8. Every worker returns a structured, evidence-bearing result.
9. v1 has four fixed permission profiles: `observe`, `research`, `modify-isolated`, and `review`.
10. Every mutation receives independent review before integration.
11. Review receives a bounded context packet up front and has strict time limits.
12. System prompts are assembled from ordered, approved fragments.
13. Catalog source is human-authored YAML; workers execute only from a generated JSON lockfile.
14. The catalog supports atomic resources and approved recipes.
15. MCP access is lazy, allowlisted, worker-scoped, and exposed through one gateway.
16. Scheduling uses an explicit dependency DAG and bounded concurrency.
17. In-policy workers launch autonomously; preview mode is optional.
18. Every run has a durable local ledger.
19. The conductor exposes one model-facing `delegate` tool and one human `/workers` interface.
20. The conductor itself is non-mutating.
21. Recipes request model tiers; compilation records an exact approved model.
22. Only backend startup failures before a model turn retry automatically, once.
23. Only an independently `verified` mutation may integrate automatically.
24. The first vertical slice is Orca-only and proves the complete loop.
25. No essential orchestration state may exist only in model context or process memory.

## System architecture

### Conductor

The Pi conductor loads only:

- `read`
- `grep`
- `find`
- `ls`
- one extension-provided `delegate` tool

It does not receive `bash`, `edit`, or `write`. Applying an approved result is a typed backend operation, not an arbitrary conductor shell action.

The conductor's system prompt teaches task decomposition, capability requests, evidence interpretation, and the result protocol. It contains capability summaries, not the full bodies of catalog resources.

### Catalog compiler

The compiler is deterministic and has no model dependency. It:

1. loads and validates authored catalog YAML;
2. resolves resource paths, versions, hashes, and compatibility;
3. verifies capability providers and conflicts;
4. emits `catalog.lock.json` atomically;
5. rejects unknown fields and unresolved references;
6. never embeds credential values.

Workers and the conductor's resolver read the lockfile, never raw YAML.

### Resolver and policy engine

The resolver accepts a valid `WorkerRequest`, chooses the smallest compatible recipe, adds missing atomic capabilities, and produces a candidate manifest.

The policy engine then validates:

- resource IDs and hashes exist in the lockfile;
- backend and Pi compatibility;
- one implementation per exclusive capability;
- permission profile permits every tool and resource behavior;
- workspace isolation matches side effects;
- model, timeout, and concurrency budgets;
- delegation depth is zero;
- MCP servers and tools are explicitly allowlisted;
- prompt fragments cannot weaken invariant policy.

Resolution or policy failure produces a structured rejection. There is no silent fallback to an unapproved implementation.

### Scheduler

The scheduler executes an explicit DAG. Only nodes whose dependencies have reached acceptable terminal states may launch. Maximum global concurrency is three, with at most one active mutation worker per target repository.

The scheduler automatically inserts a review node after each mutation node. Review is not model-authored optional work.

For Orca, the scheduler projects this plan into one native Run with Tasks, dependencies, and Dispatches. Orca is authoritative for its worker lifecycle and coordinator inbox; Agent Forge remains authoritative for manifest compilation, policy, cross-backend DAG intent, evidence contracts, and integration disposition. The implementation must reconcile these records rather than operating a second competing worker scheduler.

### Backend adapters

Each backend implements the same conceptual interface:

```text
prepare(manifest) → BackendHandle
launch(handle) → WorkerHandle
observe(worker, cursor?) → EventPage
wait(worker, deadline) → WorkerExit
cancel(worker, reason) → disposition
collect(worker) → WorkerResult
integrate(worker, reviewedResult) → IntegrationResult
reconcile(persistedHandle) → observed backend state
```

Backend commands and responses are captured in the ledger after redaction.

## Durable execution model

Durability is a primary correctness property, not an observability add-on.

### Invariants

- The request and compiled manifest are durable before any worker is spawned.
- A state transition is appended to the ledger before its corresponding external side effect when possible.
- Every external creation operation has a persisted idempotency key.
- Backend IDs, terminal handles, worktree IDs, revisions, and event cursors are persisted as soon as observed.
- Results and review packets are written atomically before their state becomes terminal.
- Process restart reconstructs scheduler state from disk and reconciles it with the backend.
- Pi conversation context may cache a run summary, but it is never authoritative.
- The Pi session stores only durable run pointers as custom entries; the ledger stores the referenced state.
- On session start, resume, or post-compaction turn, the extension rebuilds a bounded active-run digest from disk before the conductor reasons.
- Compaction cannot erase a run, approval, budget, worker handle, or integration disposition.

### Run directory

```text
~/.pi/agent/delegations/
  index.json                 # atomic active/recent run registry
  <run-id>/
    request.json
    plan.json
    manifests/<worker-id>.json
    events.jsonl
    backend.json             # Orca Run/Task/Dispatch IDs and recovery cursors
    results/<worker-id>.json
    reviews/<worker-id>.json
    usage.json
    disposition.json
    artifacts/
```

Sensitive environment values, auth tokens, and model credentials are never recorded.

### Event journal

`events.jsonl` is append-only. Each event includes:

```json
{
  "schemaVersion": 1,
  "eventId": "01J...",
  "runId": "run-...",
  "workerId": "implementation",
  "sequence": 12,
  "timestamp": "2026-08-23T00:00:00Z",
  "type": "worker.launch.requested",
  "idempotencyKey": "run-.../implementation/launch/1",
  "data": {}
}
```

Sequence numbers are monotonic per run. Recovery ignores incomplete trailing JSONL writes, reports them, and never guesses that an external operation did not happen. It reconciles by idempotency key or persisted backend identity.

### State machine

```text
requested
  → compiled
  → policy-approved
  → queued
  → preparing
  → launched
  → running
  → completed | partial | blocked | failed | cancelled
  → review-pending (mutation only)
  → verified | verified-with-caveats | refuted
  → integrated | retained-isolated | rejected
```

Every transition has a closed set of legal predecessors. Terminal states are immutable except for appending a later disposition event.

### Recovery

On conductor startup, after compaction, or `/workers resume`:

1. load the run registry and scan nonterminal ledgers;
2. validate request and manifest hashes;
3. acquire a per-run scheduler lease;
4. query the backend using persisted identities;
5. append reconciliation observations;
6. resume waits, collection, or review without replaying completed side effects;
7. refresh the Pi session's bounded run digest from recovered state;
8. surface ambiguous state instead of spawning a replacement worker.

## Catalog

### Authored layout

```text
catalog/
  capabilities.yaml
  resources/
  recipes/
  roles/
  prompts/
  policies/
  models.yaml
  catalog.lock.json
```

`catalog.lock.json` is generated and committed when the catalog is meant to travel with the repository. Personal catalog overlays may live in dotfiles, but they compile into the same lock format.

### Catalog authoring dependency boundary

**Decision (`af-ezi.1.1`):** catalog compilation uses PyYAML 6 through the optional dependency set in `requirements-catalog.txt`. Agent Spec v1 validation and generation remain stdlib-only and must not import PyYAML, directly or transitively. Reading a compiled JSON lockfile is also stdlib-only. This is an additive command boundary, not an Agent Spec migration.

The explicit catalog-authoring setup and success path are:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-catalog.txt
.venv/bin/python -m forge.catalog compile catalog --output catalog/catalog.lock.json
```

The catalog command must import the YAML parser only on its YAML authoring path. If PyYAML is unavailable, it exits nonzero before reading or writing catalog output and reports this stable actionable error (without a traceback):

```text
catalog compilation requires PyYAML; install it with: python3 -m pip install -r requirements-catalog.txt
```

Compilation uses `yaml.safe_load`, then applies the authoritative closed catalog validator; YAML constructors cannot create project objects. The compiler must not silently accept JSON as a replacement authoring contract, invoke a system YAML executable, auto-install the dependency, or modify an existing lockfile after dependency or parse failure.

Compatibility and migration behavior:

- Existing `python3 forge/cli.py validate ...` and `generate ...` commands require no installation and retain Python 3.10+ support.
- Existing Agent Spec v1 JSON files, adapters, generated bundles, and project-wide `python3 -m pytest -q` verification are unchanged.
- Catalog authors opt in by installing `requirements-catalog.txt`, preferably in a dedicated virtual environment; deployments that only consume a committed lockfile do not install it.
- PyYAML is constrained to `>=6.0.2,<7`; this line supports the project's Python floor (its package metadata requires Python 3.8+), while the upper bound makes parser-major changes an explicit decision.

Alternatives considered:

- **Make PyYAML a project-wide requirement:** rejected because it breaks the documented zero-install generator and makes lockfile consumers install an authoring dependency.
- **Require a separately packaged compiler environment:** retained as an operational option (the virtual environment above), but rejected as the only architecture because this repository has no package metadata and a separate distribution would add a second versioning boundary.
- **Keep JSON as the only zero-install catalog source:** rejected because it revises accepted YAML authoring; JSON remains the generated execution format.
- **Implement a dependency-free YAML subset or use a stdlib format such as TOML:** rejected because Python 3.10 has no TOML parser, YAML is not in the standard library, and a bespoke parser would add security and compatibility risk.
- **Shell out to a system YAML tool:** rejected because availability and parser semantics would not be reproducible across fresh checkouts.

### Resource entry

Illustrative fields:

```yaml
id: extension.pi-web-access
kind: extension
version: 0.24.2
source: npm:pi-web-access@0.24.2
path: node_modules/pi-web-access/index.ts
sha256: "..."
provides: [web.research, web.fetch, web.source-check]
requires: []
conflicts: [extension.other-web-provider]
backends: [orca-pi, local-pi]
piCompatibility: ">=0.84.0 <0.85.0"
trust: audited
startupCost: medium
runtimeRisk: network
permissions: [research]
```

Resource kinds initially include:

- `skill`
- `extension`
- `prompt-template`
- `system-fragment`
- `mcp-server`
- `role`
- `recipe`

Themes are valid Pi resources but are excluded from v1 worker composition because they do not affect task capability.

### Capabilities

Capabilities are stable names such as:

```text
code.search
code.modify
code.test
web.research
web.fetch
browser.interact
git.review
orca.coordinate
mcp.filesystem.read
```

A capability declares whether it is additive or exclusive. Exclusive capabilities resolve to exactly one provider.

### Recipes

Initial recipes:

- `repo-scout`
- `web-researcher`
- `implementation-worker`
- `change-reviewer`
- `browser-operator` (catalogued but not required for the first vertical slice)

A recipe declares defaults, not executable paths. The compiler resolves all resource references into the manifest.

## Contracts

The examples below show intended semantics. The closed contract v1 JSON Schemas under `schema/orchestrator/` define exact fields and enums; `forge/orchestrator/contracts.py` is the stdlib-only validation authority and the test suite checks the shipped schemas against it. Documents use lower-camel-case JSON keys, reject unknown fields at every closed object, collect problems at stable JSON paths, and normalize into deeply immutable named Python contract types. Agent Spec v1 and `schema/agent-spec.schema.json` remain separate and unchanged.

`WorkerRequest` is the only model-authored launch input. Its closed shape intentionally has no command, filesystem-path, MCP-endpoint, credential, or system-prompt fields. Trusted compiler outputs and evidence contracts may contain bounded path or argv records where their schema explicitly permits them.

### WorkerRequest

The model may emit only this constrained intent:

```yaml
schemaVersion: 1
task: "Implement the accepted parser change"
acceptanceCriteria:
  - "new syntax parses"
  - "existing parser tests pass"
capabilities:
  required: [code.search, code.modify, code.test]
  optional: []
recipe: implementation-worker
permissionProfile: modify-isolated
workspace:
  repository: current
  baseRevision: "..."
modelTier: deep
budget:
  timeoutMinutes: 30
  maxDelegationDepth: 0
dependsOn: [evidence]
resourceOverrides: []
```

The request cannot contain raw extension paths, shell launch commands, MCP endpoints, credential values, or arbitrary system-prompt text.

### WorkerManifest

The compiler produces a complete immutable manifest:

```yaml
schemaVersion: 1
manifestId: "sha256:..."
runId: "run-..."
workerId: implementation
catalogLockHash: "sha256:..."
backend: orca-pi
task: "..."
acceptanceCriteria: ["..."]
model:
  tier: deep
  provider: openai-codex
  id: gpt-...
  thinking: high
permissionProfile: modify-isolated
workspace:
  mode: isolated-worktree
  repositoryId: "..."
  baseRevision: "..."
resources:
  skills: []
  extensions: []
  promptTemplates: []
  systemFragments: []
  contextFiles:
    - path: AGENTS.md
      sha256: "sha256:..."
  mcpServers: []
tools:
  allow: [read, bash, edit, write, grep, find, ls]
prompt:
  orderedFragmentHashes: ["sha256:..."]
budget:
  timeoutSeconds: 1800
  maxDelegationDepth: 0
returnContract: worker-result-v1
```

Manifest identity is content-derived after excluding runtime-assigned fields such as backend handles.

### WorkerResult

```yaml
schemaVersion: 1
workerId: implementation
status: completed # completed | partial | blocked | failed
outcome: "Implemented parser support and added regression coverage."
claims: []
evidence: []
changes: []
verification: []
artifacts: []
blockers: []
usage: {}
```

A claim without evidence is unverified. Prose outside this envelope is retained as an artifact but cannot establish completion. On Orca, the worker writes this envelope to its injected `report-path`; the required `worker_done` message settles the Dispatch and points to the report. `worker_done` prose alone is not the result contract.

### ReviewPacket

Review receives necessary context directly:

```yaml
schemaVersion: 1
originalTask: "..."
acceptanceCriteria: []
constraints: []
workerManifestSummary: {}
baseRevision: "..."
headRevision: "..."
diffArtifact:
  path: artifacts/change.diff
  sha256: "sha256:..."
claims: []
verificationCommands: []
verificationOutputs: []
artifacts: []
```

It does not receive the worker's conversation history. Review is scoped to the delegated change and may inspect relevant files and rerun declared checks.

### ReviewResult

```yaml
schemaVersion: 1
verdict: verified # verified | verified-with-caveats | refuted
summary: "..."
claimAssessments: []
verification: []
caveats: []
```

Only `verified` permits automatic integration.

## Permission profiles

### `observe`

- repository and context reads
- no network
- no writes
- shared workspace permitted

### `research`

- reads
- approved network tools
- no repository writes
- shared workspace permitted

### `modify-isolated`

- reads, shell, edits, writes, and declared verification commands
- isolated worktree required
- no deployment, messaging, package installation, credential changes, or destructive external actions
- independent review required

### `review`

- reads changed and context files
- may rerun declared verification commands under mechanical policy
- no edits, writes, network, or delegation
- default timeout 5 minutes; maximum 15 minutes

A raw unrestricted `bash` tool does not by itself satisfy these policies. The implementation must enforce shell behavior mechanically or route commands through typed guarded operations.

Current Agent Forge pi-mono bundles do not yet provide complete mechanical enforcement for mutable workers: a prompt tells the model to call `guardrails.py`, but unrestricted Pi `bash`/`write` calls can bypass that helper. The conductor must not inherit this gap. Existing guardrail claims and adapters require a separate correction or versioned migration before serving `modify-isolated`.

## Prompt composition

Worker prompts are assembled in this order:

1. invariant worker protocol;
2. permission-profile constraints;
3. role contract;
4. capability-specific guidance;
5. task and acceptance criteria;
6. required result envelope.

Later fragments may specialize earlier instructions but cannot weaken permissions, budgets, evidence requirements, or the return contract. Every fragment comes from the locked catalog or compiler-owned invariant text.

Untrusted task text, repository content, web content, tool output, and worker prose cannot redefine the manifest.

## Model selection

Recipes request one of:

- `fast`
- `balanced`
- `deep`

The lockfile maps each tier to ordered approved models. Compilation selects one exact available model and thinking level and records them in the manifest. Workers may not switch models.

Defaults:

| Worker | Tier |
|---|---|
| conductor | `deep` |
| repo scout | `fast` |
| change reviewer | `fast` |
| web researcher | `balanced` |
| implementation | `deep` |

## Scheduling and budgets

| Permission profile | Default | Maximum |
|---|---:|---:|
| `observe` | 10 min | 20 min |
| `research` | 15 min | 30 min |
| `modify-isolated` | 30 min | 60 min |
| `review` | 5 min | 15 min |

- Maximum three concurrent workers.
- Maximum one active mutation worker per repository.
- Dependency-free DAG nodes may run concurrently.
- A timeout returns `partial` with gathered evidence.
- The conductor may cancel early after sufficient evidence.
- One retry is permitted only for a transient backend launch failure before the first model turn.
- A revised reasoning attempt is a new DAG node with a new manifest and ledger history.

## Review and integration

Every mutation produces a review node automatically.

- `verified`: conductor may apply the isolated change when it remains within the user's task.
- `verified-with-caveats`: keep isolated; revise or ask the user.
- `refuted`: reject and preserve evidence.
- apply/merge conflict: stop and report; never force or bypass checks.

The conductor cannot mark its own worker result verified.

## MCP gateway

The manifest lists approved server IDs and per-server tool allowlists. Workers see one gateway tool rather than every MCP tool directly.

The gateway:

- resolves only locked server definitions;
- references credential environment-variable names, never values;
- starts servers lazily;
- exposes only allowed tools;
- records invocation evidence;
- applies deadlines and cancellation;
- shuts server processes down with the worker;
- fails clearly when a required server is unavailable.

The contract is implementation-neutral. Existing Agent Forge MCP generation is evidence to reuse or revise, not automatically the final gateway.

## Pi conductor interface

The extension registers one model-facing tool:

```text
delegate
  action: catalog | preview | spawn | status | collect | cancel | integrate
```

The exact schema uses a discriminated union and rejects unknown fields. The Pi extension should remain a thin client over the durable Agent Forge orchestration core rather than implementing a second validator or state machine in TypeScript.

The extension also registers `/workers`, which displays:

- active and recent runs;
- DAG and worker states;
- elapsed time and budgets;
- resolved manifest summaries;
- review verdicts;
- cancellation and local ledger navigation.

The model does not need TUI access. All operations must also work in RPC mode.

## Backend plan

### Orca backend — v1

The backend uses Orca's public CLI with JSON output and version-matched runtime guidance. The installed runtime exposes native durable Runs, Tasks, dependency DAGs, Dispatches, FIFO Deliveries, worker settlement, bounded transcript reads, and supervised worker cleanup.

Mapping:

- run namespace: `orchestration run-create`;
- DAG nodes: `orchestration task-create --deps`;
- ordinary launch: `orchestration worker-start`;
- completion/questions: `orchestration check --wait` and Delivery acknowledgment;
- observation: `worker-show` and cursor-based `worker-read`;
- cancellation/recovery: `worker-stop`, `worker-abandon`, and typed recovery receipts;
- cleanup: idempotent `worker-release` after accepted settlement;
- worker settlement: one injected `worker_done` with explicit outcome and report path.

The backend persists Run, Task, Dispatch, Delivery, worktree, and terminal identities. Unknown mutation outcomes follow Orca's exact `--retry-request` recovery guidance; they do not trigger a guessed replacement.

A remaining launch gap must be proven before implementation: `worker-start --agent pi` does not expose arbitrary Pi extension, skill, prompt-template, tool, or system-prompt argv. The likely bounded path is to start a manifest-specific Pi command in an Orca terminal and attach it with `worker-start --terminal`, but ownership, cleanup, setup sequencing, and prompt injection require an observed canary. The spec does not declare that path solved yet.

### Local Pi backend — post-v1

The same manifest will later compile into an isolated Pi subprocess or SDK session. Pi's SDK supports custom `ResourceLoader`, explicit tools, model selection, system-prompt override, in-memory sessions, event subscriptions, and RPC mode. The backend choice must not change worker semantics or weaken policy.

## Current interface findings

### Pi

Current Pi supports the worker composition model directly:

- `DefaultResourceLoader` can receive exact extension paths and override skills, prompts, context files, system prompt, and appended prompt fragments.
- A fully custom `ResourceLoader` can disable all ambient discovery.
- `createAgentSession` accepts an exact model, thinking level, built-in/custom tool allowlist, in-memory settings, and session policy.
- RPC exposes `agent_settled`, tool events, usage statistics, append-only session entries, and stable entry cursors.
- Pi's own automatic retry must be disabled for workers because the conductor retry contract is stricter.
- TUI `custom()` is unavailable in RPC mode, so `/workers` must have a text/RPC representation independent of its interactive overlay.

Project context is not an ambient afterthought. Code-worker manifests must record the selected `AGENTS.md`/compatible context sources and their hashes. The backend then loads only that resolved set. This preserves project instructions while keeping the manifest reproducible.

### Orca

Orca already provides most backend lifecycle durability. Agent Forge should use native orchestration provenance instead of approximating it with terminal polling. Important semantics include:

- a Run is a durable namespace and inbox, not a scheduler;
- a Task owns dependencies and status;
- a Dispatch owns one supervised attempt;
- `worker_done` is authoritative settlement and must be sent exactly once;
- Delivery batches replay until acknowledged;
- transcript cursors are source-pinned;
- stale terminal handles are routing failures, not new worker identity;
- release is idempotent and preserves inspectable output;
- retries are explicit new Dispatches linked to prior attempts.

### Existing Agent Forge gaps exposed by this design

- Pi mutable-agent guardrails do not intercept every direct built-in shell/write path.
- The generated MCP extension starts every configured server eagerly and registers every tool directly; it is not the accepted lazy gateway.
- Agent Spec v1 permits MCP environment/header values that could become literal secrets; conductor catalogs may store only credential references.
- Agent Spec v1 uses runtime-native model IDs, while conductor recipes use model tiers resolved at compile time.

These findings do not block the specification. They create explicit migration work and must not be papered over by adapter claims.

## Security model

- Extensions and MCP servers are privileged code and require catalog audit.
- Launch never downloads or installs resources.
- Lockfile hashes bind manifests to reviewed content.
- Credential values remain in approved runtime stores or environment sources.
- Task and repository content are untrusted.
- Capability conflicts fail closed.
- Backend commands are typed and argument-safe.
- Worker shell access requires mechanical enforcement.
- No worker may delegate in v1.
- No hidden retry or unbounded wait is permitted.
- Ledger redaction is tested with trap fixtures.

## Autonomous behavior

All four v1 permission profiles may launch automatically within policy and budget. The conductor logs the resolved manifest before launch. A preview/confirm mode is available for debugging and cost control.

Requests outside policy are rejected rather than silently widened. The implementation may ask the user for a narrower task, but it cannot grant itself a new permission profile.

## MVP acceptance criteria

The Orca-only vertical slice is complete when it demonstrates:

1. catalog YAML compiles deterministically to a hashed lockfile;
2. invalid, conflicting, missing, or unhashed resources fail closed;
3. a Pi conductor exposes only read/search plus `delegate`;
4. `repo-scout` and `web-researcher` workers launch from compiled manifests;
5. an implementation worker receives an isolated Orca worktree;
6. a review worker receives a bounded ReviewPacket automatically;
7. only a `verified` change can integrate;
8. a process restart can reconstruct and reconcile an active run without duplicate spawn;
9. timeouts and cancellation preserve partial evidence;
10. no secrets enter manifests, logs, or fixtures;
11. all behavior is testable without a live model, with one explicit live smoke test documented separately;
12. the existing Agent Forge examples and supported adapters either remain compatible or have an explicit migration.

## Open design questions

These are intentionally unresolved pending interface and schema work:

- Is the durable Agent Forge core best invoked as short-lived commands, a resumable foreground operation, or a separate daemon behind the thin Pi extension?
- What mechanical shell policy is sufficient for `modify-isolated` and `review`?
- What exact typed operation should integrate a verified Orca worktree change?
- How should a manifest-specific Pi command be launched and supervised in Orca without losing setup or cleanup guarantees?
- Which fields belong in portable AgentSpec versus machine-local CatalogEntry?
- How are personal catalog overlays merged without weakening repository policy?
- What is the stable MCP gateway protocol and evidence format?
- What token/cost fields are consistently observable across Pi and Orca?

Answers must update this document with evidence and migration impact.
