#!/usr/bin/env python3
"""Pre-LLM gather. Writes brief.md and allow.json. Prints llm=skip|run.

Replace load_items() with the real source. Default is empty (skip the model).
If SITTER_ITEMS points at a JSON array, that array is the roster.
classify() parks items (sendable=false) so a nonempty parked roster skips pi.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAME = "hn-ai-sitter"


def load_items() -> list:
    fixture = os.environ.get("SITTER_ITEMS", "")
    if not fixture:
        return []
    data = json.loads(Path(fixture).read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def _first_line(s: str) -> str:
    text = s.replace("\r\n", "\n").replace("\r", "\n")
    for line in text.split("\n"):
        t = line.strip()
        if t:
            return t
    return ""


def classify(item):
    """Return {id, sendable} or None. Replace to park items before the model."""
    if isinstance(item, str):
        ident = _first_line(item)
        return {"id": ident, "sendable": True} if ident else None
    if isinstance(item, dict):
        raw = item.get("id")
        ident = _first_line(raw if isinstance(raw, str) else "")
        if not ident:
            return None
        return {"id": ident, "sendable": bool(item.get("sendable", True))}
    return None


def main() -> int:
    try:
        rows = [r for r in (classify(i) for i in load_items()) if r]
        allowed = [r["id"] for r in rows if r["sendable"]]
        parked = [r["id"] for r in rows if not r["sendable"]]
        llm = bool(allowed)
        lines = [
            "# " + NAME + " brief",
            "",
            "llm: " + ("run" if llm else "skip"),
            "items: " + str(len(allowed)),
            "",
            "## Sendable",
        ]
        if allowed:
            lines.extend("- " + i for i in allowed)
        else:
            lines.append("(none)")
        lines += ["", "## Parked"]
        if parked:
            lines.extend("- " + i for i in parked)
        else:
            lines.append("(none)")
        lines += ["", "## Allowlist", ", ".join(allowed) or "(none)", ""]
        (HERE / "brief.md").write_text("\n".join(lines), encoding="utf-8")
        (HERE / "allow.json").write_text(
            json.dumps({"allowed": allowed, "ts": int(time.time())}) + "\n",
            encoding="utf-8",
        )
        (HERE / "llm.txt").write_text(("run" if llm else "skip") + "\n", encoding="utf-8")
        print(HERE / "brief.md")
        print("llm=" + ("run" if llm else "skip") + " allowed=" + str(len(allowed)))
        return 0
    except Exception as exc:
        print("gatherer failed: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
