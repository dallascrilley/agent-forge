"""Re-bless golden adapter output trees after an intended generator change.

  python3 tests/bless_golden.py           # all runtimes, all example specs
"""

import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"
GOLDEN = REPO / "tests" / "golden"

TARGETS = {
    "pimono": [("sitter-spec.json", "sitter"), ("assistant-spec.json", "assistant")],
    "langgraph": [("assistant-spec.json", "assistant")],
}


def main() -> int:
    for runtime, specs in TARGETS.items():
        for spec_name, golden_name in specs:
            dest = GOLDEN / runtime / golden_name
            if dest.exists():
                shutil.rmtree(dest)
            dest.mkdir(parents=True)
            subprocess.run(
                [
                    sys.executable,
                    str(REPO / "forge" / "cli.py"),
                    "generate",
                    str(EXAMPLES / spec_name),
                    "--runtime",
                    runtime,
                    "--out",
                    str(dest),
                ],
                check=True,
                capture_output=True,
            )
            print(f"blessed {dest.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
