"""Crash-tolerant run registry, document, and event-journal tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from forge.orchestrator.resolver import resolve_request
from forge.orchestrator.ledger import (
    LedgerConflictError,
    LedgerCorruptionError,
    LedgerValidationError,
    RunLedger,
)

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures" / "orchestrator"
VALID = json.loads((FIXTURES / "valid-contracts.json").read_text(encoding="utf-8"))
REQUEST = VALID["worker-request"]
RESULT = VALID["worker-result"]
EVENT = VALID["run-event"]
LOCK = json.loads((REPO / "catalog" / "catalog.lock.json").read_text(encoding="utf-8"))


def _event(run_id: str, sequence: int, *, message: str = ""):
    event = copy.deepcopy(EVENT)
    event.update(
        {
            "eventId": f"event-{sequence}",
            "runId": run_id,
            "workerId": "repo-scout",
            "sequence": sequence,
            "timestamp": "2026-08-23T00:00:00Z",
            "idempotencyKey": f"{run_id}/event/{sequence}",
            "data": {"message": message} if message else {},
        }
    )
    return event


def test_create_run_is_durable_and_idempotent(tmp_path):
    ledger = RunLedger(tmp_path)
    ledger.create_run("run-1", REQUEST, timestamp="2026-08-23T00:00:00Z")
    ledger.create_run("run-1", REQUEST, timestamp="2026-08-23T00:00:00Z")

    assert ledger.read_request("run-1").to_dict() == REQUEST
    assert ledger.read_index() == {
        "schemaVersion": 1,
        "active": [
            {"runId": "run-1", "status": "requested", "updatedAt": "2026-08-23T00:00:00Z"}
        ],
        "recent": [],
    }
    assert len(ledger.read_events("run-1").events) == 1
    with pytest.raises(LedgerConflictError):
        changed = copy.deepcopy(REQUEST)
        changed["task"] = "different"
        ledger.create_run("run-1", changed)


def test_retry_after_crash_rebuilds_missing_request_event_and_index(tmp_path):
    ledger = RunLedger(tmp_path)
    ledger.create_run("run-1", REQUEST)
    (tmp_path / "run-1" / "events.jsonl").unlink()
    ledger.index_path.unlink()

    ledger.create_run("run-1", REQUEST)

    assert len(ledger.read_events("run-1").events) == 1
    assert ledger.read_index()["active"][0]["runId"] == "run-1"


def test_existing_journal_rebuilds_index_without_demoting_progress(tmp_path):
    ledger = RunLedger(tmp_path)
    ledger.create_run("run-1", REQUEST)
    event = _event("run-1", 1)
    event["type"] = "worker.compiled"
    ledger.append_event(event)
    ledger.index_path.unlink()

    ledger.create_run("run-1", REQUEST)

    assert ledger.read_index()["active"][0]["status"] == "active"


def test_contract_documents_backend_and_terminal_projection(tmp_path):
    ledger = RunLedger(tmp_path)
    ledger.create_run("run-1", REQUEST, timestamp="2026-08-23T00:00:00Z")

    manifest = resolve_request(
        REQUEST,
        LOCK,
        run_id="run-1",
        worker_id="repo-scout",
        repository_id="repo-fixture",
        repository_root=FIXTURES / "context",
    )
    ledger.write_manifest("run-1", "repo-scout", manifest)
    ledger.write_result("run-1", "repo-scout", RESULT)
    ledger.write_backend(
        "run-1",
        "orca-pi",
        identities={"run": "orca-run-1", "dispatch": "dispatch-1"},
        cursors={"delivery": 3},
        idempotency_keys={"launch": "run-1/repo-scout/launch/1"},
    )
    disposition = ledger.mark_terminal(
        "run-1", "completed", "read-only evidence collected", worker_id="repo-scout", timestamp="2026-08-23T00:01:00Z"
    )

    assert disposition["status"] == "completed"
    assert ledger.read_manifest("run-1", "repo-scout").to_dict()["runId"] == "run-1"
    assert ledger.read_result("run-1", "repo-scout").to_dict() == RESULT
    assert ledger.read_backend("run-1")["cursors"] == {"delivery": 3}
    assert ledger.read_disposition("run-1")["reason"] == "read-only evidence collected"
    assert ledger.read_index()["active"] == []
    assert ledger.read_index()["recent"][0]["status"] == "completed"
    assert ledger.read_events("run-1").events[-1].to_dict()["type"] == "worker.completed"


def test_terminal_projection_is_not_published_before_terminal_evidence(tmp_path, monkeypatch):
    ledger = RunLedger(tmp_path)
    ledger.create_run("run-1", REQUEST, timestamp="2026-08-23T00:00:00Z")

    def fail_append(_event):
        raise OSError("simulated interruption")

    monkeypatch.setattr(ledger, "append_event", fail_append)
    with pytest.raises(OSError, match="simulated interruption"):
        ledger.mark_terminal("run-1", "failed", "backend stopped")

    assert ledger.read_index()["active"][0]["status"] == "requested"
    assert ledger.read_disposition("run-1")["status"] == "failed"


def test_incomplete_tail_is_preserved_without_losing_prior_events(tmp_path):
    ledger = RunLedger(tmp_path)
    ledger.create_run("run-1", REQUEST)
    ledger.append_event(_event("run-1", 1))
    journal = tmp_path / "run-1" / "events.jsonl"
    tail = b'{"schemaVersion":1,"eventId":"incomplete"'
    with journal.open("ab") as output:
        output.write(tail)

    recovery = ledger.read_events("run-1")
    assert [event.to_dict()["sequence"] for event in recovery.events] == [0, 1]
    assert recovery.truncated
    assert recovery.corrupt_path is not None
    assert recovery.corrupt_path.read_bytes() == tail
    with pytest.raises(LedgerCorruptionError):
        ledger.append_event(_event("run-1", 2))
    assert len(ledger.read_events("run-1").events) == 2


def test_non_terminal_corruption_fails_closed(tmp_path):
    ledger = RunLedger(tmp_path)
    ledger.create_run("run-1", REQUEST)
    journal = tmp_path / "run-1" / "events.jsonl"
    journal.write_bytes(b"not-json\n" + json.dumps(_event("run-1", 1)).encode() + b"\n")

    with pytest.raises(LedgerCorruptionError, match="non-terminal"):
        ledger.read_events("run-1")


def test_secret_values_are_redacted_from_all_written_documents(tmp_path):
    ledger = RunLedger(tmp_path, secret_values=("fixture-secret",))
    ledger.create_run("run-1", REQUEST)
    ledger.append_event(_event("run-1", 1, message="fixture-secret"))
    ledger.write_backend("run-1", "orca-pi", identities={"dispatch": "fixture-secret"})
    ledger.mark_terminal("run-1", "completed", "finished: fixture-secret")

    written = b"".join(
        path.read_bytes()
        for path in (tmp_path / "run-1").rglob("*")
        if path.is_file() and not path.name.endswith(".lock")
    )
    assert b"fixture-secret" not in written
    assert b"[REDACTED]" in written


def test_read_boundary_rejects_tampered_contract(tmp_path):
    ledger = RunLedger(tmp_path)
    ledger.create_run("run-1", REQUEST)
    request_path = tmp_path / "run-1" / "request.json"
    tampered = json.loads(request_path.read_text(encoding="utf-8"))
    tampered["unexpected"] = True
    request_path.write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises((LedgerCorruptionError, LedgerValidationError)):
        ledger.read_request("run-1")
