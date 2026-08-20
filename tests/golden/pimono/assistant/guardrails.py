"""Generic guardrails helper for a generated agent. Stdlib-only.

Reads config.json (guardrails section) from the bundle directory:

- stopped()          stop-file check; a present stop file pauses the agent
- allow(action)      allowlist + per-run budget check
- require(action)    allow() or exit 1
- write_receipt()    append the run receipt JSON
- lock-acquire/drop  overlap lock for cron sitters
- run-pi             argv-logged pi launch with timeout

Delete the call sites in run.sh / the agent prompt contract at your own risk —
this file existing is not enforcement; being called is.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
_CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
GUARDRAILS = _CONFIG["guardrails"]
LOCK_PATH = HERE / (_CONFIG["name"] + ".lock")


def stopped() -> bool:
    return (HERE / GUARDRAILS["stop_file"]).exists()


def _matches(pattern: str, action: str) -> bool:
    if pattern.endswith("/"):
        return action.startswith(pattern)
    return action == pattern


def _this_run_allows(action: str) -> bool:
    """If allow.json exists, action must match a this-run entry. Missing file
    means no extra gate (interactive / non-sitter bundles). Empty or unreadable
    allow file refuses everything."""
    path = HERE / "allow.json"
    if not path.exists():
        return True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        allowed = data.get("allowed") or []
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        return False
    return any(_matches(p, action) for p in allowed if isinstance(p, str))


class Budget:
    """Per-run side-effect budget. Instantiate once per run."""

    def __init__(self):
        self.used = 0

    def allow(self, action: str) -> bool:
        allowed = any(
            _matches(p, action) for p in GUARDRAILS["allowed_side_effects"]
        )
        if allowed and _this_run_allows(action) and self.used < GUARDRAILS["max_actions"]:
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
    ) and _this_run_allows(action)


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


def lock_acquire() -> int:
    """0 = acquired, 2 = a fresh lock is already held."""
    fresh = int(os.environ.get("SIT_LOCK_SEC", "720"))
    if LOCK_PATH.exists() and time.time() - LOCK_PATH.stat().st_mtime < fresh:
        return 2
    LOCK_PATH.write_text(str(int(time.time())), encoding="utf-8")
    return 0


def lock_drop() -> None:
    try:
        LOCK_PATH.unlink()
    except FileNotFoundError:
        pass


def run_pi(argv: list, log_path) -> int:
    timeout_sec = int(os.environ.get("SIT_TIMEOUT_SEC", "180"))
    log = Path(log_path)
    try:
        with log.open("w", encoding="utf-8") as logf:
            logf.write(" ".join(str(a) for a in argv) + "\n\n")
            logf.flush()
            proc = subprocess.run(
                argv,
                timeout=timeout_sec,
                cwd=str(HERE),
                stdout=logf,
                stderr=subprocess.STDOUT,
            )
        return proc.returncode
    except subprocess.TimeoutExpired:
        write_receipt("blocked", "pi timed out after %ss" % timeout_sec)
        return 1
    except FileNotFoundError:
        write_receipt("blocked", "pi not on PATH")
        return 1


if __name__ == "__main__":
    # CLI:
    #   guardrails.py require <action>
    #   guardrails.py write-receipt <verdict> <note> [action ...]
    #   guardrails.py lock-acquire | lock-drop
    #   guardrails.py run-pi <log> -- <argv...>
    if len(sys.argv) >= 3 and sys.argv[1] == "require":
        Budget().require(sys.argv[2])
    elif len(sys.argv) >= 4 and sys.argv[1] == "write-receipt":
        write_receipt(sys.argv[2], sys.argv[3], sys.argv[4:])
    elif len(sys.argv) >= 2 and sys.argv[1] == "lock-acquire":
        sys.exit(lock_acquire())
    elif len(sys.argv) >= 2 and sys.argv[1] == "lock-drop":
        lock_drop()
    elif (
        len(sys.argv) >= 5
        and sys.argv[1] == "run-pi"
        and sys.argv[3] == "--"
    ):
        sys.exit(run_pi(sys.argv[4:], sys.argv[2]))
    else:
        print(__doc__)
        sys.exit(2)
