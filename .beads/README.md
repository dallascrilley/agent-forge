# Beads (local tracker)

This repo uses Beads for agent execution tracking. Linear is the human product
board; GitHub Issues is public intake.

```bash
bd ready --json
bd update <id> --claim --json
bd close <id> --reason "…" --json
```

The Dolt database under `embeddeddolt/` is local-only. Do not commit it.
Promote an outcome to Linear with `bd update <id> --add-label promote:linear`.
