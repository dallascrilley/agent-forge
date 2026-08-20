# Agent instructions

## Issue tracking

- Execution source of truth: **Beads** (`bd`). Ready queue, claim, create, close, and dependencies live here.
- Linear is the human product board. Do not create or update Linear issues from coding sessions. To surface an outcome, label a bead: `bd update <id> --add-label promote:linear`.
- GitHub Issues is public intake. Pull requests and CI stay on GitHub.
- Always pass `--json` to `bd`. Do not use interactive `bd edit`.
- Never commit `.beads/embeddeddolt/`, `.beads/dolt/`, or `.beads/redirect`. Issue data syncs with `bd dolt`, not git.
