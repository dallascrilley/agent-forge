"""Capability requests resolve deterministically or return structured policy rejections."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from forge.orchestrator.canonical import bind_content_identity
from forge.orchestrator.resolver import ResolutionError, resolve_request

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures" / "orchestrator"
CONTEXT = FIXTURES / "context"
LOCK = json.loads((REPO / "catalog" / "catalog.lock.json").read_text(encoding="utf-8"))
REQUEST = json.loads(
    (FIXTURES / "valid-contracts.json").read_text(encoding="utf-8")
)["worker-request"]
GOLDEN = json.loads(
    (FIXTURES / "resolved-manifest.golden.json").read_text(encoding="utf-8")
)


def _resolve(request=None, lock=None, **kwargs):
    return resolve_request(
        copy.deepcopy(request or REQUEST),
        copy.deepcopy(lock or LOCK),
        run_id=kwargs.pop("run_id", "run-golden"),
        worker_id=kwargs.pop("worker_id", "repo-scout"),
        repository_id=kwargs.pop("repository_id", "repo-fixture"),
        repository_root=kwargs.pop("repository_root", CONTEXT),
        **kwargs,
    )


def _rejection(request=None, lock=None, **kwargs):
    with pytest.raises(ResolutionError) as exc:
        _resolve(request, lock, **kwargs)
    assert exc.value.to_dict()["status"] == "rejected"
    return exc.value.problems


def test_request_resolves_to_golden_immutable_manifest():
    manifest = _resolve()
    assert manifest.to_dict() == GOLDEN
    with pytest.raises(TypeError):
        manifest._value["backend"] = "local-pi"


def test_separate_processes_emit_byte_identical_manifests():
    code = """
import json, sys
from pathlib import Path
from forge.orchestrator.canonical import canonical_bytes
from forge.orchestrator.resolver import resolve_request
root = Path(sys.argv[1])
fixtures = root / 'tests/fixtures/orchestrator'
request = json.loads((fixtures / 'valid-contracts.json').read_text())['worker-request']
lock = json.loads((root / 'catalog/catalog.lock.json').read_text())
manifest = resolve_request(request, lock, run_id='run-golden', worker_id='repo-scout', repository_id='repo-fixture', repository_root=fixtures / 'context')
sys.stdout.buffer.write(canonical_bytes(manifest.to_dict()))
"""
    outputs = [
        subprocess.check_output(
            [sys.executable, "-c", code, str(REPO)],
            cwd=REPO,
            env=dict(os.environ, PYTHONHASHSEED=seed),
        )
        for seed in ("3", "903")
    ]
    assert outputs[0] == outputs[1]
    assert json.loads(outputs[0])["manifestId"] == GOLDEN["manifestId"]


def test_equivalent_request_and_lock_have_same_id_across_runtime_assignments():
    first = _resolve(run_id="run-one", worker_id="worker-one").to_dict()
    second = _resolve(run_id="run-two", worker_id="worker-two").to_dict()
    assert first["manifestId"] == second["manifestId"]
    assert first["runId"] != second["runId"]


def test_recipe_is_optional_and_smallest_compatible_recipe_is_selected():
    request = copy.deepcopy(REQUEST)
    del request["recipe"]
    lock = copy.deepcopy(LOCK)
    bloated = copy.deepcopy(next(recipe for recipe in lock["recipes"] if recipe["id"] == "repo-scout"))
    bloated["id"] = "bloated-scout"
    extra = copy.deepcopy(next(resource for resource in lock["resources"] if resource["id"] == "policy.evidence"))
    extra["id"] = "policy.extra-evidence"
    lock["resources"].append(extra)
    bloated["resourceIds"].append("policy.extra-evidence")
    lock["recipes"].append(bloated)
    lock = bind_content_identity("catalog-lock", lock).to_dict()
    manifest = _resolve(request, lock).to_dict()
    selected_ids = {
        item["id"]
        for field in ("skills", "extensions", "promptTemplates", "systemFragments")
        for item in manifest["resources"][field]
    }
    assert "policy.extra-evidence" not in selected_ids
    assert "role.repo-scout" in selected_ids


def test_unknown_exact_recipe_never_falls_back():
    request = copy.deepcopy(REQUEST)
    request["recipe"] = "missing-recipe"
    problems = _rejection(request)
    assert [(problem.code, problem.path) for problem in problems] == [
        ("recipe-unavailable", "$.recipe")
    ]


def test_unknown_capability_has_no_silent_implementation_fallback():
    request = copy.deepcopy(REQUEST)
    request["capabilities"]["required"] = ["browser.interact"]
    request.pop("recipe")
    problems = _rejection(request)
    assert any(problem.code == "capability-unavailable" for problem in problems)


def test_delegation_depth_above_zero_is_a_structured_invalid_request():
    request = copy.deepcopy(REQUEST)
    request["budget"]["maxDelegationDepth"] = 1
    problems = _rejection(request)
    assert any(
        problem.code == "invalid-request" and problem.path == "$.budget.maxDelegationDepth"
        for problem in problems
    )


def test_recipe_profile_and_budget_cannot_widen_request_policy():
    request = copy.deepcopy(REQUEST)
    request["recipe"] = "implementation-worker"
    problems = _rejection(request)
    assert any(problem.code == "permission-widening" for problem in problems)

    request = copy.deepcopy(REQUEST)
    request["budget"]["timeoutMinutes"] = 15
    problems = _rejection(request)
    assert any(problem.code == "budget-widening" for problem in problems)


def test_resource_override_cannot_widen_permission():
    request = copy.deepcopy(REQUEST)
    request["resourceOverrides"] = ["role.implementation-worker"]
    problems = _rejection(request)
    assert any(
        problem.code == "permission-widening"
        and problem.path == "$.resourceOverrides[0]"
        for problem in problems
    )


def test_context_selection_hashes_only_trusted_relative_files(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "AGENTS.md").write_text("first", encoding="utf-8")
    first = _resolve(repository_root=root).to_dict()
    (root / "unselected.md").write_text("ambient", encoding="utf-8")
    second = _resolve(repository_root=root).to_dict()
    assert second["manifestId"] == first["manifestId"]
    (root / "AGENTS.md").write_text("changed", encoding="utf-8")
    third = _resolve(repository_root=root).to_dict()
    assert third["manifestId"] != first["manifestId"]

    outside = tmp_path / "outside.md"
    outside.write_text("escape", encoding="utf-8")
    problems = _rejection(repository_root=root, context_files=("../outside.md",))
    assert any(problem.code == "context-escape" for problem in problems)


def test_context_aliases_are_rejected_and_manifest_paths_are_canonical(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "AGENTS.md").write_text("instructions", encoding="utf-8")
    (root / "alias.md").symlink_to(root / "AGENTS.md")

    for aliases in (("AGENTS.md", "./AGENTS.md"), ("AGENTS.md", "alias.md")):
        problems = _rejection(repository_root=root, context_files=aliases)
        assert any(problem.code == "context-duplicate" for problem in problems)

    manifest = _resolve(repository_root=root, context_files=("alias.md",)).to_dict()
    assert manifest["resources"]["contextFiles"] == [
        {
            "path": "AGENTS.md",
            "sha256": "sha256:" + hashlib.sha256(b"instructions").hexdigest(),
        }
    ]


def test_tampered_lockfile_is_rejected_before_resolution():
    lock = copy.deepcopy(LOCK)
    lock["sourceHash"] = "sha256:" + "0" * 64
    problems = _rejection(lock=lock)
    assert [(problem.code, problem.path) for problem in problems] == [
        ("invalid-lock-identity", "$.lockId")
    ]


def test_selected_resource_conflicts_fail_closed():
    lock = copy.deepcopy(LOCK)
    scout = next(resource for resource in lock["resources"] if resource["id"] == "role.repo-scout")
    scout["conflicts"] = ["policy.evidence"]
    lock = bind_content_identity("catalog-lock", lock).to_dict()
    problems = _rejection(lock=lock)
    assert any(problem.code == "resource-conflict" for problem in problems)


def test_duplicate_exclusive_providers_in_a_rebound_lock_still_fail_closed():
    lock = copy.deepcopy(LOCK)
    scout = copy.deepcopy(
        next(resource for resource in lock["resources"] if resource["id"] == "role.repo-scout")
    )
    scout["id"] = "role.duplicate-scout"
    lock["resources"].append(scout)
    lock = bind_content_identity("catalog-lock", lock).to_dict()
    assert any(problem.code == "capability-conflict" for problem in _rejection(lock=lock))


def test_mcp_resources_require_gateway_and_locked_tool_allowlist():
    lock = copy.deepcopy(LOCK)
    server = copy.deepcopy(
        next(resource for resource in lock["resources"] if resource["id"] == "policy.evidence")
    )
    server.update(
        {
            "id": "mcp.fixture",
            "kind": "mcp-server",
            "permissions": ["observe"],
            "tools": ["mcp-gateway"],
        }
    )
    lock["resources"].append(server)
    lock = bind_content_identity("catalog-lock", lock).to_dict()
    request = copy.deepcopy(REQUEST)
    request["resourceOverrides"] = ["mcp.fixture"]
    assert any(problem.code == "mcp-policy-missing" for problem in _rejection(request, lock))


def test_model_tier_and_backend_must_be_available():
    lock = copy.deepcopy(LOCK)
    lock["models"] = [model for model in lock["models"] if model["tier"] != "fast"]
    lock = bind_content_identity("catalog-lock", lock).to_dict()
    assert any(problem.code == "model-unavailable" for problem in _rejection(lock=lock))

    lock = copy.deepcopy(LOCK)
    scout = next(resource for resource in lock["resources"] if resource["id"] == "role.repo-scout")
    scout["backends"] = ["orca-pi"]
    lock = bind_content_identity("catalog-lock", lock).to_dict()
    assert any(
        problem.code == "backend-incompatible"
        for problem in _rejection(lock=lock, backend="local-pi")
    )


def test_manifest_records_exact_lock_model_resources_context_and_zero_depth():
    manifest = _resolve().to_dict()
    assert manifest["catalogLockHash"] == LOCK["lockId"]
    assert manifest["model"] == next(model for model in LOCK["models"] if model["tier"] == "fast")
    assert manifest["resources"]["contextFiles"][0]["path"] == "AGENTS.md"
    assert manifest["budget"]["maxDelegationDepth"] == 0
    assert "delegate" not in manifest["tools"]["allow"]
    assert "literal-secret" not in json.dumps(manifest)
