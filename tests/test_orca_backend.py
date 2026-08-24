"""Orca JSON adapter receipts and unknown-effect traps."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forge.orchestrator.ledger import RunLedger
from forge.orchestrator.orca import (
    CommandResult,
    OrcaBackend,
    OrcaClient,
    OrcaError,
    OrcaObserver,
    OrcaSettlementError,
    OrcaUnknownEffect,
)

REPO = Path(__file__).resolve().parent.parent
VALID_RESULT = json.loads((REPO / "tests/fixtures/orchestrator/valid-contracts.json").read_text())["worker-result"]


def test_orca_backend_persists_one_run_task_dispatch_provenance(tmp_path):
    calls = []

    def runner(argv):
        calls.append(tuple(argv))
        command = " ".join(argv)
        if "status" in command:
            return CommandResult(0, json.dumps({"ok": True, "result": {"runtime": {"capabilities": ["orchestration.contract.v1"]}}}))
        if "run-create" in command:
            return CommandResult(0, json.dumps({"ok": True, "result": {"run": {"id": "orca-run-1"}}}))
        if "task-create" in command:
            return CommandResult(0, json.dumps({"ok": True, "result": {"task": {"id": "orca-task-1"}}}))
        if "worker-start" in command:
            return CommandResult(0, json.dumps({"ok": True, "result": {"dispatchId": "orca-dispatch-1"}}))
        if "worker-show" in command:
            return CommandResult(0, json.dumps({"ok": True, "result": {"dispatch": {"id": "orca-dispatch-1"}}}))
        raise AssertionError(command)

    ledger = RunLedger(tmp_path)
    backend = OrcaBackend(OrcaClient(runner=runner), ledger)
    assert backend.ensure_run("run-1", "objective") == "orca-run-1"
    assert backend.ensure_run("run-1", "objective") == "orca-run-1"
    task = backend.ensure_task("run-1", "repo-scout", "Inspect the repository")
    assert task == "orca-task-1"
    assert backend.ensure_task("run-1", "repo-scout", "must not be sent again") == task
    dispatch = backend.launch("run-1", "repo-scout", task, "term-agent")
    assert dispatch == "orca-dispatch-1"
    assert backend.launch("run-1", "repo-scout", task, "term-agent") == dispatch

    identities = ledger.read_backend("run-1")["identities"]
    assert identities == {
        "run": "orca-run-1",
        "task:repo-scout": "orca-task-1",
        "dispatch:repo-scout": "orca-dispatch-1",
        "dispatch:repo-scout:attempt:1": "orca-dispatch-1",
        "attempt-count:repo-scout": "1",
    }
    assert sum("run-create" in " ".join(call) for call in calls) == 1
    assert sum("task-create" in " ".join(call) for call in calls) == 1
    assert sum("worker-start" in " ".join(call) for call in calls) == 1
    worker_start = next(call for call in calls if "worker-start" in " ".join(call))
    assert "--terminal term-agent" in " ".join(worker_start)
    assert "--run orca-run-1" in " ".join(worker_start)
    assert "--run run-1" not in " ".join(worker_start)


def test_one_replacement_attempt_uses_fresh_terminal_and_exact_retry_link(tmp_path):
    calls = []

    def runner(argv):
        calls.append(tuple(argv))
        command = " ".join(argv)
        if command.startswith("orca status"):
            result = {"runtime": {"capabilities": ["orchestration.contract.v1"]}}
        elif "run-create" in command:
            result = {"run": {"id": "orca-run-1"}}
        elif "task-create" in command:
            result = {"task": {"id": "orca-task-1"}}
        elif "terminal create" in command:
            result = {"terminal": {"handle": "term-retry"}}
        elif "terminal wait" in command:
            result = {"timedOut": False}
        elif "worker-start" in command and "--retry-of" in command:
            result = {"dispatchId": "dispatch-2"}
        elif "worker-start" in command:
            result = {"dispatchId": "dispatch-1"}
        elif "worker-show" in command:
            dispatch = argv[argv.index("--dispatch") + 1]
            result = {"dispatch": {"id": dispatch}}
        else:
            raise AssertionError(command)
        return CommandResult(0, json.dumps({"ok": True, "result": result}))

    ledger = RunLedger(tmp_path)
    backend = OrcaBackend(OrcaClient(runner=runner), ledger)
    task = backend.ensure_task("run-1", "repo-scout", "inspect")
    first = backend.launch("run-1", "repo-scout", task, "term-first")
    terminal = backend.ensure_retry_terminal(
        "run-1",
        "repo-scout",
        "pi --no-session",
        worktree="path:/repo",
        title="Agent Forge retry",
    )
    second = backend.launch_retry(
        "run-1",
        "repo-scout",
        task,
        terminal,
        retry_of=first,
    )
    assert second == "dispatch-2"
    assert backend.launch_retry(
        "run-1", "repo-scout", task, terminal, retry_of=first
    ) == second
    identities = ledger.read_backend("run-1")["identities"]
    assert identities["dispatch:repo-scout:attempt:1"] == "dispatch-1"
    assert identities["dispatch:repo-scout:attempt:2"] == "dispatch-2"
    assert identities["retry-of:repo-scout:attempt:2"] == "dispatch-1"
    assert identities["attempt-count:repo-scout"] == "2"
    assert identities["terminal:repo-scout:attempt:2"] == "term-retry"
    retry = next(call for call in calls if "worker-start" in " ".join(call) and "--retry-of" in call)
    assert "--retry-of dispatch-1" in " ".join(retry)
    assert "--terminal term-retry" in " ".join(retry)
    assert sum("worker-start" in " ".join(call) for call in calls) == 2

    before = len(calls)
    with pytest.raises(OrcaError, match="provenance does not match"):
        backend.launch_retry("run-1", "repo-scout", task, terminal, retry_of="wrong")
    assert len(calls) == before


def test_terminal_creation_is_exact_ready_and_reuses_its_persisted_handle(tmp_path):
    calls = []

    def runner(argv):
        calls.append(tuple(argv))
        command = " ".join(argv)
        if "terminal create" in command:
            return CommandResult(
                0,
                json.dumps({"ok": True, "result": {"terminal": {"handle": "term-scout"}}}),
            )
        if "terminal show" in command:
            return CommandResult(
                0,
                json.dumps({"ok": True, "result": {"terminal": {"handle": "term-scout"}}}),
            )
        if "terminal wait" in command:
            return CommandResult(0, json.dumps({"ok": True, "result": {"timedOut": False}}))
        raise AssertionError(command)

    ledger = RunLedger(tmp_path)
    backend = OrcaBackend(OrcaClient(runner=runner), ledger)
    command = "pi --no-session --tools read,grep,find,ls"
    first = backend.ensure_terminal(
        "run-1",
        "repo-scout",
        command,
        worktree="path:/repo",
        title="Agent Forge repo-scout run-1",
        readiness_timeout_ms=1234,
    )
    second = backend.ensure_terminal(
        "run-1",
        "repo-scout",
        "must-not-create",
        worktree="path:/repo",
        title="must-not-create",
        readiness_timeout_ms=1234,
    )
    assert first == second == "term-scout"
    assert ledger.read_backend("run-1")["identities"]["terminal:repo-scout"] == "term-scout"
    assert sum("terminal create" in " ".join(call) for call in calls) == 1
    create = next(call for call in calls if "terminal create" in " ".join(call))
    assert "--worktree path:/repo" in " ".join(create)
    assert create[create.index("--command") + 1] == command
    waits = [call for call in calls if "terminal wait" in " ".join(call)]
    assert len(waits) == 2
    assert all("--for tui-idle --timeout-ms 1234" in " ".join(call) for call in waits)


def test_terminal_readiness_timeout_retains_identity_for_recovery(tmp_path):
    calls = []

    def runner(argv):
        calls.append(tuple(argv))
        if "terminal create" in " ".join(argv):
            return CommandResult(0, json.dumps({"ok": True, "result": {"handle": "term-timeout"}}))
        return CommandResult(0, json.dumps({"ok": True, "result": {"timedOut": True}}))

    ledger = RunLedger(tmp_path)
    backend = OrcaBackend(OrcaClient(runner=runner), ledger)
    with pytest.raises(OrcaError, match="did not become ready"):
        backend.ensure_terminal(
            "run-1", "repo-scout", "pi", worktree="path:/repo", title="scout"
        )
    assert ledger.read_backend("run-1")["identities"]["terminal:repo-scout"] == "term-timeout"
    assert sum("terminal create" in " ".join(call) for call in calls) == 1


def test_launch_without_a_valid_persisted_native_run_id_fails_before_orca_activity(tmp_path):
    ledger = RunLedger(tmp_path)
    ledger.write_backend("run-missing", "orca-pi", identities={"task:repo-scout": "task-1"})
    corrupt_dir = tmp_path / "run-corrupt"
    corrupt_dir.mkdir()
    (corrupt_dir / "backend.json").write_text('{"bad":true}', encoding="utf-8")
    calls = []

    def runner(argv):
        calls.append(tuple(argv))
        raise AssertionError("launch must fail before Orca activity")

    backend = OrcaBackend(OrcaClient(runner=runner), ledger)
    with pytest.raises(OrcaError, match="no persisted native Orca Run ID"):
        backend.launch("run-missing", "repo-scout", "task-1", "term-agent")
    with pytest.raises(OrcaError, match="cannot read persisted Orca identities"):
        backend.launch("run-corrupt", "repo-scout", "task-1", "term-agent")
    assert calls == []


def test_unknown_mutation_effect_exposes_exact_retry_receipt_without_retry():
    calls = []

    def runner(argv):
        calls.append(tuple(argv))
        return CommandResult(
            1,
            json.dumps(
                {
                    "ok": False,
                    "error": {"message": "creation outcome unknown", "retryRequest": "retry-42"},
                }
            ),
        )

    client = OrcaClient(runner=runner)
    with pytest.raises(OrcaUnknownEffect) as error:
        client.run_create("objective")
    assert error.value.retry_request == "retry-42"
    assert len(calls) == 1


def test_delivery_replay_report_validation_and_release_are_idempotent(tmp_path):
    report = tmp_path / "report.json"
    report.write_text(json.dumps(VALID_RESULT), encoding="utf-8")
    calls = []

    def runner(argv):
        calls.append(tuple(argv))
        command = " ".join(argv)
        if "worker-release" in command:
            return CommandResult(0, json.dumps({"ok": True, "result": {"state": "released"}}))
        if "--ack" in command:
            return CommandResult(0, json.dumps({"ok": True, "result": {"acknowledged": True}}))
        return CommandResult(
            0,
            json.dumps(
                {
                    "ok": True,
                    "result": {
                        "deliveryId": "delivery-1",
                        "messages": [
                            {
                                "type": "worker_done",
                                "payload": json.dumps(
                                    {
                                        "taskId": "task-1",
                                        "dispatchId": "dispatch-1",
                                        "outcome": "succeeded",
                                        "reportPath": "report.json",
                                    }
                                ),
                            }
                        ],
                        "timedOut": False,
                    },
                }
            ),
        )

    observer = OrcaObserver(OrcaClient(runner=runner), report_root=tmp_path)
    delivery = observer.wait("run-1", timeout_ms=1)
    first = observer.process(delivery)
    second = observer.process(delivery)
    assert first == second
    assert first[0].result["status"] == "completed"
    observer.acknowledge("run-1", delivery)
    assert observer.release("dispatch-1")["state"] == "released"
    assert any("--ack delivery-1" in " ".join(call) for call in calls)
    assert any("worker-release" in " ".join(call) for call in calls)


def test_timeout_is_checkpoint_and_bad_report_blocks_acceptance(tmp_path):
    timeout = OrcaObserver(
        OrcaClient(
            runner=lambda argv: CommandResult(
                0,
                json.dumps({"ok": True, "result": {"deliveryId": None, "messages": [], "timedOut": True}}),
            )
        ),
        report_root=tmp_path,
    )
    assert timeout.process(timeout.wait("run-1", timeout_ms=1)) == ()

    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    observer = OrcaObserver(
        OrcaClient(
            runner=lambda argv: CommandResult(
                0,
                json.dumps(
                    {
                        "ok": True,
                        "result": {
                            "deliveryId": "delivery-bad",
                            "messages": [
                                {
                                    "type": "worker_done",
                                    "payload": json.dumps(
                                        {
                                            "taskId": "task-1",
                                            "dispatchId": "dispatch-1",
                                            "outcome": "succeeded",
                                            "reportPath": "bad.json",
                                        }
                                    ),
                                }
                            ],
                        },
                    }
                ),
            )
        ),
        report_root=tmp_path,
    )
    with pytest.raises(OrcaSettlementError, match="WorkerResult"):
        observer.process(observer.wait("run-1"))


def test_corrupt_persisted_backend_never_creates_a_replacement(tmp_path):
    ledger = RunLedger(tmp_path)
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    (run_dir / "backend.json").write_text('{"bad":true}', encoding="utf-8")
    called = []

    def runner(argv):
        called.append(argv)
        return CommandResult(0, json.dumps({"ok": True, "result": {"run": {"id": "new"}}}))

    with pytest.raises(Exception):
        OrcaBackend(OrcaClient(runner=runner), ledger).ensure_run("run-1", "do not duplicate")
    assert called == []
