# Pi conductor implementation handoff

## Execution source of truth

Beads roadmap: `af-ezi` — **Deliver the durable catalog-driven Pi conductor**.

The Beads graph is authoritative for execution order, issue scope, acceptance criteria, and review gates. This file is only an orientation pointer; it does not mirror the issue bodies.

```bash
bd show af-ezi --json
bd ready --json
bd blocked --json
bd dep tree af-ezi.9 --direction=down --json
```

## Durable design sources

- [`spec.md`](spec.md) — accepted architecture, contracts, policies, and evidence-driven revisions
- [`roadmap.md`](roadmap.md) — capability-phase goals and release gates
- Beads `af-ezi` — executable dependency graph

The design sources were established in commit `1480626` before implementation began.

## Graph shape

- Root epic: `af-ezi`
- Capability phases: 8
- Total nodes: 57
- Dependency edges: 98
- Phase review gates: 8
- Final readiness gate: `af-ezi.9`

| Phase | Bead | Capability outcome |
|---|---|---|
| P1 | `af-ezi.1` | Deterministic trusted worker manifests |
| P2 | `af-ezi.2` | Durable DAG scheduling and recovery |
| P3 | `af-ezi.3` | Lean non-mutating Pi conductor surface |
| P4 | `af-ezi.4` | Supervised read-only Orca workers |
| P5 | `af-ezi.5` | Isolated mutation and independent review |
| P6 | `af-ezi.6` | Orca-first v1 readiness |
| P7 | `af-ezi.7` | Local Pi backend parity |
| P8 | `af-ezi.8` | Lazy allowlisted MCP gateway |

Every phase ends in an independent review gate. The final gate depends on all phase gates and stops before release, publication, production deployment, permission expansion, or nested delegation.

## Ready frontier

Phase 1 implementation leaves `af-ezi.1.1` through `af-ezi.1.5` and the audit fixes `af-ezi.1.7` through `af-ezi.1.9` are closed. The next non-epic action is the independent review gate:

1. `af-ezi.1.6` — verify deterministic manifest compilation

Inspect before claiming:

```bash
bd show af-ezi.1.6 --json
bd ready --json
bd update af-ezi.1.6 --claim --json
```

There is no project `bin/work-items` adapter, so raw `bd --json` owns selection, claim, notes, dependencies, and close operations.

## Permanent boundaries

- Runtime launch composes only audited, preinstalled resources; it never installs code.
- The model emits `WorkerRequest`; deterministic code emits `WorkerManifest`.
- The conductor remains non-mutating.
- Workers cannot delegate in v1.
- Mutation requires an isolated worktree and independent review.
- Only `verified` changes may integrate.
- No essential orchestration state may exist only in Pi context.
- Existing Agent Spec v1 remains separate from companion orchestration schemas during the first phase.
- Existing unrelated untracked files are outside roadmap ownership.

## Verification recorded at roadmap creation

```bash
bd dep cycles --json
bd lint --json
bd doctor --check=conventions
bd orphans --json
```

Observed:

- no dependency cycles
- the original 54-node graph passed tracker conventions
- no tracker orphans
- after Phase 1 implementation and three discovered-fix beads, the non-epic ready frontier is `af-ezi.1.6`
- every phase gate depends on all required phase leaves
- the final gate depends on all eight phase gates

The installed Beads CLI has no native critical-path command. A read-only graph audit computed a 36-node longest path for the original graph; dependency edges in Beads, not that cached calculation, remain authoritative.

## Current Phase 1 implementation state

The implementation worktree is `dallascrilley/pi-conductor-phase-1`. Commits `b4b6943` through `c74a255` implement the Phase 1 contract, catalog compiler, canonical documents, and resolver. Commits `f7f8284`, `356a563`, and `7ce16ec` close the independently discovered context-alias, optional-capability, and source-credential blockers. The worktree is clean; the phase gate remains open until its review evidence is accepted.

## Recovery from context loss

After chat compaction, agent restart, or ownership transfer:

1. read this file, `spec.md`, and the claimed Bead;
2. inspect `bd ready --json` and the issue dependency tree;
3. trust the Bead's cold-executable contract over remembered conversation details;
4. append evidence and blockers to the Bead;
5. do not skip its phase review gate;
6. create newly discovered work under the owning phase with an explicit dependency.
