#!/usr/bin/env python3
"""Pre-LLM gather. Writes brief.md and allow.json. Prints llm=skip|run.

Replace load_items() with the real source. Default is empty (skip the model).
If SITTER_ITEMS points at a JSON array, that array is the roster.
"""
from __future__ import annotations

import json
import os
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


def main() -> int:
    items = load_items()
    allowed = [i for i in items if isinstance(i, str)]
    llm = bool(allowed)
    lines = [
        "# " + NAME + " brief",
        "",
        "llm: " + ("run" if llm else "skip"),
        "items: " + str(len(allowed)),
        "",
        "## Items",
    ]
    if allowed:
        lines.extend("- " + i for i in allowed)
    else:
        lines.append("(none)")
    lines += ["", "## Allowlist", ", ".join(allowed) or "(none)", ""]
    (HERE / "brief.md").write_text("\n".join(lines), encoding="utf-8")
    (HERE / "allow.json").write_text(
        json.dumps({"allowed": allowed, "ts": int(time.time())}) + "\n",
        encoding="utf-8",
    )
    print(HERE / "brief.md")
    print("llm=" + ("run" if llm else "skip") + " allowed=" + str(len(allowed)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
