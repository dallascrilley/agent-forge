"""Companion orchestration contracts remain closed, typed, and schema-aligned."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from forge.orchestrator.contracts import (
    CONTRACT_TYPES,
    SCHEMAS as AUTHORITATIVE_SCHEMAS,
    ContractError,
    validate_contract,
)

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures" / "orchestrator"
SCHEMAS = REPO / "schema" / "orchestrator"
VALID = json.loads((FIXTURES / "valid-contracts.json").read_text(encoding="utf-8"))
INVALID = json.loads(
    (FIXTURES / "invalid-contracts.json").read_text(encoding="utf-8")
)
ENUM_TRAPS = json.loads(
    (FIXTURES / "enum-traps.json").read_text(encoding="utf-8")
)


def _patch(document, changes):
    for dotted_path, value in changes.items():
        target = document
        parts = dotted_path.split(".")
        for part in parts[:-1]:
            target = target[int(part)] if isinstance(target, list) else target[part]
        final = parts[-1]
        if isinstance(target, list):
            target[int(final)] = value
        else:
            target[final] = value


def _schema(kind):
    return json.loads((SCHEMAS / f"{kind}.schema.json").read_text(encoding="utf-8"))


def _json_path(dotted_path):
    result = "$"
    for part in dotted_path.split("."):
        result += f"[{part}]" if part.isdigit() else f".{part}"
    return result


@pytest.mark.parametrize("kind", sorted(VALID))
def test_valid_fixtures_return_named_normalized_contracts(kind):
    source = copy.deepcopy(VALID[kind])
    contract = validate_contract(kind, source)

    assert isinstance(contract, CONTRACT_TYPES[kind])
    assert contract.to_dict() == source
    source["schemaVersion"] = 999
    assert contract.to_dict()["schemaVersion"] == 1
    with pytest.raises(TypeError):
        contract._value["schemaVersion"] = 2


@pytest.mark.parametrize("case", INVALID, ids=lambda case: case["name"])
def test_invalid_fixtures_collect_stable_json_paths(case):
    document = copy.deepcopy(VALID[case["base"]])
    _patch(document, case["patch"])

    with pytest.raises(ContractError) as exc:
        validate_contract(case["kind"], document)

    paths = [problem.path for problem in exc.value.problems]
    for expected in case["paths"]:
        assert expected in paths
    assert paths == sorted(paths)


@pytest.mark.parametrize("kind,path", ENUM_TRAPS)
def test_every_closed_enum_or_const_has_an_invalid_fixture(kind, path):
    jsonschema = pytest.importorskip("jsonschema")
    document = copy.deepcopy(VALID[kind])
    target = document
    for part in path.split("."):
        target = target[int(part)] if isinstance(target, list) else target[part]
    invalid = 99 if isinstance(target, int) else "not-a-contract-value"
    _patch(document, {path: invalid})

    with pytest.raises(ContractError) as exc:
        validate_contract(kind, document)
    assert _json_path(path) in [problem.path for problem in exc.value.problems]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, _schema(kind))


@pytest.mark.parametrize("kind", sorted(VALID))
def test_schema_version_is_closed_for_every_contract(kind):
    document = copy.deepcopy(VALID[kind])
    document["schemaVersion"] = 2
    with pytest.raises(ContractError) as exc:
        validate_contract(kind, document)
    assert "$.schemaVersion" in [problem.path for problem in exc.value.problems]


def test_worker_request_recipe_is_an_optional_approved_override():
    jsonschema = pytest.importorskip("jsonschema")
    document = copy.deepcopy(VALID["worker-request"])
    del document["recipe"]
    validate_contract("worker-request", document)
    jsonschema.validate(document, _schema("worker-request"))


def test_non_object_collects_one_root_problem():
    with pytest.raises(ContractError) as exc:
        validate_contract("worker-request", [])
    assert [(p.path, p.message) for p in exc.value.problems] == [
        ("$", "must be an object")
    ]


def test_unknown_contract_kind_fails_closed():
    with pytest.raises(ValueError, match="unknown orchestration contract"):
        validate_contract("agent-spec", {})


@pytest.mark.parametrize("kind", sorted(VALID))
def test_shipped_json_schemas_match_authoritative_stdlib_contracts(kind):
    assert _schema(kind) == AUTHORITATIVE_SCHEMAS[kind]


@pytest.mark.parametrize("kind", sorted(VALID))
def test_valid_fixtures_match_shipped_json_schemas(kind):
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(VALID[kind], _schema(kind))


@pytest.mark.parametrize("case", INVALID, ids=lambda case: case["name"])
def test_invalid_fixtures_are_rejected_by_shipped_json_schemas(case):
    jsonschema = pytest.importorskip("jsonschema")
    document = copy.deepcopy(VALID[case["base"]])
    _patch(document, case["patch"])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, _schema(case["kind"]))


@pytest.mark.parametrize(
    "profile,maximum",
    [("observe", 20), ("research", 30), ("modify-isolated", 60), ("review", 15)],
)
def test_request_timeout_cross_field_invariant(profile, maximum):
    document = copy.deepcopy(VALID["worker-request"])
    document["permissionProfile"] = profile
    document["budget"]["timeoutMinutes"] = maximum + 1
    with pytest.raises(ContractError) as exc:
        validate_contract("worker-request", document)
    assert "$.budget.timeoutMinutes" in [p.path for p in exc.value.problems]


@pytest.mark.parametrize(
    "profile,workspace,maximum,denied_tool",
    [
        ("observe", "shared-readonly", 1200, "write"),
        ("research", "shared-readonly", 1800, "write"),
        ("modify-isolated", "isolated-worktree", 3600, "web"),
        ("review", "isolated-readonly", 900, "write"),
    ],
)
def test_manifest_profile_workspace_budget_and_tool_invariants(
    profile, workspace, maximum, denied_tool
):
    document = copy.deepcopy(VALID["worker-manifest"])
    document["permissionProfile"] = profile
    document["workspace"]["mode"] = "isolated-readonly" if workspace != "isolated-readonly" else "shared-readonly"
    document["budget"]["timeoutSeconds"] = maximum + 1
    document["tools"]["allow"] = [denied_tool]
    with pytest.raises(ContractError) as exc:
        validate_contract("worker-manifest", document)
    paths = [p.path for p in exc.value.problems]
    assert "$.workspace.mode" in paths
    assert "$.budget.timeoutSeconds" in paths
    assert "$.tools.allow[0]" in paths


@pytest.mark.parametrize(
    "verdict,caveats",
    [("verified", ["unexpected"]), ("verified-with-caveats", [])],
)
def test_review_verdict_caveat_invariant(verdict, caveats):
    document = copy.deepcopy(VALID["review-result"])
    document["verdict"] = verdict
    document["caveats"] = caveats
    with pytest.raises(ContractError) as exc:
        validate_contract("review-result", document)
    assert "$.caveats" in [p.path for p in exc.value.problems]


def test_catalog_ids_and_model_tiers_are_unique():
    document = copy.deepcopy(VALID["catalog-lock"])
    document["resources"].append(copy.deepcopy(document["resources"][0]))
    document["models"].append(copy.deepcopy(document["models"][0]))
    with pytest.raises(ContractError) as exc:
        validate_contract("catalog-lock", document)
    paths = [p.path for p in exc.value.problems]
    assert "$.resources[1].id" in paths
    assert "$.models[1].tier" in paths


def test_agent_spec_v1_schema_and_validator_are_not_replaced():
    assert (REPO / "schema" / "agent-spec.schema.json").is_file()
    assert "spec_version" in json.loads(
        (REPO / "schema" / "agent-spec.schema.json").read_text(encoding="utf-8")
    )["properties"]
