"""Bounded observe-worker result submission and duplicate-mutation traps."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from forge.orchestrator.ledger import RunLedger
from forge.orchestrator.orca import CommandResult, OrcaClient, OrcaUnknownEffect
from forge.orchestrator.verticals import compile_repo_scout, fake_repo_scout_result, snapshot_workspace
from forge.orchestrator.worker_submit import WorkerSubmissionError, submit

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures" / "orchestrator"
REQUEST = json.loads((FIXTURES / "valid-contracts.json").read_text())["worker-request"]
LOCK = json.loads((REPO / "catalog" / "catalog.lock.json").read_text())


def _prepared(tmp_path, monkeypatch):
    request = copy.deepcopy(REQUEST)
    request["workspace"]["baseRevision"] = snapshot_workspace(REPO).revision
    plan = compile_repo_scout(
        request,
        LOCK,
        run_id="run-submit",
        worker_id="repo-scout",
        repository_id="repo",
        repository_root=REPO,
    )
    root = tmp_path / "delegations"
    ledger = RunLedger(root)
    ledger.create_run("run-submit", request)
    ledger.write_manifest("run-submit", "repo-scout", plan.manifest)
    report = (root / "run-submit" / "results" / "repo-scout.json").resolve()
    ledger.write_backend(
        "run-submit",
        "orca-pi",
        identities={
            "run": "native-run",
            "task:repo-scout": "native-task",
            "dispatch:repo-scout": "native-dispatch",
            "report:repo-scout": str(report),
        },
    )
    monkeypatch.setenv("AGENT_FORGE_DELEGATIONS", str(root))
    monkeypatch.setenv("AGENT_FORGE_RUN_ID", "run-submit")
    monkeypatch.setenv("AGENT_FORGE_WORKER_ID", "repo-scout")
    value = {
        "taskId": "native-task",
        "dispatchId": "native-dispatch",
        "result": fake_repo_scout_result("repo-scout", "forge/orchestrator/worker_submit.py").to_dict(),
    }
    return ledger, report, value


def test_valid_submission_writes_exact_report_then_sends_worker_done_once(tmp_path, monkeypatch):
    ledger, report, value = _prepared(tmp_path, monkeypatch)
    calls = []

    def runner(argv):
        calls.append(tuple(argv))
        assert report.is_file(), "report must be durable before worker_done"
        return CommandResult(0, json.dumps({"ok": True, "result": {"messageId": "message-1"}}))

    client = OrcaClient(runner=runner)
    first = submit(value, client=client)
    second = submit(value, client=client)
    assert first["state"] == "submitted"
    assert second["state"] == "already-attempted"
    assert len(calls) == 1
    command = calls[0]
    assert "--task-id native-task" in " ".join(command)
    assert "--dispatch-id native-dispatch" in " ".join(command)
    assert command[command.index("--report-path") + 1] == str(report)
    assert ledger.read_result("run-submit", "repo-scout").to_dict() == value["result"]
    backend = ledger.read_backend("run-submit")
    assert backend["identities"]["submitted:repo-scout"] == "worker_done"
    assert backend["idempotencyKeys"]["submit:repo-scout"] == first["resultDigest"]


def test_mismatch_or_mutating_result_fails_before_report_or_orca(tmp_path, monkeypatch):
    _ledger, report, value = _prepared(tmp_path, monkeypatch)
    calls = []
    client = OrcaClient(runner=lambda argv: calls.append(tuple(argv)))

    wrong = copy.deepcopy(value)
    wrong["dispatchId"] = "other-dispatch"
    with pytest.raises(WorkerSubmissionError, match="provenance"):
        submit(wrong, client=client)
    assert not report.exists()

    changed = copy.deepcopy(value)
    changed["result"]["changes"] = [{"path": "README.md", "summary": "changed"}]
    with pytest.raises(WorkerSubmissionError, match="must not report changes"):
        submit(changed, client=client)
    assert not report.exists()
    assert calls == []


def test_unknown_send_effect_is_never_repeated(tmp_path, monkeypatch):
    _ledger, _report, value = _prepared(tmp_path, monkeypatch)
    calls = []

    def runner(argv):
        calls.append(tuple(argv))
        return CommandResult(
            1,
            json.dumps(
                {
                    "ok": False,
                    "error": {"message": "send outcome unknown", "retryRequest": "retry-send-1"},
                }
            ),
        )

    client = OrcaClient(runner=runner)
    with pytest.raises(OrcaUnknownEffect):
        submit(value, client=client)
    assert submit(value, client=client)["state"] == "already-attempted"
    assert len(calls) == 1
