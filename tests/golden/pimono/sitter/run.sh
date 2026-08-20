#!/bin/bash
# hn-ai-sitter — run entrypoint.
# --dry-run prints the pi argv without executing.
set -euo pipefail
cd "$(dirname "$0")"

STOP=$(python3 -c 'import json; print(json.load(open("config.json"))["guardrails"]["stop_file"])')

if [ -e "$STOP" ]; then
  python3 guardrails.py write-receipt paused "stop file present"
  exit 0
fi

mapfile_args() {
  python3 -c 'import json,sys; [print(a) for a in json.load(open("harness.json"))["args"]]'
}

PI_ARGS=()
while IFS= read -r line; do PI_ARGS+=("$line"); done < <(mapfile_args)

CMD=(pi "${PI_ARGS[@]}" --system-prompt 'hn-ai-sitter. Follow the appended SYSTEM.md only.' --append-system-prompt SYSTEM.md @brief.md)

if [ "${1:-}" = "--dry-run" ]; then
  printf '%q ' "${CMD[@]}" "${@:2}"
  printf '\n'
  exit 0
fi

python3 guardrails.py lock-acquire && lock_rc=0 || lock_rc=$?
if [ "$lock_rc" -eq 2 ]; then
  exit 0
fi
if [ "$lock_rc" -ne 0 ]; then
  exit "$lock_rc"
fi
trap 'python3 guardrails.py lock-drop' EXIT

if ! python3 gatherer.py; then
  python3 guardrails.py write-receipt blocked "gather failed"
  exit 1
fi
if [ "$(tr -d '\n' < llm.txt)" = "skip" ]; then
  python3 guardrails.py write-receipt quiet "nothing to do"
  exit 0
fi

python3 guardrails.py run-pi sit.pi.log -- "${CMD[@]}" "$@"
