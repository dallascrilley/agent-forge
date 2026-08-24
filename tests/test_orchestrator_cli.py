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
from forge.orchestrator.ledger import RunLedger

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures" / "orchestrator"
VALID = json.loads((FIXTURES / "valid-contracts.json").read_text(encoding="utf-8"))
REQUEST = VALID["worker-request"]


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
    request = copy.deepcopy(REQUEST)
    spawned = dispatch(
        {
            "action": "spawn",
            "request": request,
            "runId": "run-cli",
            "workerId": "repo-scout",
        },
        REPO,
    )
    assert spawned["state"] == "compiled"

    status = dispatch({"action": "status", "runId": "run-cli"}, REPO)
    assert status["projection"]["runId"] == "run-cli"
    assert status["eventCount"] == 2
    events = RunLedger(tmp_path / "delegations").read_events("run-cli").to_dicts()
    assert [event["timestamp"] for event in events] == sorted(
        event["timestamp"] for event in events
    )
    assert events[1]["timestamp"] == "2099-01-01T00:00:00Z"

    collected = dispatch(
        {"action": "collect", "runId": "run-cli", "workerId": "repo-scout"}, REPO
    )
    assert collected["state"] == "pending"
    digest = dispatch({"action": "digest", "maxRuns": 10}, REPO)
    assert digest["runs"][0]["runId"] == "run-cli"
    assert digest["runs"][0]["manifests"][0]["timeoutSeconds"] == 600


def test_cancel_targets_a_persisted_queued_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FORGE_DELEGATIONS", str(tmp_path / "delegations"))
    dispatch(
        {"action": "spawn", "request": REQUEST, "runId": "run-cancel", "workerId": "repo-scout"},
        REPO,
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
