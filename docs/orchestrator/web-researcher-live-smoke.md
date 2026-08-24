# Web-researcher live smoke procedure

> **Operator and spend gate. Do not run this procedure without explicit operator approval.**
>
> `spawn` creates an Orca Run, Task, terminal, and Dispatch, then starts a model-backed
> worker that may consume managed model credentials. It performs no repository mutation,
> but it is not an offline test.

## Preconditions

1. Work from a clean checkout at the revision being tested.
2. Confirm the target is a public, approved source and that its retrieval is permitted.
3. Confirm the active Orca runtime is the intended local runtime with `orca status --json`.
4. Use a fresh, disposable delegation directory. Do not reuse a prior live run ID.
5. Do not add browser-cookie, MCP, shell, write, or extension capabilities. The request below
   must resolve to exactly `read,web`.

## Approved command sequence

```bash
cd /path/to/agent-forge
base_revision="$(git rev-parse HEAD)"
input_file="$(mktemp -t agent-forge-web-research)"
delegations="$(mktemp -d -t agent-forge-web-research)"

cat >"$input_file" <<EOF
{
  "action": "preview",
  "runId": "web-research-smoke-$(date +%s)",
  "workerId": "web-researcher",
  "repositoryId": "agent-forge",
  "repositoryRoot": "$(pwd)",
  "backend": "orca-pi",
  "request": {
    "schemaVersion": 1,
    "task": "Research only the operator-approved public source and return bounded URL evidence. Do not modify the repository.",
    "acceptanceCriteria": ["Cite approved source URLs and report a bounded read-only result."],
    "capabilities": {"required": ["web.research"], "optional": []},
    "recipe": "web-researcher",
    "permissionProfile": "research",
    "workspace": {"repository": "current", "baseRevision": "$base_revision"},
    "modelTier": "balanced",
    "budget": {"timeoutMinutes": 10, "maxDelegationDepth": 0},
    "dependsOn": [],
    "resourceOverrides": []
  }
}
EOF

AGENT_FORGE_DELEGATIONS="$delegations" python3 -m forge.orchestrator.cli --input-file "$input_file"
```

The command above is preview-only. Inspect the returned manifest: it must report
`permissionProfile: research` and `tools: ["read", "web"]`. Any other tool, browser-cookie
path, MCP resource, or unexpected policy is a stop condition.

Only after the operator explicitly authorizes resource creation and credential spend, change the
already-reviewed request to `spawn` and run it:

```bash
python3 - "$input_file" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as source:
    value = json.load(source)
value["action"] = "spawn"
with open(path, "w", encoding="utf-8") as destination:
    json.dump(value, destination)
PY
AGENT_FORGE_DELEGATIONS="$delegations" python3 -m forge.orchestrator.cli --input-file "$input_file"
```

## Collection and evidence checks

After a `worker_done` delivery, use the returned `runId` with a new JSON input using
`action: "collect"` and the same disposable `AGENT_FORGE_DELEGATIONS` directory. Accept only
`completed`, `partial`, or explicit `blocked` results. A completed/partial result must contain
URL evidence with the source URL and bounded summary; a blocked result must preserve an explicit
network blocker. In either case, verify:

```bash
git status --porcelain
git rev-parse HEAD
```

Both must match the pre-spawn clean state and `$base_revision`. If the worker is still active,
ambiguous, or yields an unexpected result, stop; do not retry, replace, close, or abandon it
without separate operator authorization.
