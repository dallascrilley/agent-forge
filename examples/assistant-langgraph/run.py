"""Manual run: python3 run.py "your input"  (or pipe input on stdin)."""

import asyncio
import sys

from my_agent.agent import graph
from my_agent.guardrails import GUARDRAILS


async def main() -> None:
    if GUARDRAILS.stopped():
        GUARDRAILS.write_receipt("paused", "stop file present")
        return
    text = " ".join(sys.argv[1:]).strip() or sys.stdin.read().strip()
    if not text:
        GUARDRAILS.write_receipt("quiet", "no input")
        return
    g = await graph() if callable(graph) else graph
    result = await g.ainvoke(
        {"messages": [{"role": "user", "content": text}]}
    )
    last = result["messages"][-1]
    print(last.content)
    GUARDRAILS.write_receipt("acted", str(last.content)[:200])


if __name__ == "__main__":
    asyncio.run(main())
