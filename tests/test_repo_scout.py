"""Offline repo-scout planning and read-only result invariants."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from forge.orchestrator.verticals import (
    VerticalSliceError,
    assert_workspace_unchanged,
    compile_repo_scout,
    fake_repo_scout_result,
    snapshot_workspace,
    validate_repo_scout_result,
)

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures" / "orchestrator"
REQUEST = json.loads((FIXTURES / "valid-contracts.json").read_text())["worker-request"]
LOCK = json.loads((REPO / "catalog" / "catalog.lock.json").read_text())


def test_repo_scout_compiles_observe_plan_without_side_effects():
    plan = compile_repo_scout(
        REQUEST,
        LOCK,
        run_id="run-scout",
        worker_id="repo-scout",
        repository_id="repo-fixture",
        repository_root=FIXTURES / "context",
    )
    assert plan.manifest.to_dict()["permissionProfile"] == "observe"
    assert plan.manifest.to_dict()["tools"]["allow"] == ["read", "grep", "find", "ls"]


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
