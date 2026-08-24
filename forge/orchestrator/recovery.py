"""Fail-closed recovery decisions for persisted Orca Dispatch attempts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal

from .canonical import canonical_bytes
from .ledger import LedgerConflictError, RunLedger
from .orca import OrcaClient, OrcaError, OrcaUnknownEffect
from .reducer import reduce_events

RecoveryState = Literal[
    "ready",
    "failed",
    "stopped",
    "outcome_unknown",
    "stale_handle",
    "timeout",
    "source_changed",
    "settled",
]
RecoveryIntent = Literal["resume", "cancel", "abandon"]
RecoveryAction = Literal[
    "wait",
    "collect",
    "stop",
    "abandon",
    "retry",
    "restart_read",
    "record_cancelled",
    "reconcile",
    "fail_closed",
]


class RecoveryError(OrcaError):
    """Persisted and observed backend state cannot authorize an operation."""


@dataclass(frozen=True)
class RecoveryObservation:
    state: RecoveryState
    model_turn_started: bool | None = None
    transient_startup_failure: bool = False
    retry_count: int = 0


@dataclass(frozen=True)
class RecoveryDecision:
    action: RecoveryAction
    reason: str
    retry_of: str | None = None


@dataclass(frozen=True)
class RecoveryExecution:
    dispatch_id: str
    observation: RecoveryObservation
    decision: RecoveryDecision
    evidence: dict[str, Any] | None = None
    receipt: dict[str, Any] | None = None


def decide_recovery(
    observation: RecoveryObservation,
    intent: RecoveryIntent,
    *,
    dispatch_id: str | None = None,
) -> RecoveryDecision:
    """Select one documented operation without inferring missing authority."""

    state = observation.state
    if state == "source_changed":
        return RecoveryDecision("restart_read", "restart bounded observation without the stale cursor")
    if state == "timeout":
        return RecoveryDecision("wait", "timeout is a checkpoint; reconcile before any mutation")
    if state == "stale_handle":
        return RecoveryDecision("reconcile", "stale routing metadata cannot authorize replacement or cleanup")
    if state == "settled":
        return RecoveryDecision("collect", "settled Dispatch requires collection and release accounting")
    if state == "outcome_unknown":
        if intent == "cancel":
            return RecoveryDecision("stop", "fence the exact ambiguous Dispatch before claiming cancellation")
        if intent == "abandon":
            return RecoveryDecision("abandon", "explicitly fence authority while retaining possibly-live resources")
        return RecoveryDecision("reconcile", "ambiguous outcome must not create a replacement")
    if state == "ready":
        if intent == "cancel":
            return RecoveryDecision("stop", "preserve evidence, then stop the exact supervised Dispatch")
        if intent == "abandon":
            return RecoveryDecision("abandon", "explicit abandon retains the worker resources")
        return RecoveryDecision("wait", "ready worker remains authoritative")
    if state in {"failed", "stopped"}:
        if intent == "cancel":
            return RecoveryDecision("record_cancelled", "worker is already stopped; record terminal cancellation only")
        if intent == "abandon":
            return RecoveryDecision("fail_closed", "an already stopped worker needs no abandon mutation")
        if (
            observation.model_turn_started is False
            and observation.transient_startup_failure
            and observation.retry_count == 0
            and dispatch_id
        ):
            return RecoveryDecision("retry", "one proven pre-turn transient launch retry is allowed", dispatch_id)
        return RecoveryDecision("fail_closed", "replacement lacks proof of one pre-turn transient startup failure")
    raise RecoveryError(f"unsupported recovery state {state!r}")


def _boolean(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def observation_from_worker_show(
    result: dict[str, Any],
    *,
    retry_count: int = 0,
) -> RecoveryObservation:
    worker = result.get("worker")
    if not isinstance(worker, dict):
        raise RecoveryError("worker-show returned no worker object")
    raw_state = worker.get("state")
    if raw_state in {"succeeded", "completed", "settled"}:
        state: RecoveryState = "settled"
    elif raw_state in {"ready", "failed", "stopped", "outcome_unknown"}:
        state = raw_state
    else:
        raise RecoveryError(f"worker-show returned unsupported state {raw_state!r}")
    started = _boolean(worker.get("modelTurnStarted"))
    if started is None:
        started = _boolean(worker.get("model_turn_started"))
    transient = worker.get("transientStartupFailure") is True or worker.get("transient_startup_failure") is True
    return RecoveryObservation(state, started, transient, retry_count)


class OrcaRecovery:
    """Inspect one persisted Dispatch and execute only its typed recovery action."""

    def __init__(self, client: OrcaClient, ledger: RunLedger):
        self.client = client
        self.ledger = ledger

    def inspect(
        self,
        run_id: str,
        node_id: str,
        intent: RecoveryIntent,
        *,
        retry_count: int = 0,
    ) -> RecoveryExecution:
        backend = self.ledger.read_backend(run_id)
        dispatch_id = backend["identities"].get(f"dispatch:{node_id}")
        if not dispatch_id:
            raise RecoveryError(f"no persisted Dispatch for node {node_id!r}")
        try:
            shown = self.client.worker_show(dispatch_id)
        except OrcaError as error:
            if "stale" not in str(error).lower():
                raise
            observation = RecoveryObservation("stale_handle", retry_count=retry_count)
        else:
            observation = observation_from_worker_show(shown, retry_count=retry_count)
        decision = decide_recovery(observation, intent, dispatch_id=dispatch_id)
        return RecoveryExecution(dispatch_id, observation, decision)

    def execute(self, execution: RecoveryExecution) -> RecoveryExecution:
        """Apply a prior typed decision; retry and terminal recording remain caller-owned."""

        action = execution.decision.action
        evidence = None
        receipt = None
        if action == "stop":
            evidence = self.client.worker_read(execution.dispatch_id, limit=50)
            receipt = self.client.worker_stop(execution.dispatch_id)
        elif action == "abandon":
            receipt = self.client.worker_abandon(execution.dispatch_id)
        elif action == "restart_read":
            evidence = self.client.worker_read(execution.dispatch_id, cursor=None, limit=50)
        elif action in {"wait", "collect", "retry", "record_cancelled", "reconcile", "fail_closed"}:
            pass
        else:  # pragma: no cover - RecoveryAction is closed, retained as a runtime trap
            raise RecoveryError(f"unsupported recovery action {action!r}")
        return RecoveryExecution(
            execution.dispatch_id,
            execution.observation,
            execution.decision,
            evidence,
            receipt,
        )

    def cancel(self, run_id: str, node_id: str, reason: str) -> RecoveryExecution:
        """Persist intent, preserve evidence, stop exact authority, then publish cancellation."""

        if not isinstance(reason, str) or not reason:
            raise RecoveryError("cancellation reason must be non-empty")
        existing_path = self.ledger.root / run_id / "cancellation.json"
        if existing_path.exists():
            existing = self.ledger.read_cancellation(run_id)
            if existing["workerId"] != node_id or existing["reason"] != reason:
                raise LedgerConflictError("cancellation intent already binds different worker or reason")
            if existing["status"] in {"stopped", "already-stopped"}:
                self._finalize_cancelled(run_id, node_id, reason)
                return RecoveryExecution(
                    existing["dispatchId"],
                    RecoveryObservation("stopped"),
                    RecoveryDecision("record_cancelled", "durable cancellation already completed"),
                )
            if existing["status"] == "abandoned":
                raise RecoveryError("worker was explicitly abandoned; resources may remain live")
            if existing["status"] in {"outcome-unknown", "operation-failed"}:
                raise RecoveryError("cancellation requires explicit receipt-based reconciliation")
        inspected = self.inspect(run_id, node_id, "cancel")
        if inspected.decision.action not in {"stop", "record_cancelled"}:
            raise RecoveryError(
                f"cannot cancel from {inspected.observation.state!r}: {inspected.decision.reason}"
            )
        self.ledger.request_cancellation(run_id, node_id, inspected.dispatch_id, reason)
        try:
            executed = self.execute(inspected)
        except OrcaUnknownEffect as error:
            retry = f"retry-request={error.retry_request}" if error.retry_request else "retry receipt unavailable"
            self.ledger.update_cancellation(
                run_id,
                "outcome-unknown",
                last_error=f"{error}; {retry}",
            )
            raise
        except OrcaError as error:
            self.ledger.update_cancellation(run_id, "operation-failed", last_error=str(error)[:300])
            raise

        if executed.decision.action == "record_cancelled":
            self.ledger.update_cancellation(
                run_id,
                "already-stopped",
                backend_state=executed.observation.state,
            )
        else:
            evidence = executed.evidence or {}
            digest = "sha256:" + hashlib.sha256(canonical_bytes(evidence)).hexdigest()
            cursor = evidence.get("cursor")
            state = (executed.receipt or {}).get("state")
            self.ledger.update_cancellation(
                run_id,
                "stopped",
                evidence_source=str(evidence.get("source") or "unknown"),
                evidence_cursor=str(cursor) if cursor is not None else "",
                evidence_digest=digest,
                backend_state=str(state or "stopped"),
            )
        self._finalize_cancelled(run_id, node_id, reason)
        return executed

    def abandon(self, run_id: str, node_id: str, reason: str) -> RecoveryExecution:
        """Explicitly fence lifecycle authority without claiming resources stopped."""

        if not isinstance(reason, str) or not reason:
            raise RecoveryError("abandon reason must be non-empty")
        existing_path = self.ledger.root / run_id / "cancellation.json"
        if existing_path.exists():
            existing = self.ledger.read_cancellation(run_id)
            if existing["workerId"] != node_id or existing["reason"] != reason:
                raise LedgerConflictError("cancellation intent already binds different worker or reason")
            if existing["status"] == "abandoned":
                self._finalize_cancelled(
                    run_id, node_id, reason + "; resources may remain live"
                )
                return RecoveryExecution(
                    existing["dispatchId"],
                    RecoveryObservation("outcome_unknown"),
                    RecoveryDecision("abandon", "durable abandon already completed"),
                )
            if existing["status"] in {"stopped", "already-stopped"}:
                raise RecoveryError("worker is already proven stopped")
            if existing["status"] in {"outcome-unknown", "operation-failed"}:
                raise RecoveryError("abandon requires explicit receipt-based reconciliation")
        inspected = self.inspect(run_id, node_id, "abandon")
        if inspected.decision.action != "abandon":
            raise RecoveryError(
                f"cannot abandon from {inspected.observation.state!r}: {inspected.decision.reason}"
            )
        self.ledger.request_cancellation(run_id, node_id, inspected.dispatch_id, reason)
        try:
            executed = self.execute(inspected)
        except OrcaUnknownEffect as error:
            retry = f"retry-request={error.retry_request}" if error.retry_request else "retry receipt unavailable"
            self.ledger.update_cancellation(
                run_id,
                "outcome-unknown",
                last_error=f"{error}; {retry}",
            )
            raise
        except OrcaError as error:
            self.ledger.update_cancellation(run_id, "operation-failed", last_error=str(error)[:300])
            raise
        state = (executed.receipt or {}).get("state")
        self.ledger.update_cancellation(
            run_id,
            "abandoned",
            backend_state=str(state or "abandoned-resources-may-be-live"),
        )
        self._finalize_cancelled(run_id, node_id, reason + "; resources may remain live")
        return executed

    def _finalize_cancelled(self, run_id: str, node_id: str, reason: str) -> None:
        disposition_path = self.ledger.root / run_id / "disposition.json"
        if disposition_path.exists():
            disposition = self.ledger.read_disposition(run_id)
            if disposition["status"] != "cancelled":
                raise LedgerConflictError("run already has a non-cancelled terminal disposition")
            return
        projection = reduce_events(self.ledger.read_events(run_id).events)
        worker = projection.worker(node_id)
        if worker.status == "cancelled":
            return
        if worker.status not in {"queued", "preparing", "launched", "running"}:
            raise RecoveryError(f"cannot publish cancellation from worker state {worker.status!r}")
        self.ledger.mark_terminal(
            run_id,
            "cancelled",
            reason,
            worker_id=node_id,
        )
