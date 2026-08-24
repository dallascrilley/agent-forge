"""Typed Orca recovery table and no-hidden-mutation traps."""

from __future__ import annotations

import json

import pytest

from forge.orchestrator.ledger import RunLedger
from forge.orchestrator.orca import CommandResult, OrcaClient, OrcaUnknownEffect
from forge.orchestrator.recovery import (
    OrcaRecovery,
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
