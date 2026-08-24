"""Typed Orca recovery table and no-hidden-mutation traps."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forge.orchestrator.ledger import RunLedger
from forge.orchestrator.orca import CommandResult, OrcaClient, OrcaUnknownEffect
from forge.orchestrator.recovery import (
    OrcaRecovery,
    RecoveryError,
    RecoveryDecision,
    RecoveryExecution,
    RecoveryObservation,
    decide_recovery,
)


@pytest.mark.parametrize(
    ("state", "intent", "action"),
    [
        ("ready", "resume", "wait"),
        ("ready", "cancel", "stop"),
        ("ready", "abandon", "abandon"),
        ("outcome_unknown", "resume", "reconcile"),
        ("outcome_unknown", "cancel", "stop"),
        ("outcome_unknown", "abandon", "abandon"),
        ("stale_handle", "resume", "reconcile"),
        ("timeout", "resume", "wait"),
        ("source_changed", "resume", "restart_read"),
        ("settled", "resume", "collect"),
        ("failed", "resume", "fail_closed"),
        ("stopped", "cancel", "record_cancelled"),
    ],
)
def test_recovery_table_selects_only_documented_actions(state, intent, action):
    decision = decide_recovery(RecoveryObservation(state), intent, dispatch_id="dispatch-1")
    assert decision.action == action


def test_replacement_requires_exact_pre_turn_transient_proof_and_one_attempt():
    allowed = decide_recovery(
        RecoveryObservation(
            "failed",
            model_turn_started=False,
            transient_startup_failure=True,
            retry_count=0,
        ),
        "resume",
        dispatch_id="dispatch-old",
    )
    assert allowed.action == "retry"
    assert allowed.retry_of == "dispatch-old"

    for observation in (
        RecoveryObservation("failed", None, True, 0),
        RecoveryObservation("failed", True, True, 0),
        RecoveryObservation("failed", False, False, 0),
        RecoveryObservation("failed", False, True, 1),
    ):
        assert decide_recovery(observation, "resume", dispatch_id="dispatch-old").action == "fail_closed"


REPO = Path(__file__).resolve().parent.parent
REQUEST = json.loads(
    (REPO / "tests/fixtures/orchestrator/valid-contracts.json").read_text()
)["worker-request"]


def _active_recovery(tmp_path, runner):
    ledger = RunLedger(tmp_path)
    ledger.create_run("run-1", REQUEST, timestamp="2026-08-23T00:00:00Z")
    for sequence, event_type in enumerate(
        (
            "worker.compiled",
            "worker.policy-approved",
            "worker.queued",
            "worker.prepare.requested",
            "worker.launched",
            "worker.running",
        ),
        1,
    ):
        ledger.append_event(
            {
                "schemaVersion": 1,
                "eventId": f"recovery-{sequence}",
                "runId": "run-1",
                "workerId": "repo-scout",
                "sequence": sequence,
                "timestamp": f"2026-08-23T00:00:0{sequence}Z",
                "type": event_type,
                "idempotencyKey": f"run-1/repo-scout/{event_type}/1",
                "data": {},
            }
        )
    ledger.write_backend(
        "run-1",
        "orca-pi",
        identities={
            "run": "native-run",
            "task:repo-scout": "task-1",
            "dispatch:repo-scout": "dispatch-1",
            "dispatch:repo-scout:attempt:1": "dispatch-1",
            "attempt-count:repo-scout": "1",
        },
    )
    return OrcaRecovery(OrcaClient(runner=runner), ledger)


def _recovery(tmp_path, runner):
    ledger = RunLedger(tmp_path)
    ledger.write_backend(
        "run-1",
        "orca-pi",
        identities={"run": "native-run", "dispatch:repo-scout": "dispatch-1"},
    )
    return OrcaRecovery(OrcaClient(runner=runner), ledger)


def test_cancel_reads_bounded_evidence_before_stopping_exact_dispatch(tmp_path):
    calls = []

    def runner(argv):
        calls.append(tuple(argv))
        command = " ".join(argv)
        if "worker-show" in command:
            result = {"worker": {"state": "ready"}}
        elif "worker-read" in command:
            result = {"source": "terminal", "cursor": 7, "lines": ["partial evidence"]}
        elif "worker-stop" in command:
            result = {"state": "stopped", "dispatchId": "dispatch-1"}
        else:
            raise AssertionError(command)
        return CommandResult(0, json.dumps({"ok": True, "result": result}))

    recovery = _recovery(tmp_path, runner)
    execution = recovery.execute(recovery.inspect("run-1", "repo-scout", "cancel"))
    assert execution.decision.action == "stop"
    assert execution.evidence["cursor"] == 7
    assert execution.receipt["state"] == "stopped"
    assert [next(part for part in call if part in {"worker-show", "worker-read", "worker-stop"}) for call in calls] == [
        "worker-show",
        "worker-read",
        "worker-stop",
    ]
    assert all("dispatch-1" in call for call in calls)


def test_abandon_performs_no_read_process_or_filesystem_operation(tmp_path):
    calls = []

    def runner(argv):
        calls.append(tuple(argv))
        command = " ".join(argv)
        result = (
            {"worker": {"state": "outcome_unknown"}}
            if "worker-show" in command
            else {"state": "abandoned", "resourcesMayBeLive": True}
        )
        return CommandResult(0, json.dumps({"ok": True, "result": result}))

    recovery = _recovery(tmp_path, runner)
    execution = recovery.execute(recovery.inspect("run-1", "repo-scout", "abandon"))
    assert execution.receipt["resourcesMayBeLive"] is True
    assert sum("worker-abandon" in " ".join(call) for call in calls) == 1
    assert not any("worker-read" in " ".join(call) for call in calls)
    assert not any("worker-stop" in " ".join(call) for call in calls)


def test_source_change_restarts_read_without_old_cursor(tmp_path):
    calls = []

    def runner(argv):
        calls.append(tuple(argv))
        return CommandResult(0, json.dumps({"ok": True, "result": {"source": "terminal", "cursor": 1}}))

    recovery = _recovery(tmp_path, runner)
    execution = RecoveryExecution(
        "dispatch-1",
        RecoveryObservation("source_changed"),
        RecoveryDecision("restart_read", "drop stale cursor"),
    )
    recovery.execute(execution)
    assert len(calls) == 1
    assert "worker-read" in calls[0]
    assert "--cursor" not in calls[0]


def test_typed_retry_decision_creates_one_fresh_linked_attempt(tmp_path):
    calls = []

    def runner(argv):
        calls.append(tuple(argv))
        command = " ".join(argv)
        if "terminal create" in command:
            result = {"terminal": {"handle": "term-retry"}}
        elif "terminal wait" in command:
            result = {"timedOut": False}
        elif "worker-start" in command:
            result = {"dispatchId": "dispatch-2"}
        else:
            raise AssertionError(command)
        return CommandResult(0, json.dumps({"ok": True, "result": result}))

    recovery = _active_recovery(tmp_path, runner)
    observation = RecoveryObservation(
        "failed",
        model_turn_started=False,
        transient_startup_failure=True,
        retry_count=0,
    )
    execution = RecoveryExecution(
        "dispatch-1",
        observation,
        decide_recovery(observation, "resume", dispatch_id="dispatch-1"),
    )
    launched = recovery.launch_retry(
        execution,
        "run-1",
        "repo-scout",
        "task-1",
        "pi --no-session",
        worktree="path:/repo",
        title="retry",
    )
    assert launched.retry_of == "dispatch-1"
    assert launched.terminal_handle == "term-retry"
    assert launched.dispatch_id == "dispatch-2"
    worker_start = next(call for call in calls if "worker-start" in " ".join(call))
    assert "--retry-of dispatch-1" in " ".join(worker_start)

    before = len(calls)
    with pytest.raises(RecoveryError, match="typed retry decision"):
        recovery.launch_retry(
            RecoveryExecution(
                "dispatch-2",
                RecoveryObservation("ready"),
                RecoveryDecision("wait", "still active"),
            ),
            "run-1",
            "repo-scout",
            "task-1",
            "pi",
            worktree="path:/repo",
            title="forbidden",
        )
    assert len(calls) == before


def test_stale_retry_authorization_fails_before_terminal_creation(tmp_path):
    calls = []
    recovery = _active_recovery(
        tmp_path,
        lambda argv: calls.append(tuple(argv)),
    )
    recovery.ledger.update_backend(
        "run-1", identities={"dispatch:repo-scout": "dispatch-changed"}
    )
    observation = RecoveryObservation(
        "failed",
        model_turn_started=False,
        transient_startup_failure=True,
        retry_count=0,
    )
    execution = RecoveryExecution(
        "dispatch-1",
        observation,
        decide_recovery(observation, "resume", dispatch_id="dispatch-1"),
    )
    with pytest.raises(RecoveryError, match="changed after retry authorization"):
        recovery.launch_retry(
            execution,
            "run-1",
            "repo-scout",
            "task-1",
            "pi",
            worktree="path:/repo",
            title="must-not-create",
        )
    assert calls == []
    assert "terminal:repo-scout:attempt:2" not in recovery.ledger.read_backend("run-1")["identities"]


def test_high_level_cancel_persists_evidence_and_terminal_disposition(tmp_path):
    calls = []

    def runner(argv):
        calls.append(tuple(argv))
        command = " ".join(argv)
        if "worker-show" in command:
            result = {"worker": {"state": "ready"}}
        elif "worker-read" in command:
            result = {"source": "terminal", "cursor": "cursor-7", "lines": ["partial"]}
        elif "worker-stop" in command:
            result = {"state": "stopped"}
        else:
            raise AssertionError(command)
        return CommandResult(0, json.dumps({"ok": True, "result": result}))

    recovery = _active_recovery(tmp_path, runner)
    recovery.cancel("run-1", "repo-scout", "operator requested stop")
    cancellation = recovery.ledger.read_cancellation("run-1")
    assert cancellation["status"] == "stopped"
    assert cancellation["evidenceSource"] == "terminal"
    assert cancellation["evidenceCursor"] == "cursor-7"
    assert cancellation["evidenceDigest"].startswith("sha256:")
    assert recovery.ledger.read_disposition("run-1")["status"] == "cancelled"
    assert recovery.ledger.read_events("run-1").events[-1].to_dict()["type"] == "worker.cancelled"

    before = len(calls)
    replay = recovery.cancel("run-1", "repo-scout", "operator requested stop")
    assert replay.decision.action == "record_cancelled"
    assert len(calls) == before


def test_high_level_abandon_records_possible_live_resources_without_read_or_stop(tmp_path):
    calls = []

    def runner(argv):
        calls.append(tuple(argv))
        command = " ".join(argv)
        result = (
            {"worker": {"state": "outcome_unknown"}}
            if "worker-show" in command
            else {"state": "abandoned", "resourcesMayBeLive": True}
        )
        return CommandResult(0, json.dumps({"ok": True, "result": result}))

    recovery = _active_recovery(tmp_path, runner)
    recovery.abandon("run-1", "repo-scout", "authority cannot be proven")
    cancellation = recovery.ledger.read_cancellation("run-1")
    assert cancellation["status"] == "abandoned"
    assert "may remain live" in recovery.ledger.read_disposition("run-1")["reason"]
    assert not any("worker-read" in " ".join(call) for call in calls)
    assert not any("worker-stop" in " ".join(call) for call in calls)


def test_unknown_high_level_stop_persists_receipt_and_requires_explicit_reconciliation(tmp_path):
    calls = []

    def runner(argv):
        calls.append(tuple(argv))
        command = " ".join(argv)
        if "worker-show" in command:
            return CommandResult(0, json.dumps({"ok": True, "result": {"worker": {"state": "ready"}}}))
        if "worker-read" in command:
            return CommandResult(0, json.dumps({"ok": True, "result": {"source": "terminal"}}))
        return CommandResult(
            1,
            json.dumps(
                {
                    "ok": False,
                    "error": {"message": "unknown", "retryRequest": "retry-stop-exact"},
                }
            ),
        )

    recovery = _active_recovery(tmp_path, runner)
    with pytest.raises(OrcaUnknownEffect):
        recovery.cancel("run-1", "repo-scout", "stop")
    cancellation = recovery.ledger.read_cancellation("run-1")
    assert cancellation["status"] == "outcome-unknown"
    assert "retry-stop-exact" in cancellation["lastError"]
    before = len(calls)
    with pytest.raises(RecoveryError, match="explicit receipt-based"):
        recovery.cancel("run-1", "repo-scout", "stop")
    assert len(calls) == before
    assert not (tmp_path / "run-1" / "disposition.json").exists()


def test_unknown_stop_effect_is_exposed_once_without_hidden_retry(tmp_path):
    calls = []

    def runner(argv):
        calls.append(tuple(argv))
        command = " ".join(argv)
        if "worker-show" in command:
            return CommandResult(0, json.dumps({"ok": True, "result": {"worker": {"state": "ready"}}}))
        if "worker-read" in command:
            return CommandResult(0, json.dumps({"ok": True, "result": {"lines": []}}))
        return CommandResult(
            1,
            json.dumps(
                {
                    "ok": False,
                    "error": {"message": "stop outcome unknown", "retryRequest": "retry-stop-1"},
                }
            ),
        )

    recovery = _recovery(tmp_path, runner)
    with pytest.raises(OrcaUnknownEffect) as error:
        recovery.execute(recovery.inspect("run-1", "repo-scout", "cancel"))
    assert error.value.retry_request == "retry-stop-1"
    assert sum("worker-stop" in " ".join(call) for call in calls) == 1
