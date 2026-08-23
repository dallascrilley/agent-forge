# Agent Forge Conductor — roadmap

- Status: accepted phased baseline; Phase 1 and Phase 2 complete, Phase 3 implementation not started
- Depends on: [working specification](spec.md)
- Tracking: `af-ezi`

## Delivery rule

Each phase must leave a durable, testable artifact. A later phase may revise an earlier contract only by updating the specification, migration notes, schemas, fixtures, and gates together.

The first product milestone is one complete Orca-backed delegation loop, not broad backend or resource coverage.

## Phase 0 — durable design baseline

Outcome: the accepted design survives chat compaction and process restart.

Deliverables:

- `docs/orchestrator/spec.md`
- `docs/orchestrator/roadmap.md`
- companion-schema decision for orchestration alongside Agent Spec v1
- documented current Pi SDK/RPC and Orca orchestration constraints
- explicit findings for existing pi-mono guardrail and MCP gaps
- tracked implementation work broken into Beads after schemas stabilize

Gate:

- all accepted brainstorm decisions are represented;
- unresolved questions are explicit;
- existing untracked user files remain untouched;
- current Agent Forge tests still pass because no runtime behavior changed.

## Phase 1 — contract and catalog foundation

Outcome: authored resources compile into a deterministic, trusted catalog, and worker intent compiles into a policy-checked manifest.

Deliverables:

- companion orchestration JSON Schemas and authoritative stdlib validators for:
  - catalog source and lockfile
  - `WorkerRequest`
  - `WorkerManifest`
  - run events
  - `WorkerResult`
  - `ReviewPacket`
  - `ReviewResult`
- optional PyYAML catalog-compiler dependency in `requirements-catalog.txt`; the existing generator and lockfile readers remain stdlib-only
- catalog compiler with atomic lockfile output
- capability resolver and conflict detection
- permission-profile validator
- context-file selection and hashing
- model-tier resolver
- initial resources and recipes:
  - `repo-scout`
  - `web-researcher`
  - `implementation-worker`
  - `change-reviewer`
- redaction and malicious-catalog fixtures

Gates:

- identical inputs produce byte-identical lockfiles and manifests;
- unknown fields, path escapes, hash mismatches, duplicate exclusive providers, incompatible Pi versions, and permission widening fail closed;
- no credential values appear in compiled output;
- existing Agent Forge examples validate unchanged; any separate Agent Spec v2 safety migration has its own documented command;
- validators collect all useful errors rather than stopping at the first.

## Phase 2 — durable scheduler core

Outcome: a model-independent engine can persist, schedule, recover, and cancel a worker DAG without launching a real backend.

Deliverables:

- append-only JSONL run ledger
- atomic active-run registry and document writer
- Pi session custom entries containing run pointers only
- legal state-transition table
- per-run scheduler lease
- idempotency-key generation
- dependency DAG scheduler
- profile timeouts and global concurrency limit
- automatic review-node insertion
- fake backend with deterministic event scripts
- crash/restart reconciliation harness

Gates:

- restart at every state boundary does not duplicate launch or integration;
- truncated final JSONL lines are detected and handled without losing prior events;
- only dependency-ready nodes launch;
- no more than three workers run concurrently;
- only one mutation runs per repository;
- timeout and cancellation retain partial evidence;
- only a pre-turn transient launch failure retries, once;
- all state can be reconstructed without Pi conversation history;
- simulated Pi compaction cannot lose active-run identity, budget, backend handles, or disposition.

## Phase 3 — lean Pi conductor surface

Outcome: Pi can request and inspect delegation while remaining non-mutating.

Deliverables:

- thin Pi extension registering one discriminated-union `delegate` tool
- durable Agent Forge core invoked without duplicating validation or scheduler logic in the extension
- actions: `catalog`, `preview`, `spawn`, `status`, `collect`, `cancel`, `integrate`
- `/workers` TUI and RPC-compatible data path
- conductor system prompt assembled from approved invariant fragments
- lazy capability summaries
- bounded active-run digest rebuilt from disk on session start and after compaction
- explicit built-in tool allowlist: `read`, `grep`, `find`, `ls`
- no-model RPC canary tests for active tools and commands

Gates:

- conductor has no `bash`, `edit`, or `write` tool;
- malformed requests and unknown capabilities fail before backend activity;
- full catalog resource bodies do not enter the conductor prompt;
- every accepted request is persisted before spawn;
- TUI is optional: RPC and noninteractive consumers retain full control;
- `/workers` can inspect a recovered run after process restart.

## Phase 4 — Orca read-only vertical slice

Outcome: the conductor launches, observes, and collects real Orca-managed read-only workers.

Deliverables:

- Orca backend adapter using native Run, Task, Dispatch, Delivery, and worker lifecycle operations
- capability/version preflight
- observed canary for manifest-specific Pi argv plus `worker-start --terminal` supervision
- persisted Run, Task, Dispatch, Delivery, worktree, and terminal identities
- exact `--retry-request` handling for unknown mutation outcomes
- stale-handle recovery
- source-pinned bounded `worker-read` cursors
- structured worker prompt, report path, and result-envelope extraction
- idempotent `worker-release` cleanup
- `repo-scout` live path
- `web-researcher` live path
- backend trap fixtures for malformed, stale, partial, and contradictory responses

Gates:

- one real repo-scout run completes from manifest to result;
- one real web-research run completes with source evidence;
- Orca remains authoritative for worker lifecycle while Agent Forge remains authoritative for manifests, policy, and disposition;
- startup handles, Dispatch IDs, Delivery IDs, and event cursors survive conductor restart;
- read-only workers do not modify the source workspace;
- cancellation reaches a terminal ledger state;
- no model or backend retry is hidden;
- backend output cannot alter manifest policy.

## Phase 5 — isolated mutation, review, and integration

Outcome: a code change can be delegated into an isolated worktree, reviewed independently, and integrated only when verified.

Deliverables:

- Orca isolated-worktree preparation
- implementation worker launcher
- mechanically enforced mutation boundary that closes the current pi-mono direct-shell/write gap
- change and verification artifact collection
- deterministic `ReviewPacket` builder
- constrained review worker launcher
- review deadline enforcement
- typed integration operation
- conflict and caveat handling

Gates:

- mutation never runs in the conductor's source checkout;
- every mutation automatically creates a review node;
- reviewer receives task, acceptance criteria, revisions, diff, claims, and raw verification evidence up front;
- reviewer receives no worker conversation history;
- review has no edits, writes, network, or delegation;
- `verified-with-caveats` and `refuted` cannot integrate automatically;
- merge/apply conflict stops without force;
- successful integration records exact revisions and disposition.

## Phase 6 — resilience and v1 release gate

Outcome: the Orca-first MVP is safe to use as the primary Pi delegator.

Deliverables:

- end-to-end recovery tests across launch, running, collection, review, and integration
- token/time/backend usage accounting where observable
- ledger retention and cleanup policy
- catalog upgrade and rollback procedure
- threat model
- operator guide
- one explicit live smoke script
- package/install wiring separated from repository source

Gates:

- all twelve MVP acceptance criteria in the specification pass;
- forced conductor termination at each side-effect boundary recovers without duplication;
- secret-scanning fixtures pass;
- no runtime package installation exists;
- documented startup and steady-state prompt costs stay within agreed budgets;
- existing Agent Forge adapters and examples pass their compatibility gate;
- an independent review of the implementation finds no unsupported completion claims.

## Post-v1 Phase 7 — local Pi backend

Outcome: the same manifest can run without Orca while preserving semantics.

Candidates:

- Pi SDK host with custom `ResourceLoader`
- isolated Pi RPC subprocess

Required parity:

- identical manifest and result contracts;
- equivalent tool and resource isolation;
- durable process identity and cancellation;
- event capture and usage reporting;
- no auth copying into generated worker homes;
- mutation still uses isolated worktrees.

The backend is not accepted if it requires backend-specific task semantics in `WorkerRequest`.

## Post-v1 Phase 8 — MCP gateway

Outcome: workers can lazily use approved MCP servers through one constrained gateway.

Deliverables:

- locked server descriptors
- server/tool allowlists
- lazy startup and worker-scoped shutdown
- credential-name references
- deadlines, cancellation, and evidence capture
- stdio and remote transport conformance tests
- malicious server and oversized-result fixtures

Gates:

- no direct unapproved MCP tool exposure;
- unavailable required servers fail clearly;
- no credential values enter manifests or ledgers;
- server processes do not outlive workers;
- MCP content is treated as untrusted evidence.

## Post-v1 Phase 9 — advanced orchestration

Candidates only after v1 evidence:

- nested delegation with explicit depth and inherited budgets
- additional backends
- catalog federation
- remote workers
- richer approval policies
- cost-aware model routing
- reusable cross-run evidence caches

Nested delegation remains prohibited until durable parent/child accounting, cancellation propagation, and cycle prevention are proven.

## Recommended implementation order inside each phase

1. schema and trap fixture;
2. pure validator/compiler logic;
3. fake backend or fake runtime test;
4. narrow integration;
5. observed smoke test;
6. documentation and migration check.

Do not begin a later phase to bypass a failed gate in an earlier one.

## Immediate next decisions

The remaining specification work should resolve these in order:

1. Foreground resumable core versus a separate daemon behind the thin Pi extension.
2. Manifest-specific Pi launch and supervision through Orca.
3. Exact mechanical shell policy for mutation and review.
4. Exact typed Orca integration operation.
5. ~~YAML dependency strategy while preserving Agent Forge's zero-install generator path.~~ Resolved in `af-ezi.1.1`: optional compiler-only PyYAML 6 dependency.
6. Agent Spec v2 migration scope for the existing mutable guardrail and MCP credential gaps.

Each decision should be captured before implementation begins.
