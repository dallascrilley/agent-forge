"""Offline repo-scout planning and read-only result invariants."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

from forge.orchestrator.ledger import RunLedger
from forge.orchestrator.orca import CommandResult, OrcaClient, OrcaObserver
from forge.orchestrator.verticals import (
    VerticalSliceError,
    assert_workspace_unchanged,
    collect_repo_scout,
    compile_pi_launch_command,
    compile_repo_scout,
    fake_repo_scout_result,
    launch_repo_scout,
    snapshot_workspace,
    validate_repo_scout_result,
)

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures" / "orchestrator"
REQUEST = json.loads((FIXTURES / "valid-contracts.json").read_text())["worker-request"]
LOCK = json.loads((REPO / "catalog" / "catalog.lock.json").read_text())
BASE_REVISION = snapshot_workspace(REPO).revision


def _scout_request():
    request = copy.deepcopy(REQUEST)
    request["workspace"]["baseRevision"] = BASE_REVISION
    return request


def test_repo_scout_compiles_observe_plan_without_side_effects():
    plan = compile_repo_scout(
        _scout_request(),
        LOCK,
        run_id="run-scout",
        worker_id="repo-scout",
        repository_id="repo-fixture",
        repository_root=FIXTURES / "context",
    )
    assert plan.manifest.to_dict()["permissionProfile"] == "observe"
    assert plan.manifest.to_dict()["tools"]["allow"] == [
        "read",
        "grep",
        "find",
        "ls",
        "submit_worker_result",
    ]


def test_repo_scout_rejects_a_stale_requested_revision_before_launch():
    with pytest.raises(VerticalSliceError, match="baseRevision does not match"):
        compile_repo_scout(
            REQUEST,
            LOCK,
            run_id="run-stale",
            worker_id="repo-scout",
            repository_id="repo-fixture",
            repository_root=REPO,
        )


def test_repo_scout_pi_command_uses_only_hash_locked_explicit_resources(tmp_path):
    plan = compile_repo_scout(
        _scout_request(),
        LOCK,
        run_id="run-scout",
        worker_id="repo-scout",
        repository_id="repo-fixture",
        repository_root=REPO,
    )
    command = compile_pi_launch_command(
        plan.manifest,
        LOCK,
        catalog_root=REPO / "catalog",
        repository_root=REPO,
    )
    argv = command.argv
    assert argv[:5] == ("pi", "--model", "openai-codex/gpt-5-mini", "--thinking", "low")
    assert "--no-extensions" in argv
    assert "--no-context-files" in argv
    assert argv[argv.index("--tools") + 1] == "read,grep,find,ls,submit_worker_result"
    assert argv[argv.index("--extension") + 1] == str(
        (REPO / "catalog/resources/worker-submit.ts").resolve()
    )
    fragments = [argv[index + 1] for index, item in enumerate(argv) if item == "--append-system-prompt"]
    assert fragments == [
        str((REPO / "catalog/prompts/worker-protocol.md").resolve()),
        str((REPO / "catalog/roles/repo-scout.md").resolve()),
        str((REPO / "catalog/policies/evidence.md").resolve()),
        str((REPO / "AGENTS.md").resolve()),
    ]
    assert command.shell.startswith("pi --model openai-codex/gpt-5-mini")

    widened = plan.manifest.to_dict()
    widened["task"] += " Expanded after compilation."
    with pytest.raises(VerticalSliceError, match="does not match manifestId"):
        compile_pi_launch_command(
            widened,
            LOCK,
            catalog_root=REPO / "catalog",
            repository_root=REPO,
        )

    catalog_copy = tmp_path / "catalog"
    shutil.copytree(REPO / "catalog", catalog_copy)
    (catalog_copy / "policies/evidence.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(VerticalSliceError, match="does not match its locked hash"):
        compile_pi_launch_command(
            plan.manifest,
            LOCK,
            catalog_root=catalog_copy,
            repository_root=REPO,
        )


def test_repo_scout_fake_launch_collect_and_restart_reconstructs_provenance(tmp_path):
    request = _scout_request()
    plan = compile_repo_scout(
        request,
        LOCK,
        run_id="run-scout-e2e",
        worker_id="repo-scout",
        repository_id="repo-fixture",
        repository_root=REPO,
    )
    ledger = RunLedger(tmp_path / "delegations")
    ledger.create_run("run-scout-e2e", request)
    ledger.write_manifest("run-scout-e2e", "repo-scout", plan.manifest)
    report_root = tmp_path / "reports"
    report_root.mkdir()
    (report_root / "report.json").write_text(
        json.dumps(fake_repo_scout_result("repo-scout", "forge/orchestrator/verticals.py").to_dict()),
        encoding="utf-8",
    )
    calls = []

    def runner(argv):
        calls.append(tuple(argv))
        command = " ".join(argv)
        if command.startswith("orca status"):
            result = {"runtime": {"capabilities": ["orchestration.contract.v1"]}}
        elif "run-create" in command:
            result = {"run": {"id": "native-run"}}
        elif "task-create" in command:
            result = {"task": {"id": "native-task"}}
        elif "terminal create" in command:
            result = {"terminal": {"handle": "term-scout"}}
        elif "terminal wait" in command:
            result = {"timedOut": False}
        elif "worker-start" in command:
            result = {"dispatchId": "native-dispatch"}
        elif "orchestration check" in command and "--ack" not in command:
            result = {
                "deliveryId": "delivery-scout",
                "messages": [
                    {
                        "type": "worker_done",
                        "payload": json.dumps(
                            {
                                "taskId": "native-task",
                                "dispatchId": "native-dispatch",
                                "outcome": "succeeded",
                                "reportPath": "report.json",
                            }
                        ),
                    }
                ],
                "timedOut": False,
            }
        elif "--ack delivery-scout" in command:
            result = {"acknowledged": True}
        elif "worker-release" in command:
            result = {"state": "released"}
        elif "terminal close" in command:
            result = {"closed": True}
        else:
            raise AssertionError(command)
        return CommandResult(0, json.dumps({"ok": True, "result": result}))

    client = OrcaClient(runner=runner)
    launched = launch_repo_scout(
        plan,
        LOCK,
        catalog_root=REPO / "catalog",
        repository_root=REPO,
        ledger=ledger,
        client=client,
    )
    assert launched.native_run_id == "native-run"
    assert launched.task_id == "native-task"
    assert launched.dispatch_id == "native-dispatch"
    create = next(call for call in calls if "terminal create" in " ".join(call))
    pi_command = create[create.index("--command") + 1]
    assert "--no-context-files" in pi_command
    assert "AGENT_FORGE_RUN_ID=run-scout-e2e" in pi_command
    assert "--tools read,grep,find,ls,submit_worker_result" in pi_command
    assert "--tools read,grep,find,ls,submit_worker_result,bash" not in pi_command

    result = collect_repo_scout(
        "run-scout-e2e",
        "repo-scout",
        repository_root=REPO,
        ledger=ledger,
        observer=OrcaObserver(client, report_root=report_root),
        timeout_ms=1,
    )
    assert result is not None
    assert result.to_dict()["status"] == "completed"

    restarted = RunLedger(tmp_path / "delegations")
    assert restarted.read_result("run-scout-e2e", "repo-scout").to_dict() == result.to_dict()
    identities = restarted.read_backend("run-scout-e2e")["identities"]
    assert identities["run"] == "native-run"
    assert identities["task:repo-scout"] == "native-task"
    assert identities["terminal:repo-scout"] == "term-scout"
    assert identities["dispatch:repo-scout"] == "native-dispatch"
    assert identities["delivery:repo-scout"] == "delivery-scout"
    event_types = [event["type"] for event in restarted.read_events("run-scout-e2e").to_dicts()]
    assert event_types == [
        "run.requested",
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
    assert any("worker-release" in " ".join(call) for call in calls)
    assert any("terminal close" in " ".join(call) for call in calls)


def test_repo_scout_result_requires_evidence_and_unchanged_workspace(tmp_path):
    before = snapshot_workspace(REPO)
    after = copy.deepcopy(before)
    result = fake_repo_scout_result("repo-scout", "forge/spec.py")
    validated = validate_repo_scout_result(
        result,
        worker_id="repo-scout",
        workspace_before=before,
        workspace_after=after,
    )
    assert validated.to_dict()["evidence"][0]["kind"] == "file"

    changed = type(before)(before.revision, before.status + " M forge/spec.py\n")
    with pytest.raises(VerticalSliceError, match="changed the workspace"):
        validate_repo_scout_result(
            result,
            worker_id="repo-scout",
            workspace_before=before,
            workspace_after=changed,
        )


def test_repo_scout_rejects_changes_and_missing_evidence():
    before = type("Snapshot", (), {"revision": "abc", "status": ""})()
    result = fake_repo_scout_result("repo-scout", "forge/spec.py").to_dict()
    result["changes"] = [{"path": "forge/spec.py", "summary": "changed"}]
    with pytest.raises(VerticalSliceError, match="must not report changes"):
        validate_repo_scout_result(result, worker_id="repo-scout", workspace_before=before, workspace_after=before)
    result["changes"] = []
    result["evidence"] = []
    with pytest.raises(VerticalSliceError, match="must cite"):
        validate_repo_scout_result(result, worker_id="repo-scout", workspace_before=before, workspace_after=before)


def test_unchanged_detector_rejects_revision_changes():
    before = type("Snapshot", (), {"revision": "abc", "status": ""})()
    after = type("Snapshot", (), {"revision": "def", "status": ""})()
    with pytest.raises(VerticalSliceError, match="changed the workspace"):
        assert_workspace_unchanged(before, after)
