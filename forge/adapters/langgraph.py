"""LangGraph adapter: emit a minimal runnable LangGraph project from a spec.

Output vocabulary (idiomatic per the LangGraph application-structure docs):

  my_agent/agent.py       single-file graph: create_agent + init_chat_model,
                          MultiServerMCPClient wiring only when the spec
                          declares MCP servers; every tool wrapped by guardrails
  my_agent/guardrails.py  stop file, side-effect allowlist + budget, receipts
  langgraph.json          graph pointer + env
  pyproject.toml          installable package (deps: langgraph, langchain,
                          langchain-mcp-adapters when MCP is declared)
  .env.example            provider key placeholders
  run.py                  manual entrypoint with stop-file + receipt handling
  SCHEDULING.md           cron options without LangSmith (system cron, launchd,
                          GitHub Actions)
  config.json             spec facts the guardrails helper reads
  skills/<name>/SKILL.md  spec skills on disk (no native LangGraph skill loader)

Deliberately minimal: a plain `graph` variable (no factory), no LangGraph
Platform / LangSmith dependency anywhere in the output.
"""

from __future__ import annotations

import json

from .common import Emitter


def _langchain_model_id(model: str) -> str:
    """'provider/model' -> 'provider:model' for init_chat_model."""
    if ":" in model or "/" not in model:
        return model
    provider, _, rest = model.partition("/")
    return f"{provider}:{rest}"


def generate(spec, out_dir) -> list[str]:
    if spec.model_for("langgraph").startswith("openai-codex"):
        from ..errors import AdapterError

        raise AdapterError(
            "openai-codex is a pi CLI auth provider with no LangChain "
            'integration; set model_overrides: {"langgraph": "openai/<model>"} '
            "in the spec"
        )
    e = Emitter(out_dir)
    e.write("my_agent/__init__.py", "")
    e.write("my_agent/agent.py", _agent_py(spec))
    e.write("my_agent/guardrails.py", _GUARDRAILS_PY)
    e.write("langgraph.json", _langgraph_json(spec))
    e.write("pyproject.toml", _pyproject_toml(spec))
    e.write(".env.example", _env_example(spec))
    e.write("run.py", _run_py(spec))
    e.write("SCHEDULING.md", _scheduling_md(spec))
    e.write("config.json", _config_json(spec))
    for skill in spec.skills:
        e.write(f"skills/{skill['name']}/SKILL.md", _skill_md(skill, spec))
    e.write("README.md", _readme_md(spec))
    return e.written


# --- agent.py ------------------------------------------------------------


def _prompt_text(spec) -> str:
    parts = [spec.purpose]
    extra = spec.system_prompt_text().strip()
    if extra:
        parts.append(extra)
    g = spec.guardrails
    if spec.skills:
        listed = "\n".join(
            f"- `{s['name']}`: {s['description']} (see skills/{s['name']}/SKILL.md)"
            for s in spec.skills
        )
        parts.append("Skills:\n" + listed)
    tools = g.get("allowed_tools")
    if tools:
        allow_t = ", ".join(f"`{t}`" for t in tools)
        parts.append(f"Tools: you may invoke only {allow_t}.")
    se = g["allowed_side_effects"]
    if se:
        allow = ", ".join(f"`{a}`" for a in se)
        parts.append(
            f"Side effects: only {allow}, at most {g['max_actions']} per run, "
            "and only through the wrapped tools (guardrails)."
        )
    else:
        parts.append("You are read-only: no side-effecting actions are allowed.")
    return "\n\n".join(parts)


def _agent_py(spec) -> str:
    model_id = _langchain_model_id(spec.model_for("langgraph"))
    prompt = _prompt_text(spec)
    mcp_block = ""
    tools_expr = "[]"
    graph_def = f'''model = init_chat_model({model_id!r})

graph = create_agent(
    model=model,
    tools={{tools_expr}},
    system_prompt=PROMPT,
)
'''
    if spec.mcp_servers:
        servers = {}
        for name, srv in spec.mcp_servers.items():
            if "command" in srv:
                entry = {
                    "command": srv["command"],
                    "args": srv.get("args", []),
                    "transport": "stdio",
                }
                if "env" in srv:
                    entry["env"] = srv["env"]
            else:
                entry = {"url": srv["url"], "transport": "streamable_http"}
                if "headers" in srv:
                    entry["headers"] = srv["headers"]
            servers[name] = entry
        mcp_block = f'''
from langchain_mcp_adapters.client import MultiServerMCPClient

MCP_SERVERS = {json.dumps(servers, indent=4)}
'''
        # MCP servers are only reachable at run time, so the graph is a
        # factory function (idiomatic langgraph.json) instead of a plain
        # module-level variable: importing this module must never connect.
        graph_def = f'''async def graph():
    """Graph factory: connects to MCP servers at run time, not import time."""
    client = MultiServerMCPClient(MCP_SERVERS)
    tools = [GUARDRAILS.wrap(t) for t in await client.get_tools()]
    model = init_chat_model({model_id!r})
    return create_agent(model=model, tools=tools, system_prompt=PROMPT)
'''
    return f'''"""{spec.name} — generated by agent-forge (spec v1, runtime: langgraph).

Do not remove the GUARDRAILS wiring below: it is the spec's guardrails
section made mechanical. To change the policy, edit the spec's guardrails
and regenerate — or make the explicit decision to delete these call sites.
"""

from langchain.agents import create_agent
from langchain.chat_models import init_chat_model

from .guardrails import GUARDRAILS
{mcp_block}
PROMPT = {prompt!r}

{graph_def.format(tools_expr=tools_expr)}'''


def _skill_md(skill, spec) -> str:
    if "body" in skill:
        body = skill["body"].strip()
    else:
        body = (spec.spec_dir / skill["file"]).read_text(encoding="utf-8").strip()
    return (
        "---\n"
        f"name: {skill['name']}\n"
        f"description: {skill['description']}\n"
        "---\n\n"
        f"{body}\n"
    )


# --- guardrails.py (emitted verbatim) --------------------------------------


_GUARDRAILS_PY = '''"""Guardrails for this agent: stop file, side-effect allowlist + budget,
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
        self.tool_calls: list[str] = []
        self.refused: list[str] = []

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
                self.refused.append(tool.name)
                return refusal
            self.tool_calls.append(tool.name)
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
            "tool_calls": self.tool_calls,
            "refused": self.refused,
            "note": note,
            "ts": int(time.time()),
        }
        path = HERE / G["receipt"]["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(receipt, indent=2) + "\\n", encoding="utf-8")


GUARDRAILS = Guardrails()
'''


# --- project files ----------------------------------------------------------


def _langgraph_json(spec) -> str:
    return (
        json.dumps(
            {
                "dependencies": ["."],
                "graphs": {spec.name: "./my_agent/agent.py:graph"},
                "env": "./.env",
            },
            indent=2,
        )
        + "\n"
    )


_PROVIDER_PACKAGES = {
    "anthropic": "langchain-anthropic",
    "openai": "langchain-openai",
    "google": "langchain-google-genai",
    "groq": "langchain-groq",
    "mistral": "langchain-mistralai",
    "ollama": "langchain-ollama",
}


def _pyproject_toml(spec) -> str:
    deps = ['"langgraph>=0.2"', '"langchain>=0.3"', '"langchain-core>=0.3"']
    model = spec.model_for("langgraph")
    provider = model.partition("/")[0] if "/" in model else ""
    pkg = _PROVIDER_PACKAGES.get(provider)
    if pkg:
        deps.append(f'"{pkg}>=0.3"')
    if spec.mcp_servers:
        deps.append('"langchain-mcp-adapters>=0.1"')
    deps_block = ",\n  ".join(deps)
    return f"""[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "{spec.name}"
version = "0.1.0"
description = "{spec.description or spec.name} (generated by agent-forge)"
requires-python = ">=3.10"
dependencies = [
  {deps_block}
]

[tool.setuptools]
packages = ["my_agent"]
"""


def _env_example(spec) -> str:
    return """# Provider API keys — set the one matching config.json's model.
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
"""


def _run_py(spec) -> str:
    return '''"""Manual run: python3 run.py "your input"  (or pipe input on stdin)."""

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
'''


def _scheduling_md(spec) -> str:
    trigger = spec.trigger
    cron_line = ""
    if trigger["type"] == "cron":
        cron_line = f"""
The spec declares cron `{trigger['schedule']}`. Two OSS options (no LangSmith
required):

**System cron:**

```cron
{trigger['schedule']} cd /path/to/{spec.name} && /path/to/venv/bin/python run.py
```

**macOS launchd:** create a plist with a StartCalendarInterval matching the
cron expression, running `run.py` in this directory.

GitHub Actions scheduled workflows also work for repo-hosted agents.
"""
    return f"""# Scheduling {spec.name}

This project runs anywhere Python runs; it does not require LangGraph
Platform. `langgraph dev` is for local development only.
{cron_line}
For interactive use, just run `python3 run.py "your input"`.
"""


def _config_json(spec) -> str:
    return (
        json.dumps(
            {
                "name": spec.name,
                "description": spec.description,
                "model": spec.model_for("langgraph"),
                "trigger": spec.trigger,
                "guardrails": spec.guardrails,
            },
            indent=2,
        )
        + "\n"
    )


def _readme_md(spec) -> str:
    g = spec.guardrails
    return f"""# {spec.name}

{spec.description or spec.purpose}

Generated by agent-forge (spec v1, runtime: langgraph).

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env  # then fill in the key for your model provider
```

## Run

```bash
python3 run.py "your question"   # manual run
langgraph dev                    # local dev server with the graph UI
```

## Pause

`touch {g['stop_file']}` — the next run writes a paused receipt and exits.
`rm {g['stop_file']}` to resume.

## Receipts

Every run writes `{g['receipt']['path']}` (verdict, actions, note, ts).

## Skills

{"Spec skills are in `skills/<name>/SKILL.md` and listed in the system prompt. LangGraph has no native skill loader." if spec.skills else "This agent declares no skills."}

## Scheduling

See `SCHEDULING.md` — no LangSmith required.
"""
