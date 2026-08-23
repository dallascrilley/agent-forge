"""Pure reduction of durable run events into legal orchestration state."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .contracts import ContractDocument, ContractError, RunEvent, validate_contract


class TransitionError(ValueError):
    """An event is malformed, out of sequence, or has an illegal predecessor."""


@dataclass(frozen=True)
class WorkerState:
    """Immutable projection of one worker's state machine."""

    worker_id: str
    status: str
    last_sequence: int
    review_verdict: str | None = None
    disposition: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "workerId": self.worker_id,
            "status": self.status,
            "lastSequence": self.last_sequence,
            "reviewVerdict": self.review_verdict,
            "disposition": self.disposition,
        }


@dataclass(frozen=True)
class RunState:
    """Immutable, deterministic projection of a run event journal."""

    run_id: str | None
    status: str
    last_sequence: int
    workers: tuple[WorkerState, ...]
    event_ids: tuple[str, ...] = ()
    idempotency_keys: tuple[str, ...] = ()

    @property
    def worker_map(self) -> Mapping[str, WorkerState]:
        return MappingProxyType({worker.worker_id: worker for worker in self.workers})

    def worker(self, worker_id: str) -> WorkerState:
        try:
            return self.worker_map[worker_id]
        except KeyError as error:
            raise KeyError(f"unknown worker {worker_id!r}") from error

    def to_dict(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "status": self.status,
            "lastSequence": self.last_sequence,
            "workers": {
                worker.worker_id: worker.to_dict()
                for worker in self.workers
            },
            "eventIds": list(self.event_ids),
            "idempotencyKeys": list(self.idempotency_keys),
        }


# Event types that move a worker from one state to another. ``None`` means the
# event must be the first event for that worker and creates the requested state.
_TRANSITIONS: dict[str, dict[str | None, str | None]] = {
    "worker.compiled": {"requested": "compiled", None: "compiled"},
    "worker.policy-approved": {"compiled": "policy-approved"},
    "worker.queued": {"policy-approved": "queued"},
    "worker.prepare.requested": {"queued": "preparing"},
    "worker.prepared": {"preparing": "preparing"},
    "worker.launch.requested": {"preparing": "preparing"},
    "worker.launched": {"preparing": "launched"},
    "worker.running": {"launched": "running"},
    "worker.completed": {"running": "completed"},
    "worker.partial": {"running": "partial"},
    "worker.blocked": {"running": "blocked"},
    "worker.failed": {"running": "failed"},
    "worker.cancelled": {
        "queued": "cancelled",
        "preparing": "cancelled",
        "launched": "cancelled",
        "running": "cancelled",
    },
    "worker.review-pending": {
        "completed": "review-pending",
        "partial": "review-pending",
    },
    "worker.verified": {"review-pending": "verified"},
    "worker.verified-with-caveats": {"review-pending": "verified-with-caveats"},
    "worker.refuted": {"review-pending": "refuted"},
    "worker.integrated": {"verified": "integrated"},
    "worker.retained-isolated": {
        "verified": "retained-isolated",
        "verified-with-caveats": "retained-isolated",
        "refuted": "retained-isolated",
    },
    "worker.rejected": {
        "verified": "rejected",
        "verified-with-caveats": "rejected",
        "refuted": "rejected",
    },
}
_RECONCILIATION = "worker.reconciled"


def _as_event(event: dict[str, Any] | ContractDocument) -> RunEvent:
    raw = event.to_dict() if isinstance(event, ContractDocument) else copy.deepcopy(event)
    try:
        value = validate_contract("run-event", raw)
    except ContractError as error:
        raise TransitionError(str(error)) from error
    return value  # type: ignore[return-value]


def _replace_worker(state: RunState, worker: WorkerState) -> RunState:
    workers = [item for item in state.workers if item.worker_id != worker.worker_id]
    workers.append(worker)
    workers.sort(key=lambda item: item.worker_id)
    return RunState(
        state.run_id,
        state.status,
        state.last_sequence,
        tuple(workers),
        state.event_ids,
        state.idempotency_keys,
    )


def _transition_worker(state: RunState, event: dict[str, Any]) -> RunState:
    worker_id = event["workerId"]
    current = state.worker_map.get(worker_id)
    event_type = event["type"]
    if event_type == _RECONCILIATION:
        if current is None:
            raise TransitionError(f"cannot reconcile unknown worker {worker_id!r}")
        reported = event["data"].get("status")
        if reported is not None and reported != current.status:
            raise TransitionError(
                f"reconciliation status {reported!r} contradicts {current.status!r}"
            )
        return _replace_worker(
            state,
            WorkerState(
                worker_id,
                current.status,
                event["sequence"],
                current.review_verdict,
                current.disposition,
            ),
        )

    transitions = _TRANSITIONS.get(event_type)
    if transitions is None:
        raise TransitionError(f"unsupported worker event type {event_type!r}")
    predecessor = current.status if current is not None else None
    if predecessor is None and event_type != "worker.compiled":
        raise TransitionError(f"{event_type} cannot start unknown worker {worker_id!r}")
    if predecessor not in transitions:
        raise TransitionError(
            f"illegal predecessor {predecessor!r} for {event_type} on worker {worker_id!r}"
        )
    next_status = transitions[predecessor]
    assert next_status is not None
    data_status = event["data"].get("status")
    expected_status = next_status
    if data_status is not None and data_status != expected_status:
        raise TransitionError(
            f"event status {data_status!r} contradicts transition status {expected_status!r}"
        )
    if current is None:
        current = WorkerState(worker_id, "requested", event["sequence"] - 1)
    review_verdict = current.review_verdict
    disposition = current.disposition
    if event_type in {"worker.verified", "worker.verified-with-caveats", "worker.refuted"}:
        review_verdict = next_status
    if event_type in {"worker.integrated", "worker.retained-isolated", "worker.rejected"}:
        disposition = next_status
    return _replace_worker(
        state,
        WorkerState(worker_id, next_status, event["sequence"], review_verdict, disposition),
    )


def reduce_event(state: RunState | None, event: dict[str, Any] | ContractDocument) -> RunState:
    """Apply one event without mutating the input state."""

    validated = _as_event(event)
    value = validated.to_dict()
    if state is None:
        if value["sequence"] != 0 or value["type"] != "run.requested":
            raise TransitionError("the first event must be run.requested at sequence 0")
        if value["workerId"] != value["runId"]:
            raise TransitionError("run.requested must use the runId as workerId")
        return RunState(
            run_id=value["runId"],
            status="requested",
            last_sequence=0,
            workers=(),
            event_ids=(value["eventId"],),
            idempotency_keys=(value["idempotencyKey"],),
        )

    if value["runId"] != state.run_id:
        raise TransitionError(f"event belongs to run {value['runId']!r}, not {state.run_id!r}")
    expected_sequence = state.last_sequence + 1
    if value["sequence"] != expected_sequence:
        raise TransitionError(
            f"event sequence must be {expected_sequence}, got {value['sequence']}"
        )
    if value["eventId"] in state.event_ids:
        raise TransitionError(f"duplicate eventId {value['eventId']!r}")
    if value["idempotencyKey"] in state.idempotency_keys:
        raise TransitionError(f"duplicate idempotencyKey {value['idempotencyKey']!r}")
    if value["type"] == "run.requested":
        raise TransitionError("run.requested may occur only once")
    next_state = state
    if value["type"].startswith("worker."):
        next_state = _transition_worker(state, value)
        if value["type"] in _TRANSITIONS:
            next_state = RunState(
                next_state.run_id,
                "active",
                next_state.last_sequence,
                next_state.workers,
                next_state.event_ids,
                next_state.idempotency_keys,
            )
    else:
        raise TransitionError(f"unsupported event type {value['type']!r}")
    return RunState(
        next_state.run_id,
        next_state.status,
        value["sequence"],
        next_state.workers,
        next_state.event_ids + (value["eventId"],),
        next_state.idempotency_keys + (value["idempotencyKey"],),
    )


def reduce_events(events: Iterable[dict[str, Any] | ContractDocument]) -> RunState:
    """Replay a journal deterministically, rejecting gaps and contradictions."""

    state: RunState | None = None
    for event in events:
        state = reduce_event(state, event)
    if state is None:
        raise TransitionError("cannot reduce an empty event journal")
    return state
