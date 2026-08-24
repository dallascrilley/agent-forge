"""No-model core bridge tests for the thin Pi extension."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from forge.orchestrator.cli import _load_input, dispatch
from forge.orchestrator.ledger import RunLedger, utc_now
from forge.orchestrator.orca import CommandResult, OrcaClient
from forge.orchestrator.resolver import resolve_request

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures" / "orchestrator"
VALID = json.loads((FIXTURES / "valid-contracts.json").read_text(encoding="utf-8"))
REQUEST = VALID["worker-request"]
REQUEST["workspace"]["baseRevision"] = subprocess.check_output(
    ["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True
).strip()


def _research_request():
    request = copy.deepcopy(REQUEST)
    request["task"] = "Research one approved public source and return bounded URL evidence."
    request["capabilities"] = {"required": ["web.research"], "optional": []}
    request["recipe"] = "web-researcher"
    request["permissionProfile"] = "research"
    request["modelTier"] = "balanced"
    return request


def _launch_client():
    def runner(argv):
        command = " ".join(argv)
        if command.startswith("orca status"):
            result = {"runtime": {"capabilities": ["orchestration.contract.v1"]}}
        elif "run-create" in command:
            result = {"run": {"id": "native-run"}}
        elif "task-create" in command:
            result = {"task": {"id": "native-task"}}
        elif "terminal create" in command:
            result = {"terminal": {"handle": "native-terminal"}}
        elif "terminal wait" in command:
            result = {"timedOut": False}
        elif "worker-start" in command:
            result = {"dispatchId": "native-dispatch"}
        elif "orchestration check" in command:
            result = {"deliveryId": None, "messages": [], "timedOut": True}
        else:
            raise AssertionError(command)
        return CommandResult(0, json.dumps({"ok": True, "result": result}))

    return OrcaClient(runner=runner)


def test_catalog_and_preview_are_no_model_core_actions():
    catalog = dispatch({"action": "catalog"}, REPO)
    assert catalog["status"] == "ok"
    assert "repo-scout" in catalog["recipes"]

    preview = dispatch(
        {
            "action": "preview",
            "request": REQUEST,
            "runId": "preview-run",
            "workerId": "repo-scout",
        },
        REPO,
    )
    assert preview["manifest"]["runId"] == "preview-run"
    assert preview["manifest"]["workerId"] == "repo-scout"


def test_unknown_core_input_fields_fail_before_dispatch(tmp_path):
    path = tmp_path / "input.json"
    path.write_text(json.dumps({"action": "catalog", "unexpected": True}), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown input field"):
        _load_input(str(path))


def test_spawn_status_and_collect_use_durable_core(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FORGE_DELEGATIONS", str(tmp_path / "delegations"))
    monkeypatch.setattr("forge.orchestrator.cli.utc_now", lambda: "2099-01-01T00:00:00Z")
    monkeypatch.setattr("forge.orchestrator.verticals.utc_now", lambda: "2099-01-01T00:00:00Z")
    request = copy.deepcopy(REQUEST)
    client = _launch_client()
    spawned = dispatch(
        {
            "action": "spawn",
            "request": request,
            "runId": "run-cli",
            "workerId": "repo-scout",
        },
        REPO,
        orca_client=client,
    )
    assert spawned["state"] == "running"
    assert spawned["backendIdentities"]["dispatch"] == "native-dispatch"

    status = dispatch({"action": "status", "runId": "run-cli"}, REPO)
    assert status["projection"]["runId"] == "run-cli"
    assert status["eventCount"] == 9
    events = RunLedger(tmp_path / "delegations").read_events("run-cli").to_dicts()
    assert [event["timestamp"] for event in events] == sorted(
        event["timestamp"] for event in events
    )
    assert events[1]["timestamp"] == "2099-01-01T00:00:00Z"

    collected = dispatch(
        {"action": "collect", "runId": "run-cli", "workerId": "repo-scout"},
        REPO,
        orca_client=client,
    )
    assert collected["state"] == "pending"
    digest = dispatch({"action": "digest", "maxRuns": 10}, REPO)
    assert digest["runs"][0]["runId"] == "run-cli"
    assert digest["runs"][0]["manifests"][0]["timeoutSeconds"] == 600


def test_spawn_and_collect_route_web_researcher_without_widening(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FORGE_DELEGATIONS", str(tmp_path / "delegations"))
    spawned = dispatch(
        {
            "action": "spawn",
            "request": _research_request(),
            "runId": "run-cli-research",
            "workerId": "web-researcher",
        },
        REPO,
        orca_client=_launch_client(),
    )
    assert spawned["state"] == "running"
    assert spawned["manifest"]["permissionProfile"] == "research"
    assert spawned["manifest"]["tools"] == ["read", "web"]

    collected = dispatch(
        {"action": "collect", "runId": "run-cli-research", "workerId": "web-researcher"},
        REPO,
        orca_client=_launch_client(),
    )
    assert collected["state"] == "pending"


def test_active_cancel_uses_exact_recovery_path_and_replays_without_a_second_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FORGE_DELEGATIONS", str(tmp_path / "delegations"))
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
            result = {"terminal": {"handle": "native-terminal"}}
        elif "terminal wait" in command:
            result = {"timedOut": False}
        elif "worker-start" in command:
            result = {"dispatchId": "native-dispatch"}
        elif "worker-show" in command:
            result = {"worker": {"state": "ready"}}
        elif "worker-read" in command:
            result = {"source": "terminal", "cursor": 3, "lines": ["partial"]}
        elif "worker-stop" in command:
            result = {"state": "stopped", "dispatchId": "native-dispatch"}
        else:
            raise AssertionError(command)
        return CommandResult(0, json.dumps({"ok": True, "result": result}))

    client = OrcaClient(runner=runner)
    dispatch(
        {"action": "spawn", "request": copy.deepcopy(REQUEST), "runId": "run-cli-cancel", "workerId": "repo-scout"},
        REPO,
        orca_client=client,
    )
    cancelled = dispatch(
        {"action": "cancel", "runId": "run-cli-cancel", "workerId": "repo-scout", "reason": "operator requested stop"},
        REPO,
        orca_client=client,
    )
    assert cancelled["disposition"]["status"] == "cancelled"
    assert cancelled["cancellation"]["status"] == "stopped"
    assert cancelled["recovery"] == {
        "action": "stop",
        "dispatchId": "native-dispatch",
        "backendState": "ready",
        "remoteMutation": True,
    }
    assert [next(part for part in call if part in {"worker-show", "worker-read", "worker-stop"}) for call in calls[-3:]] == [
        "worker-show",
        "worker-read",
        "worker-stop",
    ]

    before = len(calls)
    replay = dispatch(
        {"action": "cancel", "runId": "run-cli-cancel", "workerId": "repo-scout", "reason": "operator requested stop"},
        REPO,
        orca_client=client,
    )
    assert replay["recovery"]["action"] == "record_cancelled"
    assert len(calls) == before


def test_cancel_targets_a_persisted_queued_worker(tmp_path, monkeypatch):
    root = tmp_path / "delegations"
    monkeypatch.setenv("AGENT_FORGE_DELEGATIONS", str(root))
    ledger = RunLedger(root)
    ledger.create_run("run-cancel", REQUEST)
    lock = json.loads((REPO / "catalog/catalog.lock.json").read_text())
    manifest = resolve_request(
        REQUEST,
        lock,
        run_id="run-cancel",
        worker_id="repo-scout",
        repository_id="repo",
        repository_root=REPO,
        backend="orca-pi",
    )
    ledger.write_manifest("run-cancel", "repo-scout", manifest)
    ledger.append_event(
        {
            "schemaVersion": 1,
            "eventId": "compiled-cancel",
            "runId": "run-cancel",
            "workerId": "repo-scout",
            "sequence": 1,
            "timestamp": utc_now(),
            "type": "worker.compiled",
            "idempotencyKey": "run-cancel/repo-scout/compiled/1",
            "data": {},
        }
    )
    cancelled = dispatch(
        {"action": "cancel", "runId": "run-cancel", "reason": "stop"},
        REPO,
    )
    assert cancelled["disposition"]["status"] == "cancelled"


def test_unimplemented_integrate_fails_closed():
    with pytest.raises(ValueError, match="unavailable"):
        dispatch({"action": "integrate", "runId": "run-cli", "workerId": "repo-scout"}, REPO)


def test_corrupt_ledger_cli_returns_bounded_orphan_error(tmp_path):
    ledger_root = tmp_path / "delegations"
    RunLedger(ledger_root)
    (ledger_root / "index.json").write_text('{"bad": true}', encoding="utf-8")
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps({"action": "status"}), encoding="utf-8")
    process = subprocess.run(
        [sys.executable, "-m", "forge.orchestrator.cli", "--input-file", str(input_path)],
        cwd=REPO,
        env={**os.environ, "AGENT_FORGE_DELEGATIONS": str(ledger_root)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 2
    assert "Traceback" not in process.stderr
    payload = json.loads(process.stdout)
    assert payload["error"]["code"] == "ledger-corrupt"
    assert payload["error"]["orphaned"] is True
