"""agent-forge CLI.

  python3 -m forge validate <spec.json>
  python3 -m forge generate <spec.json> --runtime <name> --out <dir>
  python3 -m forge new --name <slug> --purpose <text> --model <id>
      --runtime <name> --out <spec.json>
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

if __package__ in (None, ""):  # allow `python3 forge/cli.py ...` direct invocation
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "forge"

from . import KNOWN_RUNTIMES, __version__
from .errors import AdapterError, SpecError
from .spec import load, validate


def _cmd_validate(args) -> int:
    spec = load(args.spec)
    print(
        f"ok: {spec.name} (spec v1, runtimes: {', '.join(spec.runtimes)})"
    )
    return 0


def _cmd_generate(args) -> int:
    spec = load(args.spec)
    if args.runtime not in spec.runtimes:
        raise AdapterError(
            f"spec targets {spec.runtimes}, not {args.runtime!r}; "
            "add it to $.runtimes or pick a listed runtime"
        )
    if args.runtime == "pimono":
        from .adapters import pimono as adapter
    elif args.runtime == "langgraph":
        from .adapters import langgraph as adapter
    elif args.runtime == "eve":
        from .adapters import eve as adapter
    else:  # pragma: no cover - argparse choices guard this
        raise AdapterError(f"no adapter installed for runtime {args.runtime!r}")
    out = Path(args.out)
    written = adapter.generate(spec, out)
    for rel in written:
        print(f"wrote {out / rel}")
    return 0


def _parse_mcp(value: str) -> tuple[str, dict]:
    """Parse ``name=command`` into the spec's MCP server shape."""
    name, separator, command = value.partition("=")
    if not separator or not name.strip() or not command.strip():
        raise argparse.ArgumentTypeError(
            "MCP server must be NAME=COMMAND (quote COMMAND when it has spaces)"
        )
    command_parts = shlex.split(command)
    if not command_parts:
        raise argparse.ArgumentTypeError("MCP command must not be empty")
    if command_parts[0].startswith(("http://", "https://")):
        return name.strip(), {"url": command_parts[0]}
    server = {"command": command_parts[0]}
    if len(command_parts) > 1:
        server["args"] = command_parts[1:]
    return name.strip(), server


def _cmd_new(args) -> int:
    """Write a validated, portable spec from flags without prompting."""
    mcp_servers = dict(args.mcp or [])
    data = {
        "spec_version": 1,
        "name": args.name,
        "purpose": args.purpose,
        "model": args.model,
        "runtimes": args.runtimes,
        "skills": [],
        "mcp_servers": mcp_servers,
        "plugins": [],
        "trigger": (
            {"type": "cron", "schedule": args.cron}
            if args.cron
            else {"type": "manual"}
        ),
        "guardrails": {
            "allowed_side_effects": args.side_effect or [],
        },
    }
    if args.system_prompt:
        data["system_prompt"] = {"inline": args.system_prompt}

    out = Path(args.out)
    validate(data, out.resolve().parent)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="forge", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="verb", required=True)

    pv = sub.add_parser("validate", help="validate a spec file")
    pv.add_argument("spec")
    pv.set_defaults(fn=_cmd_validate)

    pg = sub.add_parser("generate", help="emit an agent bundle")
    pg.add_argument("spec")
    pg.add_argument("--runtime", required=True, choices=KNOWN_RUNTIMES)
    pg.add_argument("--out", required=True, help="output directory")
    pg.set_defaults(fn=_cmd_generate)

    pn = sub.add_parser(
        "new", help="write a validated spec without interactive prompts"
    )
    pn.add_argument("--name", required=True, help="kebab-case agent name")
    pn.add_argument("--purpose", required=True, help="what the agent should do")
    pn.add_argument("--model", required=True, help="runtime-native model id")
    pn.add_argument(
        "--runtime",
        dest="runtimes",
        required=True,
        action="append",
        choices=KNOWN_RUNTIMES,
        help="target runtime; repeat for more than one",
    )
    pn.add_argument("--out", required=True, help="spec JSON path to write")
    pn.add_argument("--cron", help="five-field cron schedule")
    pn.add_argument(
        "--mcp",
        action="append",
        type=_parse_mcp,
        metavar="NAME=COMMAND",
        help="MCP stdio command or NAME=URL; repeatable",
    )
    pn.add_argument(
        "--side-effect",
        action="append",
        default=[],
        help="allowed side effect; repeatable",
    )
    pn.add_argument("--system-prompt", help="inline system prompt")
    pn.set_defaults(fn=_cmd_new)

    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except (SpecError, AdapterError) as e:
        print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
