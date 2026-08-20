"""agent-forge CLI.

  python3 -m forge.cli validate <spec.json>
  python3 -m forge.cli generate <spec.json> --runtime <name> --out <dir>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):  # allow `python3 forge/cli.py ...` direct invocation
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "forge"

from . import KNOWN_RUNTIMES, __version__
from .errors import AdapterError, SpecError
from .spec import load


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
    else:  # pragma: no cover - argparse choices guard this
        raise AdapterError(f"no adapter installed for runtime {args.runtime!r}")
    out = Path(args.out)
    written = adapter.generate(spec, out)
    for rel in written:
        print(f"wrote {out / rel}")
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

    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except (SpecError, AdapterError) as e:
        print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
