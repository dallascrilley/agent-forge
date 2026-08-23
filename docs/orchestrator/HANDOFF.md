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
- Total nodes: 54
- Dependency edges: 92
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

Two leaves are intentionally ready and may proceed in parallel:

1. `af-ezi.1.1` — choose the catalog authoring dependency boundary
2. `af-ezi.1.2` — define companion orchestration contracts

Inspect before claiming:

```bash
bd show af-ezi.1.1 --json
bd show af-ezi.1.2 --json
bd update <id> --claim --json
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
- all 54 open issues pass tracker conventions
- no tracker orphans
- the non-epic ready frontier is exactly `af-ezi.1.1` and `af-ezi.1.2`
- every phase gate depends on all required phase leaves
- the final gate depends on all eight phase gates

The installed Beads CLI has no native critical-path command. A read-only graph audit computed a 36-node longest path; dependency edges in Beads, not that cached calculation, remain authoritative.

## Recovery from context loss

After chat compaction, agent restart, or ownership transfer:

1. read this file, `spec.md`, and the claimed Bead;
2. inspect `bd ready --json` and the issue dependency tree;
3. trust the Bead's cold-executable contract over remembered conversation details;
4. append evidence and blockers to the Bead;
5. do not skip its phase review gate;
6. create newly discovered work under the owning phase with an explicit dependency.
