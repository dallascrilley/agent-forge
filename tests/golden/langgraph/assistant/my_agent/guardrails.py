"""Guardrails for this agent: stop file, side-effect allowlist + budget,
run receipts. Reads config.json from the bundle root. Stdlib-only.

The graph in agent.py wraps every tool with GUARDRAILS.wrap(). Removing the
wrapper is the explicit act that disables enforcement — do it deliberately.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
_CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
G = _CONFIG["guardrails"]


def _matches(pattern: str, action: str) -> bool:
    if pattern.endswith("/"):
        return action.startswith(pattern)
    return action == pattern


class Guardrails:
    def __init__(self):
        self.used = 0
        self.actions: list[str] = []

    def stopped(self) -> bool:
        return (HERE / G["stop_file"]).exists()

    def check(self, action: str) -> str | None:
        """Return a refusal message, or None when the action is allowed."""
        allowed_tools = G.get("allowed_tools")
        if allowed_tools is not None and not any(
            _matches(p, action) for p in allowed_tools
        ):
            return f"guardrails: tool {action!r} is not in allowed_tools"
        if any(_matches(p, action) for p in G["allowed_side_effects"]):
            if self.used >= G["max_actions"]:
                return (
                    f"guardrails: per-run action budget "
                    f"({G['max_actions']}) exhausted"
                )
        return None

    def record(self, action: str) -> None:
        self.used += 1
        self.actions.append(action)

    def wrap(self, tool):
        """Wrap a LangChain tool so invocation goes through check()/record().

        A refused action returns the refusal message to the model instead of
        executing — the agent sees the boundary, the side effect never happens.
        """
        from langchain_core.tools import StructuredTool

        async def _guarded(**kwargs):
            refusal = self.check(tool.name)
            if refusal is not None:
                return refusal
            if any(
                _matches(p, tool.name) for p in G["allowed_side_effects"]
            ):
                self.record(tool.name)
            return await tool.ainvoke(kwargs)

        return StructuredTool(
            name=tool.name,
            description=(tool.description or ""),
            args_schema=getattr(tool, "args_schema", None),
            coroutine=_guarded,
        )

    def write_receipt(self, verdict: str, note: str) -> None:
        receipt = {
            "verdict": verdict,
            "actions": self.actions,
            "note": note,
            "ts": int(time.time()),
        }
        path = HERE / G["receipt"]["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


GUARDRAILS = Guardrails()
