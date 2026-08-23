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
    OrcaUnknownEffect,
)

REPO = Path(__file__).resolve().parent.parent


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
    }
    assert sum("run-create" in " ".join(call) for call in calls) == 1
    assert sum("task-create" in " ".join(call) for call in calls) == 1
    assert sum("worker-start" in " ".join(call) for call in calls) == 1
    assert any("--terminal term-agent" in " ".join(call) for call in calls if "worker-start" in " ".join(call))


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
