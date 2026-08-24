"""Fail-closed recovery decisions for persisted Orca Dispatch attempts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .ledger import RunLedger
from .orca import OrcaClient, OrcaError

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
