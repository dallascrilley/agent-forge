#!/bin/bash
# doc-assistant — run entrypoint.
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

CMD=(pi "${PI_ARGS[@]}" --system-prompt 'doc-assistant. Follow the appended SYSTEM.md only.' --append-system-prompt SYSTEM.md)

if [ "${1:-}" = "--dry-run" ]; then
  printf '%q ' "${CMD[@]}" "${@:2}"
  printf '\n'
  exit 0
fi

SIT_TIMEOUT_SEC=0 python3 guardrails.py run-pi sit.pi.log -- "${CMD[@]}" "$@"
