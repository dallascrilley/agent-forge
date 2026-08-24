"""Deterministic capability resolver and WorkerManifest policy engine."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import bind_content_identity, verify_content_identity
from .contracts import (
    BACKENDS,
    TOOLS,
    ContractDocument,
    ContractError,
    WorkerManifest,
    validate_contract,
)

_ZERO_HASH = "sha256:" + "0" * 64
_COST = {"low": 0, "medium": 1, "high": 2}
_PROFILE_POLICY = {
    "observe": (
        "shared-readonly",
        {"read", "grep", "find", "ls", "submit_worker_result"},
    ),
    "research": (
        "shared-readonly",
        {"read", "grep", "find", "ls", "web", "mcp-gateway"},
    ),
    "modify-isolated": (
        "isolated-worktree",
        {"read", "grep", "find", "ls", "bash", "edit", "write", "verify"},
    ),
    "review": (
        "isolated-readonly",
        {"read", "grep", "find", "ls", "verify"},
    ),
}


@dataclass(frozen=True, order=True)
class ResolutionProblem:
    """One deterministic policy rejection."""

    path: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "code": self.code, "message": self.message}


class ResolutionError(Exception):
    """Resolution failed closed with structured, stable-path evidence."""

    def __init__(self, problems: list[ResolutionProblem]):
        self.problems = sorted(set(problems))
        super().__init__(
            "worker request rejected:\n"
            + "\n".join(
                f"  - {problem.path} [{problem.code}]: {problem.message}"
                for problem in self.problems
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "rejected",
            "problems": [problem.to_dict() for problem in self.problems],
        }


def _document(value: dict[str, Any] | ContractDocument) -> dict[str, Any]:
    return value.to_dict() if isinstance(value, ContractDocument) else copy.deepcopy(value)


def _contract_problems(code: str, error: ContractError) -> list[ResolutionProblem]:
    return [
        ResolutionProblem(problem.path, code, problem.message)
        for problem in error.problems
    ]


def _resource_map(lock: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {resource["id"]: resource for resource in lock["resources"]}


def _provider_map(lock: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    providers: dict[str, list[dict[str, Any]]] = {
        capability["id"]: [] for capability in lock["capabilities"]
    }
    for resource in lock["resources"]:
        for capability in resource["provides"]:
            providers.setdefault(capability, []).append(resource)
    for resources in providers.values():
        resources.sort(key=lambda resource: (_COST[resource["startupCost"]], resource["id"]))
    return providers


def _check_resource(
    resource: dict[str, Any],
    *,
    profile: str,
    backend: str,
    path: str,
) -> list[ResolutionProblem]:
    problems = []
    if profile not in resource["permissions"]:
        problems.append(
            ResolutionProblem(
                path,
                "permission-widening",
                f"resource {resource['id']!r} does not permit profile {profile!r}",
            )
        )
    if backend not in resource["backends"]:
        problems.append(
            ResolutionProblem(
                path,
                "backend-incompatible",
                f"resource {resource['id']!r} does not support backend {backend!r}",
            )
        )
    if resource["kind"] == "mcp-server":
        if resource.get("mcpTools") is None:
            problems.append(
                ResolutionProblem(
                    path,
                    "mcp-policy-missing",
                    f"MCP resource {resource['id']!r} has no locked tool allowlist",
                )
            )
        if "mcp-gateway" not in resource["tools"]:
            problems.append(
                ResolutionProblem(
                    path,
                    "mcp-policy-missing",
                    f"MCP resource {resource['id']!r} is not routed through mcp-gateway",
                )
            )
    elif "mcpTools" in resource:
        problems.append(
            ResolutionProblem(
                path,
                "mcp-policy-invalid",
                "only mcp-server resources may declare mcpTools",
            )
        )
    return problems


def _select_recipe_resources(
    recipe: dict[str, Any],
    request: dict[str, Any],
    lock: dict[str, Any],
    backend: str,
) -> tuple[list[dict[str, Any]], list[ResolutionProblem]]:
    problems: list[ResolutionProblem] = []
    resources = _resource_map(lock)
    providers = _provider_map(lock)
    capability_modes = {
        capability["id"]: capability["mode"] for capability in lock["capabilities"]
    }
    profile = request["permissionProfile"]
    expected_workspace, allowed_tools = _PROFILE_POLICY[profile]

    if recipe["permissionProfile"] != profile:
        problems.append(
            ResolutionProblem(
                "$.permissionProfile",
                "permission-widening",
                f"recipe {recipe['id']!r} requires {recipe['permissionProfile']!r}",
            )
        )
    if recipe["workspaceMode"] != expected_workspace:
        problems.append(
            ResolutionProblem(
                "$.workspace",
                "workspace-policy",
                f"recipe {recipe['id']!r} must use {expected_workspace!r}",
            )
        )
    if recipe["modelTier"] != request["modelTier"]:
        problems.append(
            ResolutionProblem(
                "$.modelTier",
                "model-tier-widening",
                f"recipe {recipe['id']!r} is locked to tier {recipe['modelTier']!r}",
            )
        )
    requested_seconds = request["budget"]["timeoutMinutes"] * 60
    if requested_seconds > recipe["budget"]["timeoutSeconds"]:
        problems.append(
            ResolutionProblem(
                "$.budget.timeoutMinutes",
                "budget-widening",
                f"request exceeds recipe maximum of {recipe['budget']['timeoutSeconds']} seconds",
            )
        )

    selected: dict[str, dict[str, Any]] = {}
    origin: dict[str, str] = {}
    for resource_id in recipe["resourceIds"]:
        resource = resources.get(resource_id)
        if resource is None:
            problems.append(
                ResolutionProblem(
                    "$.recipe",
                    "resource-unavailable",
                    f"recipe references missing resource {resource_id!r}",
                )
            )
            continue
        selected[resource_id] = resource
        origin[resource_id] = "$.recipe"

    for index, resource_id in enumerate(request["resourceOverrides"]):
        resource = resources.get(resource_id)
        path = f"$.resourceOverrides[{index}]"
        if resource is None:
            problems.append(
                ResolutionProblem(path, "resource-unavailable", f"unknown resource {resource_id!r}")
            )
            continue
        selected[resource_id] = resource
        origin[resource_id] = path

    def close_requirements(required: set[str]) -> None:
        """Resolve selected-resource requirements to a complete provider closure."""

        while True:
            required.update(
                capability
                for resource in selected.values()
                for capability in resource["requires"]
            )
            provided = {
                capability
                for resource in selected.values()
                for capability in resource["provides"]
            }
            missing = sorted(required - provided)
            if not missing:
                return
            progress = False
            for capability in missing:
                available = providers.get(capability, [])
                if capability_modes.get(capability) == "exclusive" and len(available) != 1:
                    problems.append(
                        ResolutionProblem(
                            "$.capabilities.required",
                            "capability-conflict",
                            f"exclusive capability {capability!r} has {len(available)} providers",
                        )
                    )
                    continue
                compatible = [
                    resource
                    for resource in available
                    if profile in resource["permissions"] and backend in resource["backends"]
                ]
                if not compatible:
                    problems.append(
                        ResolutionProblem(
                            "$.capabilities.required",
                            "capability-unavailable",
                            f"no approved {profile}/{backend} provider for {capability!r}",
                        )
                    )
                    continue
                resource = compatible[0]
                if resource["id"] not in selected:
                    selected[resource["id"]] = resource
                    origin[resource["id"]] = "$.capabilities.required"
                    progress = True
            if not progress:
                return

    # Close over required request capabilities and selected-resource requirements.
    required = set(request["capabilities"]["required"])
    close_requirements(required)

    # Optional capabilities are selected only when an approved provider exists.
    provided = {
        capability
        for resource in selected.values()
        for capability in resource["provides"]
    }
    for capability in request["capabilities"]["optional"]:
        if capability in provided:
            continue
        compatible = [
            resource
            for resource in providers.get(capability, [])
            if profile in resource["permissions"] and backend in resource["backends"]
        ]
        if compatible:
            resource = compatible[0]
            selected[resource["id"]] = resource
            origin[resource["id"]] = "$.capabilities.optional"

    # Optional providers can introduce their own required capabilities.
    close_requirements(required)

    for resource_id, resource in sorted(selected.items()):
        problems.extend(
            _check_resource(
                resource,
                profile=profile,
                backend=backend,
                path=origin[resource_id],
            )
        )

    selected_ids = set(selected)
    seen_conflicts: set[tuple[str, str]] = set()
    for resource in selected.values():
        for conflict in selected_ids.intersection(resource["conflicts"]):
            pair = tuple(sorted((resource["id"], conflict)))
            if pair not in seen_conflicts:
                problems.append(
                    ResolutionProblem(
                        "$.resourceOverrides",
                        "resource-conflict",
                        f"selected resources {pair[0]!r} and {pair[1]!r} conflict",
                    )
                )
                seen_conflicts.add(pair)

    tools = set(recipe["tools"])
    for resource in selected.values():
        tools.update(resource["tools"])
    denied = sorted(tools - allowed_tools)
    if denied:
        problems.append(
            ResolutionProblem(
                "$.permissionProfile",
                "permission-widening",
                "resolved tools exceed the profile: " + ", ".join(denied),
            )
        )

    return sorted(selected.values(), key=lambda resource: resource["id"]), problems


def _context_bindings(
    repository_root: str | Path,
    context_files: tuple[str, ...] | list[str] | None,
) -> tuple[list[dict[str, str]], list[ResolutionProblem]]:
    root = Path(repository_root).resolve()
    if not root.is_dir():
        return [], [
            ResolutionProblem(
                "$.workspace.repository",
                "repository-missing",
                "trusted repository root is not a directory",
            )
        ]
    if context_files is None:
        names = ("AGENTS.md",) if (root / "AGENTS.md").is_file() else ()
    else:
        names = tuple(context_files)
    bindings = []
    problems = []
    seen: set[Path] = set()
    for index, name in enumerate(names):
        path = f"$.contextFiles[{index}]"
        authored = Path(name)
        if authored.is_absolute() or ".." in authored.parts:
            problems.append(
                ResolutionProblem(path, "context-escape", "context path must remain within repository root")
            )
            continue
        try:
            resolved = (root / authored).resolve()
            relative = resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            problems.append(
                ResolutionProblem(path, "context-escape", "context path must remain within repository root")
            )
            continue
        if not resolved.is_file():
            problems.append(
                ResolutionProblem(path, "context-missing", f"context file {name!r} does not exist")
            )
            continue
        if relative in seen:
            problems.append(
                ResolutionProblem(
                    path,
                    "context-duplicate",
                    f"duplicate context file {relative.as_posix()!r}",
                )
            )
            continue
        seen.add(relative)
        try:
            content = resolved.read_bytes()
        except OSError as error:
            problems.append(
                ResolutionProblem(path, "context-unreadable", f"cannot read context file: {error}")
            )
            continue
        bindings.append(
            {
                "path": relative.as_posix(),
                "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
            }
        )
    return sorted(bindings, key=lambda binding: binding["path"]), problems


def _resource_bindings(resources: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {
        "skills": [],
        "extensions": [],
        "promptTemplates": [],
        "systemFragments": [],
        "contextFiles": [],
        "mcpServers": [],
    }
    fields = {
        "skill": "skills",
        "extension": "extensions",
        "prompt-template": "promptTemplates",
        "system-fragment": "systemFragments",
        "role": "systemFragments",
    }
    for resource in resources:
        if resource["kind"] == "mcp-server":
            result["mcpServers"].append(
                {
                    "id": resource["id"],
                    "sha256": resource["sha256"],
                    "tools": list(resource["mcpTools"]),
                }
            )
        elif resource["kind"] in fields:
            result[fields[resource["kind"]]].append(
                {"id": resource["id"], "sha256": resource["sha256"]}
            )
    for bindings in result.values():
        bindings.sort(key=lambda binding: binding.get("id", binding.get("path", "")))
    return result


def _prompt_hashes(resources: list[dict[str, Any]]) -> list[str]:
    order = {"system-fragment": 0, "role": 1, "prompt-template": 2}
    fragments = [resource for resource in resources if resource["kind"] in order]
    fragments.sort(key=lambda resource: (order[resource["kind"]], resource["id"]))
    return [resource["sha256"] for resource in fragments]


def resolve_request(
    request: dict[str, Any] | ContractDocument,
    catalog_lock: dict[str, Any] | ContractDocument,
    *,
    run_id: str,
    worker_id: str,
    repository_id: str,
    repository_root: str | Path,
    backend: str = "orca-pi",
    context_files: tuple[str, ...] | list[str] | None = None,
) -> WorkerManifest:
    """Resolve a constrained request through a trusted lock or reject it."""

    raw_request = _document(request)
    raw_lock = _document(catalog_lock)
    try:
        request_data = validate_contract("worker-request", raw_request).to_dict()
    except ContractError as error:
        raise ResolutionError(_contract_problems("invalid-request", error)) from error
    try:
        lock = validate_contract("catalog-lock", raw_lock).to_dict()
    except ContractError as error:
        raise ResolutionError(_contract_problems("invalid-lock", error)) from error
    if not verify_content_identity("catalog-lock", lock):
        raise ResolutionError(
            [
                ResolutionProblem(
                    "$.lockId",
                    "invalid-lock-identity",
                    "catalog lock content does not match lockId",
                )
            ]
        )
    if backend not in BACKENDS:
        raise ResolutionError(
            [ResolutionProblem("$.backend", "backend-unavailable", f"unknown backend {backend!r}")]
        )

    providers = _provider_map(lock)
    capability_modes = {
        capability["id"]: capability["mode"] for capability in lock["capabilities"]
    }
    capability_problems = []
    for field in ("required", "optional"):
        for capability in request_data["capabilities"][field]:
            path = f"$.capabilities.{field}"
            if capability not in capability_modes:
                if field == "optional":
                    capability_problems.append(
                        ResolutionProblem(
                            path,
                            "capability-unknown",
                            f"catalog does not define optional capability {capability!r}",
                        )
                    )
                else:
                    capability_problems.append(
                        ResolutionProblem(
                            path,
                            "capability-unavailable",
                            f"catalog has no provider for {capability!r}",
                        )
                    )
                continue
            available = providers.get(capability, [])
            if not available and field == "required":
                capability_problems.append(
                    ResolutionProblem(
                        path,
                        "capability-unavailable",
                        f"catalog has no provider for {capability!r}",
                    )
                )
            elif available and capability_modes[capability] == "exclusive" and len(available) != 1:
                capability_problems.append(
                    ResolutionProblem(
                        path,
                        "capability-conflict",
                        f"exclusive capability {capability!r} has {len(available)} providers",
                    )
                )
    if capability_problems:
        raise ResolutionError(capability_problems)

    recipes = {recipe["id"]: recipe for recipe in lock["recipes"]}
    exact_recipe = request_data.get("recipe")
    if exact_recipe is not None:
        if exact_recipe not in recipes:
            raise ResolutionError(
                [
                    ResolutionProblem(
                        "$.recipe",
                        "recipe-unavailable",
                        f"catalog has no recipe {exact_recipe!r}; no fallback is permitted",
                    )
                ]
            )
        candidates = [recipes[exact_recipe]]
    else:
        requested_capabilities = set(request_data["capabilities"]["required"]) | set(
            request_data["capabilities"]["optional"]
        )
        candidates = [
            recipe
            for recipe in lock["recipes"]
            if recipe["permissionProfile"] == request_data["permissionProfile"]
            and recipe["modelTier"] == request_data["modelTier"]
            and set(recipe["capabilities"]["required"]).issubset(requested_capabilities)
        ]

    successful = []
    rejected: list[ResolutionProblem] = []
    for recipe in sorted(candidates, key=lambda item: item["id"]):
        selected, problems = _select_recipe_resources(recipe, request_data, lock, backend)
        if problems:
            rejected.extend(problems)
            continue
        score = (
            len(selected),
            sum(_COST[resource["startupCost"]] for resource in selected),
            recipe["id"],
        )
        successful.append((score, recipe, selected))
    if not successful:
        if rejected:
            raise ResolutionError(rejected)
        raise ResolutionError(
            [
                ResolutionProblem(
                    "$.capabilities.required",
                    "recipe-unavailable",
                    "no compatible recipe can satisfy the request without widening policy",
                )
            ]
        )
    _, recipe, selected = min(successful, key=lambda item: item[0])

    models = {model["tier"]: model for model in lock["models"]}
    model = models.get(request_data["modelTier"])
    if model is None:
        raise ResolutionError(
            [
                ResolutionProblem(
                    "$.modelTier",
                    "model-unavailable",
                    f"lock has no exact model for tier {request_data['modelTier']!r}",
                )
            ]
        )
    contexts, context_problems = _context_bindings(repository_root, context_files)
    if context_problems:
        raise ResolutionError(context_problems)

    resources = _resource_bindings(selected)
    resources["contextFiles"] = contexts
    allowed_tools = set(recipe["tools"])
    for resource in selected:
        allowed_tools.update(resource["tools"])
    tools = [tool for tool in TOOLS if tool in allowed_tools]
    manifest = {
        "schemaVersion": 1,
        "manifestId": _ZERO_HASH,
        "runId": run_id,
        "workerId": worker_id,
        "catalogLockHash": lock["lockId"],
        "backend": backend,
        "task": request_data["task"],
        "acceptanceCriteria": list(request_data["acceptanceCriteria"]),
        "model": copy.deepcopy(model),
        "permissionProfile": request_data["permissionProfile"],
        "workspace": {
            "mode": recipe["workspaceMode"],
            "repositoryId": repository_id,
            "baseRevision": request_data["workspace"]["baseRevision"],
        },
        "resources": resources,
        "tools": {"allow": tools},
        "prompt": {"orderedFragmentHashes": _prompt_hashes(selected)},
        "budget": {
            "timeoutSeconds": request_data["budget"]["timeoutMinutes"] * 60,
            "maxDelegationDepth": 0,
        },
        "returnContract": "worker-result-v1",
    }
    try:
        return bind_content_identity("worker-manifest", manifest)  # type: ignore[return-value]
    except ContractError as error:  # defensive: candidate compiler bugs fail as policy rejection
        raise ResolutionError(_contract_problems("invalid-manifest", error)) from error
