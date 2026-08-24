"""Bounded worker-only bridge from a structured result to Orca settlement."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from .canonical import canonical_bytes
from .contracts import validate_contract
from .ledger import LedgerConflictError, RunLedger
from .orca import OrcaClient

_MAX_INPUT_BYTES = 128 * 1024


class WorkerSubmissionError(RuntimeError):
    """A worker attempted an invalid or duplicate lifecycle mutation."""


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise WorkerSubmissionError(f"missing required worker environment: {name}")
    return value


def _input() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
    if len(raw) > _MAX_INPUT_BYTES:
        raise WorkerSubmissionError("worker submission input exceeds its byte bound")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise WorkerSubmissionError(f"worker submission input is not valid JSON: {error}") from error
    if not isinstance(value, dict) or set(value) != {"taskId", "dispatchId", "result"}:
        raise WorkerSubmissionError("worker submission has unknown or missing fields")
    if not isinstance(value["taskId"], str) or not value["taskId"]:
        raise WorkerSubmissionError("taskId must be a non-empty string")
    if not isinstance(value["dispatchId"], str) or not value["dispatchId"]:
        raise WorkerSubmissionError("dispatchId must be a non-empty string")
    if not isinstance(value["result"], dict):
        raise WorkerSubmissionError("result must be an object")
    return value


def submit(value: dict[str, Any], *, client: OrcaClient | None = None) -> dict[str, Any]:
    root = Path(_required_env("AGENT_FORGE_DELEGATIONS")).expanduser().resolve()
    run_id = _required_env("AGENT_FORGE_RUN_ID")
    worker_id = _required_env("AGENT_FORGE_WORKER_ID")
    ledger = RunLedger(root)
    manifest = ledger.read_manifest(run_id, worker_id).to_dict()
    if manifest["permissionProfile"] != "observe":
        raise WorkerSubmissionError("bounded worker submission currently permits only observe workers")
    if "submit_worker_result" not in manifest["tools"]["allow"]:
        raise WorkerSubmissionError("worker manifest does not allow result submission")

    backend = ledger.read_backend(run_id)
    task_id = backend["identities"].get(f"task:{worker_id}")
    dispatch_id = backend["identities"].get(f"dispatch:{worker_id}")
    report_path = backend["identities"].get(f"report:{worker_id}")
    expected_report = (ledger.root / run_id / "results" / f"{worker_id}.json").resolve()
    if value["taskId"] != task_id or value["dispatchId"] != dispatch_id:
        raise WorkerSubmissionError("submitted Task/Dispatch provenance does not match the ledger")
    if not report_path or Path(report_path).resolve() != expected_report:
        raise WorkerSubmissionError("persisted worker report path is missing or invalid")

    result_value = validate_contract("worker-result", value["result"]).to_dict()
    if result_value["workerId"] != worker_id:
        raise WorkerSubmissionError("result workerId does not match this worker")
    if result_value["status"] not in {"completed", "partial"}:
        raise WorkerSubmissionError("observe workers may submit only completed or partial results")
    if not result_value["evidence"]:
        raise WorkerSubmissionError("observe worker results must include evidence")
    if result_value["changes"]:
        raise WorkerSubmissionError("observe worker results must not report changes")
    ledger.write_result(run_id, worker_id, result_value)

    digest = "sha256:" + hashlib.sha256(canonical_bytes(result_value)).hexdigest()
    marker = ledger.root / run_id / "artifacts" / f"{worker_id}.submission-attempt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = marker.read_text(encoding="utf-8").strip()
        if existing != digest:
            raise LedgerConflictError("worker submission attempt already binds different result content")
        return {"status": "ok", "state": "already-attempted", "resultDigest": digest}
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(digest + "\n")
        output.flush()
        os.fsync(output.fileno())
    ledger.update_backend(run_id, idempotency_keys={f"submit:{worker_id}": digest})

    status = result_value["status"]
    receipt = (client or OrcaClient()).worker_done(
        task_id=task_id,
        dispatch_id=dispatch_id,
        outcome="succeeded",
        subject=status,
        body=result_value["outcome"][:500],
        report_path=str(expected_report),
    )
    ledger.update_backend(run_id, identities={f"submitted:{worker_id}": "worker_done"})
    return {
        "status": "ok",
        "state": "submitted",
        "resultDigest": digest,
        "receipt": receipt,
    }


def main() -> int:
    try:
        print(json.dumps(submit(_input()), sort_keys=True))
        return 0
    except Exception as error:
        print(str(error)[:2000], file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
