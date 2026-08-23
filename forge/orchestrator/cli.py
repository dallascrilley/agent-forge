"""JSON bridge for the thin Pi conductor extension."""

from __future__ import annotations

import argparse
import json
import os
import uuid
from pathlib import Path
from typing import Any

from .canonical import verify_content_identity
from .contracts import validate_contract
from .ledger import LedgerError, RunLedger
from .reducer import reduce_events
from .resolver import ResolutionError, resolve_request

_ALLOWED_FIELDS = {
    "action",
    "maxRuns",
    "request",
    "runId",
    "workerId",
    "repositoryId",
    "repositoryRoot",
    "backend",
    "reason",
}
_ACTIONS = {"catalog", "preview", "spawn", "status", "collect", "cancel", "integrate", "digest"}


def _error(message: str, *, code: int = 2) -> int:
    print(json.dumps({"status": "error", "error": message}, sort_keys=True))
    return code


def _load_input(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("input must be an object")
    unknown = sorted(set(value) - _ALLOWED_FIELDS)
    if unknown:
        raise ValueError("unknown input field(s): " + ", ".join(unknown))
    action = value.get("action")
    if action not in _ACTIONS:
        raise ValueError(f"action must be one of {sorted(_ACTIONS)!r}")
    return value


def _catalog(cwd: Path) -> dict[str, Any]:
    path = cwd / "catalog" / "catalog.lock.json"
    lock = json.loads(path.read_text(encoding="utf-8"))
    validate_contract("catalog-lock", lock)
    if not verify_content_identity("catalog-lock", lock):
        raise ValueError("catalog lock content does not match lockId")
    return {
        "status": "ok",
        "action": "catalog",
        "catalogLockHash": lock["lockId"],
        "capabilities": [{"id": item["id"], "mode": item["mode"]} for item in lock["capabilities"]],
        "recipes": [item["id"] for item in lock["recipes"]],
        "modelTiers": [item["tier"] for item in lock["models"]],
    }


def _request(value: dict[str, Any]) -> dict[str, Any]:
    request = value.get("request")
    if not isinstance(request, dict):
        raise ValueError("request is required for this action")
    return request


def _resolve(value: dict[str, Any], cwd: Path):
    lock_path = cwd / "catalog" / "catalog.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    request = _request(value)
    return resolve_request(
        request,
        lock,
        run_id=value.get("runId", "preview-run"),
        worker_id=value.get("workerId", "preview-worker"),
        repository_id=value.get("repositoryId", "current-repository"),
        repository_root=value.get("repositoryRoot", str(cwd)),
        backend=value.get("backend", "orca-pi"),
    )


def _manifest_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "manifestId": manifest["manifestId"],
        "runId": manifest["runId"],
        "workerId": manifest["workerId"],
        "backend": manifest["backend"],
        "permissionProfile": manifest["permissionProfile"],
        "model": manifest["model"],
        "workspace": manifest["workspace"],
        "resourceIds": sorted(
            item["id"]
            for field, values in manifest["resources"].items()
            if field != "contextFiles"
            for item in values
            if "id" in item
        ),
        "tools": manifest["tools"]["allow"],
        "timeoutSeconds": manifest["budget"]["timeoutSeconds"],
    }


def _ledger(cwd: Path) -> RunLedger:
    root = os.environ.get("AGENT_FORGE_DELEGATIONS")
    return RunLedger(Path(root).expanduser() if root else Path.home() / ".pi" / "agent" / "delegations")


def _spawn(value: dict[str, Any], cwd: Path) -> dict[str, Any]:
    value = dict(value)
    value.setdefault("runId", "run-" + uuid.uuid4().hex)
    value.setdefault("workerId", "worker-" + uuid.uuid4().hex)
    manifest = _resolve(value, cwd)
    run_id = manifest.to_dict()["runId"]
    worker_id = manifest.to_dict()["workerId"]
    ledger = _ledger(cwd)
    ledger.create_run(run_id, _request(value))
    ledger.write_manifest(run_id, worker_id, manifest)
    events = ledger.read_events(run_id).events
    sequence = events[-1].to_dict()["sequence"] + 1 if events else 0
    ledger.append_event(
        {
            "schemaVersion": 1,
            "eventId": uuid.uuid4().hex,
            "runId": run_id,
            "workerId": worker_id,
            "sequence": sequence,
            "timestamp": "2026-08-23T00:00:00Z",
            "type": "worker.compiled",
            "idempotencyKey": f"{run_id}/{worker_id}/compile/1",
            "data": {},
        }
    )
    return {
        "status": "ok",
        "action": "spawn",
        "runId": run_id,
        "workerId": worker_id,
        "manifest": _manifest_summary(manifest.to_dict()),
        "state": "compiled",
        "backend": "not-started",
    }


def _status(value: dict[str, Any], cwd: Path) -> dict[str, Any]:
    ledger = _ledger(cwd)
    run_id = value.get("runId")
    if not isinstance(run_id, str) or not run_id:
        return {"status": "ok", "action": "status", "index": ledger.read_index()}
    recovery = ledger.read_events(run_id)
    projection = reduce_events(recovery.events)
    return {
        "status": "ok",
        "action": "status",
        "runId": run_id,
        "index": ledger.read_index(),
        "projection": projection.to_dict(),
        "eventCount": len(recovery.events),
        "truncatedTail": recovery.truncated,
    }


def _cancel(value: dict[str, Any], cwd: Path) -> dict[str, Any]:
    run_id = value.get("runId")
    reason = value.get("reason")
    if not isinstance(run_id, str) or not run_id or not isinstance(reason, str) or not reason:
        raise ValueError("runId and reason are required for cancel")
    ledger = _ledger(cwd)
    events = ledger.read_events(run_id).events
    projection = reduce_events(events)
    worker_id = value.get("workerId") or (projection.workers[0].worker_id if len(projection.workers) == 1 else None)
    if not isinstance(worker_id, str) or worker_id not in projection.worker_map:
        raise ValueError("cancel requires one persisted workerId")
    worker = projection.worker(worker_id)
    if worker.status == "compiled":
        for event_type, status in (("worker.policy-approved", "policy-approved"), ("worker.queued", "queued")):
            sequence = ledger.read_events(run_id).events[-1].to_dict()["sequence"] + 1
            ledger.append_event(
                {
                    "schemaVersion": 1,
                    "eventId": uuid.uuid4().hex,
                    "runId": run_id,
                    "workerId": worker_id,
                    "sequence": sequence,
                    "timestamp": "2026-08-23T00:00:00Z",
                    "type": event_type,
                    "idempotencyKey": f"{run_id}/{worker_id}/{status}/1",
                    "data": {"status": status},
                }
            )
    elif worker.status not in {"queued", "preparing"}:
        raise ValueError("cancel is only available before a backend worker is active")
    disposition = ledger.mark_terminal(run_id, "cancelled", reason, worker_id=worker_id)
    return {"status": "ok", "action": "cancel", "runId": run_id, "workerId": worker_id, "disposition": disposition}


def _digest(value: dict[str, Any], cwd: Path) -> dict[str, Any]:
    maximum = value.get("maxRuns", 10)
    if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= 20:
        raise ValueError("maxRuns must be between 1 and 20")
    ledger = _ledger(cwd)
    index = ledger.read_index()
    entries = (index["active"] + index["recent"])[:maximum]
    runs = []
    orphaned = []
    for entry in entries:
        run_id = entry["runId"]
        try:
            recovery = ledger.read_events(run_id)
            projection = reduce_events(recovery.events)
            manifests = []
            for path in sorted((ledger.root / run_id / "manifests").glob("*.json")):
                worker_id = path.stem
                manifest = ledger.read_manifest(run_id, worker_id).to_dict()
                manifests.append({"workerId": worker_id, "timeoutSeconds": manifest["budget"]["timeoutSeconds"]})
            backend = ledger.read_backend(run_id) if (ledger.root / run_id / "backend.json").exists() else None
            disposition = ledger.read_disposition(run_id) if (ledger.root / run_id / "disposition.json").exists() else None
            runs.append(
                {
                    "runId": run_id,
                    "status": entry["status"],
                    "updatedAt": entry["updatedAt"],
                    "projection": projection.to_dict(),
                    "manifests": manifests,
                    "backendIdentities": backend["identities"] if backend else {},
                    "disposition": disposition,
                    "truncatedTail": recovery.truncated,
                }
            )
        except Exception as error:
            orphaned.append({"runId": run_id, "reason": str(error)[:300]})
    return {"status": "ok", "action": "digest", "runs": runs, "orphaned": orphaned, "maxRuns": maximum}


def _collect(value: dict[str, Any], cwd: Path) -> dict[str, Any]:
    run_id = value.get("runId")
    worker_id = value.get("workerId")
    if not isinstance(run_id, str) or not run_id or not isinstance(worker_id, str) or not worker_id:
        raise ValueError("runId and workerId are required for collect")
    ledger = _ledger(cwd)
    try:
        result = ledger.read_result(run_id, worker_id).to_dict()
    except Exception as error:
        if "missing worker-result" in str(error):
            return {"status": "ok", "action": "collect", "runId": run_id, "workerId": worker_id, "state": "pending"}
        raise
    return {"status": "ok", "action": "collect", "runId": run_id, "workerId": worker_id, "state": "collected", "result": result}


def dispatch(value: dict[str, Any], cwd: str | Path) -> dict[str, Any]:
    """Execute one validated core action; exposed for no-model tests."""

    root = Path(cwd).resolve()
    action = value["action"]
    if action == "catalog":
        return _catalog(root)
    if action == "preview":
        return {"status": "ok", "action": "preview", "manifest": _manifest_summary(_resolve(value, root).to_dict())}
    if action == "spawn":
        return _spawn(value, root)
    if action == "status":
        return _status(value, root)
    if action == "digest":
        return _digest(value, root)
    if action == "collect":
        return _collect(value, root)
    if action == "cancel":
        return _cancel(value, root)
    if action == "integrate":
        raise ValueError(f"{action} is unavailable until the backend phase is implemented")
    raise ValueError(f"unsupported action {action!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m forge.orchestrator.cli")
    parser.add_argument("--input-file", required=True)
    args = parser.parse_args(argv)
    try:
        value = _load_input(args.input_file)
        print(json.dumps(dispatch(value, Path.cwd()), sort_keys=True))
        return 0
    except LedgerError as error:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": {
                        "code": "ledger-corrupt",
                        "message": str(error)[:300],
                        "orphaned": True,
                    },
                },
                sort_keys=True,
            )
        )
        return 2
    except (OSError, json.JSONDecodeError, ValueError, KeyError, ResolutionError) as error:
        return _error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
