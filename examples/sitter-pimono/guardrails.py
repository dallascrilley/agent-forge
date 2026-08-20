"""Generic guardrails helper for a generated agent. Stdlib-only.

Reads config.json (guardrails section) from the bundle directory:

- stopped()          stop-file check; a present stop file pauses the agent
- allow(action)      allowlist + per-run budget check
- require(action)    allow() or exit 1
- write_receipt()    append the run receipt JSON

Delete the call sites in run.sh / the agent prompt contract at your own risk —
this file existing is not enforcement; being called is.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
_CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
GUARDRAILS = _CONFIG["guardrails"]


def stopped() -> bool:
    return (HERE / GUARDRAILS["stop_file"]).exists()


def _matches(pattern: str, action: str) -> bool:
    if pattern.endswith("/"):
        return action.startswith(pattern)
    return action == pattern


class Budget:
    """Per-run side-effect budget. Instantiate once per run."""

    def __init__(self):
        self.used = 0

    def allow(self, action: str) -> bool:
        allowed = any(
            _matches(p, action) for p in GUARDRAILS["allowed_side_effects"]
        )
        if allowed and self.used < GUARDRAILS["max_actions"]:
            self.used += 1
            return True
        return False

    def require(self, action: str) -> None:
        if not self.allow(action):
            raise SystemExit(f"guardrails: refused action {action!r}")


def allow(action: str) -> bool:
    """Budgetless allowlist check (for pre-flight questions)."""
    return any(
        _matches(p, action) for p in GUARDRAILS["allowed_side_effects"]
    )


def write_receipt(verdict: str, note: str, actions: list | None = None) -> None:
    receipt = {
        "verdict": verdict,
        "actions": actions or [],
        "note": note,
        "ts": int(time.time()),
    }
    path = HERE / GUARDRAILS["receipt"]["path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    # CLI: guardrails.py write-receipt <verdict> <note> [action ...]
    if len(sys.argv) >= 4 and sys.argv[1] == "write-receipt":
        write_receipt(sys.argv[2], sys.argv[3], sys.argv[4:])
    else:
        print(__doc__)
        sys.exit(2)
