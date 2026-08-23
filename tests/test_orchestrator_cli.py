"""No-model core bridge tests for the thin Pi extension."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from forge.orchestrator.cli import _load_input, dispatch

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

    collected = dispatch(
        {"action": "collect", "runId": "run-cli", "workerId": "repo-scout"}, REPO
    )
    assert collected["state"] == "pending"


def test_unimplemented_side_effect_actions_fail_closed():
    with pytest.raises(ValueError, match="unavailable"):
        dispatch({"action": "cancel", "runId": "run-cli", "reason": "stop"}, REPO)
    with pytest.raises(ValueError, match="unavailable"):
        dispatch({"action": "integrate", "runId": "run-cli", "workerId": "repo-scout"}, REPO)
