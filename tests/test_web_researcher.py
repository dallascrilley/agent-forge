"""Offline web-researcher planning, trap, and result invariants."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from forge.orchestrator.ledger import RunLedger
from forge.orchestrator.orca import CommandResult, OrcaClient, OrcaObserver
from forge.orchestrator.verticals import (
    VerticalSliceError,
    collect_web_researcher,
    compile_pi_launch_command,
    compile_web_researcher,
    fake_web_research_result,
    launch_web_researcher,
    snapshot_workspace,
    validate_web_research_result,
)

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures" / "orchestrator"
REQUEST = json.loads((FIXTURES / "valid-contracts.json").read_text())["worker-request"]
LOCK = json.loads((REPO / "catalog" / "catalog.lock.json").read_text())
BASE_REVISION = snapshot_workspace(REPO).revision


def _research_request():
    request = copy.deepcopy(REQUEST)
    request["task"] = "Research one approved public source and return bounded URL evidence."
    request["capabilities"] = {"required": ["web.research"], "optional": []}
    request["recipe"] = "web-researcher"
    request["permissionProfile"] = "research"
    request["modelTier"] = "balanced"
    request["workspace"]["baseRevision"] = BASE_REVISION
    return request


def test_web_researcher_compiles_exact_research_profile_without_network_access():
    plan = compile_web_researcher(
        _research_request(),
        LOCK,
        run_id="run-research",
        worker_id="web-researcher",
        repository_id="repo-fixture",
        repository_root=REPO,
    )
    manifest = plan.manifest.to_dict()
    assert manifest["permissionProfile"] == "research"
    assert manifest["tools"]["allow"] == ["read", "web"]

    command = compile_pi_launch_command(
        plan.manifest,
        LOCK,
        catalog_root=REPO / "catalog",
        repository_root=REPO,
    )
    assert command.argv[command.argv.index("--tools") + 1] == "read,web"
    assert "--no-extensions" in command.argv
    assert "--no-context-files" in command.argv
    assert "--browser-cookie" not in command.argv
    assert "--mcp" not in command.argv


def test_web_researcher_fake_lifecycle_never_performs_network_io(tmp_path):
    request = _research_request()
    plan = compile_web_researcher(
        request,
        LOCK,
        run_id="run-research-e2e",
        worker_id="web-researcher",
        repository_id="repo-fixture",
        repository_root=REPO,
    )
    ledger = RunLedger(tmp_path / "delegations")
    ledger.create_run("run-research-e2e", request)
    ledger.write_manifest("run-research-e2e", "web-researcher", plan.manifest)
    report_root = tmp_path / "reports"
    report_root.mkdir()
    (report_root / "report.json").write_text(
        json.dumps(fake_web_research_result("web-researcher", "https://example.test/source").to_dict()),
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
            result = {"terminal": {"handle": "term-research"}}
        elif "terminal wait" in command:
            result = {"timedOut": False}
        elif "worker-start" in command:
            result = {"dispatchId": "native-dispatch"}
        elif "orchestration check" in command and "--ack" not in command:
            result = {
                "deliveryId": "delivery-research",
                "messages": [{"type": "worker_done", "payload": json.dumps({
                    "taskId": "native-task", "dispatchId": "native-dispatch",
                    "outcome": "succeeded", "reportPath": "report.json",
                })}],
                "timedOut": False,
            }
        elif "--ack delivery-research" in command:
            result = {"acknowledged": True}
        elif "worker-release" in command:
            result = {"state": "released"}
        elif "terminal close" in command:
            result = {"closed": True}
        else:
            raise AssertionError(command)
        return CommandResult(0, json.dumps({"ok": True, "result": result}))

    client = OrcaClient(runner=runner)
    receipt = launch_web_researcher(
        plan,
        LOCK,
        catalog_root=REPO / "catalog",
        repository_root=REPO,
        ledger=ledger,
        client=client,
    )
    assert receipt.dispatch_id == "native-dispatch"
    create = next(call for call in calls if "terminal create" in " ".join(call))
    pi_command = create[create.index("--command") + 1]
    assert "--tools read,web" in pi_command
    assert "cookie" not in pi_command.lower()
    assert "mcp" not in pi_command.lower()

    result = collect_web_researcher(
        "run-research-e2e",
        "web-researcher",
        repository_root=REPO,
        ledger=ledger,
        observer=OrcaObserver(client, report_root=report_root),
        timeout_ms=1,
    )
    assert result is not None
    assert result.to_dict()["evidence"][0]["kind"] == "url"
    assert all("http" not in " ".join(call) for call in calls)


def test_web_researcher_rejects_unbounded_or_unattributed_results():
    before = snapshot_workspace(REPO)
    result = fake_web_research_result("web-researcher", "https://example.test/source").to_dict()
    validated = validate_web_research_result(
        result,
        worker_id="web-researcher",
        workspace_before=before,
        workspace_after=before,
    )
    assert validated.to_dict()["status"] == "completed"

    result["evidence"][0]["summary"] = "source omitted"
    with pytest.raises(VerticalSliceError, match="identify its source URL"):
        validate_web_research_result(
            result,
            worker_id="web-researcher",
            workspace_before=before,
            workspace_after=before,
        )

    blocked = fake_web_research_result("web-researcher", "https://example.test/source").to_dict()
    blocked.update({"status": "blocked", "blockers": ["approved network resource unavailable"], "evidence": []})
    assert validate_web_research_result(
        blocked,
        worker_id="web-researcher",
        workspace_before=before,
        workspace_after=before,
    ).to_dict()["status"] == "blocked"
