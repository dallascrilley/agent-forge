"""pi-mono adapter: emit a pi harness bundle from a spec.

Output vocabulary (generalized from a proven sitting-agent pattern):

  harness.json   pi CLI flags (the harness)
  SYSTEM.md      system prompt + guardrails contract (the brain)
  skills/        one SKILL.md directory per spec skill
  mcp.json       standard MCP server map (only when the spec declares servers)
  config.json    name, model, trigger, guardrails — the facts guardrails.py reads
  guardrails.py  generic stop-file / allowlist / budget / receipt helper
  run.sh         sit/manual entrypoint (--dry-run prints the pi argv)
  gatherer.py    pre-LLM roster stub (cron sitters only)
  launchd/       plist template when trigger is cron
  README.md      how to run, schedule, pause

Everything is stdlib Python or POSIX shell. The bundle carries no facts about
its generator's environment.
"""

from __future__ import annotations

import json

from .common import Emitter


def generate(spec, out_dir) -> list[str]:
    e = Emitter(out_dir)
    g = spec.guardrails

    e.write("harness.json", _harness_json(spec))
    e.write("SYSTEM.md", _system_md(spec))
    for skill in spec.skills:
        e.write(f"skills/{skill['name']}/SKILL.md", _skill_md(skill, spec))
    if spec.mcp_servers:
        e.write("mcp.json", _mcp_json(spec))
    e.write("config.json", _config_json(spec))
    e.write("guardrails.py", _GUARDRAILS_PY)
    e.write("run.sh", _run_sh(spec), executable=True)
    if spec.trigger["type"] == "cron":
        e.write("gatherer.py", _gatherer_py(spec), executable=True)
        e.write(
            f"launchd/local.{spec.name}.plist",
            _launchd_plist(spec),
        )
    e.write("README.md", _readme_md(spec))
    return e.written


# --- harness -------------------------------------------------------------


def _harness_json(spec) -> str:
    args = [
        "--print",
        "--no-session",
        "--no-extensions",
    ]
    if not spec.skills:
        args.append("--no-skills")
    args += [
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--no-approve",
        "--tools",
        "read,bash,write",
        "--thinking",
        "off",
        "--model",
        spec.model,
    ]
    harness = {
        "description": f"{spec.name}: {spec.description or spec.purpose[:72]}",
        "args": args,
    }
    return json.dumps(harness, indent=2) + "\n"


# --- SYSTEM.md ------------------------------------------------------------


def _system_md(spec) -> str:
    g = spec.guardrails
    tools = g.get("allowed_tools")
    side_effects = g["allowed_side_effects"]
    tool_block = ""
    if tools:
        t_lines = "\n".join(f"  - `{t}`" for t in tools)
        tool_block = (
            "- Tools: you may invoke only these:\n" + t_lines + "\n"
        )
    if side_effects:
        se_lines = "\n".join(f"  - `{a}`" for a in side_effects)
        se_block = (
            "- You may perform only these side-effecting actions:\n" + se_lines
        )
    else:
        se_block = (
            "- You are read-only: no side-effecting actions are allowed."
        )
    quiet = (
        "If there is nothing to do, write a `quiet` receipt and stop without "
        "further work. Never manufacture work."
        if g["llm_optional"]
        else "Run the full pass every time, even when there is little to do."
    )
    parts = [
        f"# {spec.name}",
        "",
        spec.purpose,
        "",
    ]
    prompt = spec.system_prompt_text().strip()
    if prompt:
        parts += [prompt, ""]
    parts += [
        "## Operating contract (guardrails — enforced by guardrails.py)",
        "",
        f"- Stop file: if `{g['stop_file']}` exists, do nothing; write a "
        "`paused` receipt and exit.",
    ]
    if tool_block:
        parts.append(tool_block.rstrip("\n"))
    parts += [
        se_block,
        "  Perform side effects only through "
        "`python3 guardrails.py require ACTION`; never ad-hoc.",
        f"- Action budget: at most {g['max_actions']} side-effecting "
        "action(s) per run.",
        f"- Receipt: when finished, write `{g['receipt']['path']}` — JSON "
        'with `verdict` ("acted"|"quiet"|"paused"|"blocked"), `actions`, '
        '`note` (one line), `ts` (unix).',
        f"- {quiet}",
        "",
    ]
    if spec.trigger["type"] == "cron" and g["llm_optional"]:
        parts[-1:-1] = [
            "- Brief: trust the pre-gathered `brief.md`. If it says "
            "`llm: skip`, you will not be started.",
        ]
    return "\n".join(parts)


# --- skills / mcp / config -------------------------------------------------


def _skill_md(skill, spec) -> str:
    if "body" in skill:
        body = skill["body"].strip()
    else:
        body = (spec.spec_dir / skill["file"]).read_text(
            encoding="utf-8"
        ).strip()
    return (
        "---\n"
        f"name: {skill['name']}\n"
        f"description: {skill['description']}\n"
        "---\n\n"
        f"{body}\n"
    )


def _mcp_json(spec) -> str:
    servers = {}
    for name, srv in spec.mcp_servers.items():
        entry = {"command": srv["command"]} if "command" in srv else {"url": srv["url"]}
        for key in ("args", "env", "headers"):
            if key in srv:
                entry[key] = srv[key]
        servers[name] = entry
    return json.dumps({"mcpServers": servers}, indent=2) + "\n"


def _config_json(spec) -> str:
    return (
        json.dumps(
            {
                "name": spec.name,
                "description": spec.description,
                "model": spec.model,
                "trigger": spec.trigger,
                "guardrails": spec.guardrails,
            },
            indent=2,
        )
        + "\n"
    )


# --- run.sh -----------------------------------------------------------------


def _run_sh(spec) -> str:
    identity = f"{spec.name}. Follow the appended SYSTEM.md only."
    sit = spec.trigger["type"] == "cron" and spec.guardrails["llm_optional"]
    brief_arg = " @brief.md" if sit else ""
    gather = ""
    if sit:
        gather = """
python3 gatherer.py
if grep -q '^llm: skip' brief.md; then
  python3 guardrails.py write-receipt quiet "nothing to do"
  exit 0
fi
"""
    return f"""#!/bin/bash
# {spec.name} — run entrypoint.
# --dry-run prints the pi argv without executing.
set -euo pipefail
cd "$(dirname "$0")"

STOP=$(python3 -c 'import json; print(json.load(open("config.json"))["guardrails"]["stop_file"])')

if [ -e "$STOP" ]; then
  python3 guardrails.py write-receipt paused "stop file present"
  exit 0
fi

mapfile_args() {{
  python3 -c 'import json,sys; [print(a) for a in json.load(open("harness.json"))["args"]]'
}}

PI_ARGS=()
while IFS= read -r line; do PI_ARGS+=("$line"); done < <(mapfile_args)

CMD=(pi "${{PI_ARGS[@]}}" --system-prompt '{identity}' --append-system-prompt SYSTEM.md{brief_arg})

if [ "${{1:-}}" = "--dry-run" ]; then
  printf '%q ' "${{CMD[@]}}" "${{@:2}}"
  printf '\\n'
  exit 0
fi
{gather}
exec "${{CMD[@]}}" "$@"
"""


def _gatherer_py(spec) -> str:
    return '''#!/usr/bin/env python3
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
NAME = "__AGENT_NAME__"


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
    (HERE / "brief.md").write_text("\\n".join(lines), encoding="utf-8")
    (HERE / "allow.json").write_text(
        json.dumps({"allowed": allowed, "ts": int(time.time())}) + "\\n",
        encoding="utf-8",
    )
    print(HERE / "brief.md")
    print("llm=" + ("run" if llm else "skip") + " allowed=" + str(len(allowed)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''.replace("__AGENT_NAME__", spec.name)


# --- launchd plist -----------------------------------------------------------

_CRON_FIELD_TO_PLIST_KEYS = ["Minute", "Hour", "Day", "Month", "Weekday"]


def _cron_to_calendar_interval(schedule: str) -> dict:
    """Translate a 5-field cron expression to launchd StartCalendarInterval.

    Supports '*', single ints, '*/n' (minute/hour only), and comma lists.
    Cron weekday 7 maps to launchd 0 (Sunday).
    """
    fields = schedule.split()
    out = {}
    for value, key in zip(fields, _CRON_FIELD_TO_PLIST_KEYS):
        if value == "*":
            continue
        if value.startswith("*/"):
            if key not in ("Minute", "Hour"):
                raise ValueError(
                    f"*/n steps are only supported for minute/hour: {schedule!r}"
                )
            step = int(value[2:])
            limit = 60 if key == "Minute" else 24
            out[key] = list(range(0, limit, step))
            continue
        vals = [int(v) for v in value.split(",")]
        if key == "Weekday":
            vals = [0 if v == 7 else v for v in vals]
        out[key] = vals[0] if len(vals) == 1 else vals
    return out


def _plist_value(v) -> str:
    if isinstance(v, list):
        items = "".join(f"    <integer>{x}</integer>\n" for x in v)
        return f"<array>\n{items}  </array>"
    return f"<integer>{v}</integer>"


def _launchd_plist(spec) -> str:
    interval = _cron_to_calendar_interval(spec.trigger["schedule"])
    cal_lines = "".join(
        f"    <key>{k}</key>\n    {_plist_value(v)}\n"
        for k, v in interval.items()
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- Generated from cron schedule "{spec.trigger['schedule']}".
     Replace __INSTALL_DIR__ with this bundle's absolute path before installing. -->
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>local.{spec.name}</string>
  <key>ProgramArguments</key>
  <array>
    <string>__INSTALL_DIR__/run.sh</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
{cal_lines}  </dict>
  <key>RunAtLoad</key>
  <false/>
  <key>StandardOutPath</key>
  <string>__INSTALL_DIR__/logs/launchd.log</string>
  <key>StandardErrorPath</key>
  <string>__INSTALL_DIR__/logs/launchd.log</string>
</dict>
</plist>
"""


# --- bundle README ------------------------------------------------------------


def _readme_md(spec) -> str:
    g = spec.guardrails
    cron = spec.trigger["type"] == "cron"
    gather_docs = ""
    if cron and g["llm_optional"]:
        gather_docs = """
`gatherer.py` runs before the model. An empty roster writes a quiet receipt
and never starts pi. Point `SITTER_ITEMS` at a JSON array to inject a roster;
replace `load_items()` with the real source.

"""
    sched = ""
    if cron:
        sched = f"""
## Schedule (cron: `{spec.trigger['schedule']}`)

```bash
mkdir -p logs
sed "s|__INSTALL_DIR__|$(pwd)|" launchd/local.{spec.name}.plist \\
  > ~/Library/LaunchAgents/local.{spec.name}.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.{spec.name}.plist
```
"""
    return f"""# {spec.name}

{spec.description or spec.purpose}

Generated by agent-forge (spec v1, runtime: pimono). Do not hand-edit
`guardrails.py` — change the spec's `guardrails` section and regenerate.

## Run

```bash
./run.sh --dry-run   # print the pi argv
./run.sh             # run one sitting
```
{gather_docs}
Input for the model is passed as pi arguments after `--`, e.g.
`./run.sh -- @brief.md`.

## Pause

```bash
touch {g['stop_file']}    # pause (next run writes a paused receipt and exits)
rm {g['stop_file']}       # resume
```

## Receipts

Every run writes `{g['receipt']['path']}`: verdict, actions, note, ts.

## MCP servers

{"See `mcp.json` — point your runtime's MCP config at it." if spec.mcp_servers else "This agent declares no MCP servers."}
{sched}"""


# --- guardrails.py (emitted verbatim into every bundle) -----------------------

_GUARDRAILS_PY = '''"""Generic guardrails helper for a generated agent. Stdlib-only.

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
    path.write_text(json.dumps(receipt, indent=2) + "\\n", encoding="utf-8")


if __name__ == "__main__":
    # CLI:
    #   guardrails.py require <action>
    #   guardrails.py write-receipt <verdict> <note> [action ...]
    if len(sys.argv) >= 3 and sys.argv[1] == "require":
        Budget().require(sys.argv[2])
    elif len(sys.argv) >= 4 and sys.argv[1] == "write-receipt":
        write_receipt(sys.argv[2], sys.argv[3], sys.argv[4:])
    else:
        print(__doc__)
        sys.exit(2)
'''
