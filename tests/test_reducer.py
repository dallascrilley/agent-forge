"""Legal delegation transition and journal replay tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from forge.orchestrator.reducer import TransitionError, reduce_events

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "orchestrator"
EVENT = json.loads((FIXTURES / "valid-contracts.json").read_text(encoding="utf-8"))["run-event"]


def _event(sequence: int, event_type: str, *, status: str | None = None, worker_id: str = "repo-scout"):
    event = copy.deepcopy(EVENT)
    event.update(
        {
            "eventId": f"event-{sequence}",
            "runId": "run-1",
            "workerId": worker_id,
            "sequence": sequence,
            "timestamp": "2026-08-23T00:00:00Z",
            "type": event_type,
            "idempotencyKey": f"run-1/event/{sequence}",
            "data": {"status": status} if status else {},
        }
    )
    return event


def _run_request():
    return {
        "schemaVersion": 1,
        "eventId": "event-0",
        "runId": "run-1",
        "workerId": "run-1",
        "sequence": 0,
        "timestamp": "2026-08-23T00:00:00Z",
        "type": "run.requested",
        "idempotencyKey": "run-1/request/1",
        "data": {},
    }


def _valid_completion():
    names = [
        "worker.compiled",
        "worker.policy-approved",
        "worker.queued",
        "worker.prepare.requested",
        "worker.prepared",
        "worker.launch.requested",
        "worker.launched",
        "worker.running",
        "worker.completed",
    ]
    return [_run_request()] + [
        _event(index, name, status="completed" if name == "worker.completed" else None)
        for index, name in enumerate(names, 1)
    ]


def test_replay_is_deterministic_and_tracks_review_and_integration():
    events = _valid_completion() + [
        _event(10, "worker.review-pending"),
        _event(11, "worker.verified", status="verified"),
        _event(12, "worker.integrated", status="integrated"),
        _event(13, "worker.reconciled", status="integrated"),
    ]

    first = reduce_events(events)
    second = reduce_events(copy.deepcopy(events))

    assert first.to_dict() == second.to_dict()
    assert first.status == "active"
    assert first.worker("repo-scout").to_dict() == {
        "workerId": "repo-scout",
        "status": "integrated",
        "lastSequence": 13,
        "reviewVerdict": "verified",
        "disposition": "integrated",
    }


def test_illegal_predecessor_and_sequence_gap_fail_closed():
    with pytest.raises(TransitionError, match="unknown worker"):
        reduce_events([_run_request(), _event(1, "worker.running")])

    with pytest.raises(TransitionError, match="sequence must be 1"):
        reduce_events([_run_request(), _event(2, "worker.compiled")])

    with pytest.raises(TransitionError, match="first event"):
        reduce_events([_event(1, "worker.compiled")])


def test_terminal_worker_outcome_cannot_be_rewritten():
    events = _valid_completion()
    with pytest.raises(TransitionError, match="illegal predecessor"):
        reduce_events(events + [_event(10, "worker.failed", status="failed")])

    with pytest.raises(TransitionError, match="contradict"):
        reduce_events(events + [_event(10, "worker.review-pending", status="failed")])


def test_read_only_completion_cannot_be_integrated_without_review():
    with pytest.raises(TransitionError, match="illegal predecessor"):
        reduce_events(_valid_completion() + [_event(10, "worker.integrated")])


def test_cancellation_is_terminal_for_worker():
    events = [_run_request(), _event(1, "worker.compiled"), _event(2, "worker.policy-approved"), _event(3, "worker.queued")]
    state = reduce_events(events + [_event(4, "worker.cancelled", status="cancelled")])
    assert state.worker("repo-scout").status == "cancelled"
    with pytest.raises(TransitionError):
        reduce_events(events + [_event(4, "worker.cancelled", status="cancelled"), _event(5, "worker.running")])


def test_empty_and_malformed_journals_fail_closed():
    with pytest.raises(TransitionError, match="empty"):
        reduce_events([])
    malformed = _run_request()
    malformed["unexpected"] = True
    with pytest.raises(TransitionError, match="unknown field"):
        reduce_events([malformed])
