"""Re-bless golden adapter output trees after an intended generator change.

  python3 tests/bless_golden.py           # all runtimes, all example specs
"""

import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from forge.adapters import eve, langgraph, pimono  # noqa: E402
from forge.spec import load  # noqa: E402

ADAPTERS = {"pimono": pimono, "langgraph": langgraph, "eve": eve}

EXAMPLES = REPO / "examples"
GOLDEN = REPO / "tests" / "golden"


TARGETS = {
    "pimono": [("sitter-spec.json", "sitter"), ("assistant-spec.json", "assistant")],
    "langgraph": [("assistant-spec.json", "assistant")],
    "eve": [("sitter-spec.json", "sitter"), ("assistant-spec.json", "assistant")],
}


def main() -> int:
    for runtime, specs in TARGETS.items():
        for spec_name, golden_name in specs:
            dest = GOLDEN / runtime / golden_name
            if dest.exists():
                shutil.rmtree(dest)
            dest.mkdir(parents=True)
            spec = load(EXAMPLES / spec_name)
            if runtime not in spec.runtimes:
                spec.runtimes.append(runtime)
            ADAPTERS[runtime].generate(spec, dest)
            print(f"blessed {dest.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
