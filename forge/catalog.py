"""Compile audited YAML catalog sources into one deterministic JSON lockfile.

  python3 -m forge.catalog compile catalog --output catalog/catalog.lock.json
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import re
import sys
from pathlib import Path
from typing import Any

from .orchestrator.canonical import bind_content_identity, canonical_bytes, write_canonical
from .orchestrator.contracts import ContractError, validate_contract

MISSING_DEPENDENCY_ERROR = (
    "catalog compilation requires PyYAML; install it with: "
    "python3 -m pip install -r requirements-catalog.txt"
)
_ZERO_HASH = "sha256:" + "0" * 64
_COMPATIBILITY_RE = re.compile(
    r"^>=(\d+)\.(\d+)\.(\d+) <(\d+)\.(\d+)\.(\d+)$"
)
_RESOURCE_ROOTS = {
    "skill": {"skills"},
    "extension": {"resources"},
    "prompt-template": {"prompts", "policies"},
    "system-fragment": {"prompts", "policies"},
    "mcp-server": {"resources"},
    "role": {"roles"},
    "recipe": {"resources"},
}
_PROFILE_POLICY = {
    "observe": ("shared-readonly", 1200, {"read", "grep", "find", "ls"}),
    "research": (
        "shared-readonly",
        1800,
        {"read", "grep", "find", "ls", "web", "mcp-gateway"},
    ),
    "modify-isolated": (
        "isolated-worktree",
        3600,
        {"read", "grep", "find", "ls", "bash", "edit", "write", "verify"},
    ),
    "review": (
        "isolated-readonly",
        900,
        {"read", "grep", "find", "ls", "verify"},
    ),
}


class MissingCatalogDependency(RuntimeError):
    """The optional YAML authoring dependency is unavailable."""


class CatalogCompileError(Exception):
    """Catalog compilation failed with sorted, durable problem descriptions."""

    def __init__(self, problems: list[str]):
        self.problems = sorted(set(problems))
        super().__init__(
            "catalog compilation failed:\n"
            + "\n".join(f"  - {problem}" for problem in self.problems)
        )


def _yaml_api():
    try:
        import yaml
    except ModuleNotFoundError as error:
        if error.name == "yaml":
            raise MissingCatalogDependency(MISSING_DEPENDENCY_ERROR) from error
        raise

    class ClosedSafeLoader(yaml.SafeLoader):
        def compose_node(self, parent, index):
            if self.check_event(yaml.AliasEvent):
                event = self.peek_event()
                raise yaml.constructor.ConstructorError(
                    None,
                    None,
                    f"YAML aliases are forbidden: *{event.anchor}",
                    event.start_mark,
                )
            return super().compose_node(parent, index)

        def construct_mapping(self, node, deep=False):
            mapping = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                try:
                    duplicate = key in mapping
                except TypeError as error:
                    raise yaml.constructor.ConstructorError(
                        "while constructing a mapping",
                        node.start_mark,
                        "mapping keys must be scalar",
                        key_node.start_mark,
                    ) from error
                if duplicate:
                    raise yaml.constructor.ConstructorError(
                        "while constructing a mapping",
                        node.start_mark,
                        f"duplicate mapping key {key!r}",
                        key_node.start_mark,
                    )
                mapping[key] = self.construct_object(value_node, deep=deep)
            return mapping

    return yaml, ClosedSafeLoader


def _load_yaml(path: Path, yaml, loader, problems: list[str]) -> Any:
    try:
        documents = list(yaml.load_all(path.read_text(encoding="utf-8"), Loader=loader))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        problems.append(f"{path}: invalid YAML: {error}")
        return None
    if len(documents) != 1:
        problems.append(f"{path}: expected exactly one YAML document")
        return None
    return documents[0]


def _unwrap(
    path: Path,
    document: Any,
    field: str,
    expected_type: type,
    problems: list[str],
) -> Any:
    if not isinstance(document, dict):
        problems.append(f"{path}$: must be an object")
        return None
    for key in sorted(set(document) - {"schemaVersion", field}):
        problems.append(f"{path}$.{key}: unknown field")
    if document.get("schemaVersion") != 1:
        problems.append(f"{path}$.schemaVersion: must be 1")
    value = document.get(field)
    if not isinstance(value, expected_type):
        article = "an" if expected_type is list else "an"
        problems.append(f"{path}$.{field}: must be {article} {expected_type.__name__}")
        return None
    return value


def _load_source(root: Path) -> dict[str, Any]:
    yaml, loader = _yaml_api()
    problems: list[str] = []
    capabilities_path = root / "capabilities.yaml"
    models_path = root / "models.yaml"
    for required in (capabilities_path, models_path, root / "resources", root / "recipes"):
        if not required.exists():
            problems.append(f"{required}: required catalog source is missing")
    if problems:
        raise CatalogCompileError(problems)

    capabilities = _unwrap(
        capabilities_path,
        _load_yaml(capabilities_path, yaml, loader, problems),
        "capabilities",
        list,
        problems,
    )
    models = _unwrap(
        models_path,
        _load_yaml(models_path, yaml, loader, problems),
        "models",
        list,
        problems,
    )

    resources = []
    resource_paths = sorted((root / "resources").glob("*.yaml"))
    if not resource_paths:
        problems.append(f"{root / 'resources'}: at least one resource descriptor is required")
    for path in resource_paths:
        value = _unwrap(
            path,
            _load_yaml(path, yaml, loader, problems),
            "resource",
            dict,
            problems,
        )
        if value is not None:
            resources.append(value)

    recipes = []
    recipe_paths = sorted((root / "recipes").glob("*.yaml"))
    if not recipe_paths:
        problems.append(f"{root / 'recipes'}: at least one recipe descriptor is required")
    for path in recipe_paths:
        value = _unwrap(
            path,
            _load_yaml(path, yaml, loader, problems),
            "recipe",
            dict,
            problems,
        )
        if value is not None:
            recipes.append(value)

    if problems:
        raise CatalogCompileError(problems)
    source = {
        "schemaVersion": 1,
        "capabilities": sorted(capabilities, key=lambda item: item.get("id", "") if isinstance(item, dict) else ""),
        "resources": sorted(resources, key=lambda item: item.get("id", "")),
        "recipes": sorted(recipes, key=lambda item: item.get("id", "")),
        "models": sorted(models, key=lambda item: item.get("tier", "") if isinstance(item, dict) else ""),
    }
    try:
        return validate_contract("catalog-source", source).to_dict()
    except ContractError as error:
        raise CatalogCompileError([str(problem) for problem in error.problems]) from error


def _parse_version(version: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    return tuple(map(int, match.groups())) if match else None


def _compatible(expression: str, version: tuple[int, int, int]) -> bool:
    match = _COMPATIBILITY_RE.fullmatch(expression)
    if not match:
        return False
    values = tuple(map(int, match.groups()))
    return values[:3] <= version < values[3:]


def _validate_semantics(root: Path, source: dict[str, Any], pi_version: str) -> None:
    problems: list[str] = []
    version = _parse_version(pi_version)
    if version is None:
        raise CatalogCompileError([f"piVersion: invalid semantic version {pi_version!r}"])

    capabilities = {entry["id"]: entry for entry in source["capabilities"]}
    resources = {entry["id"]: entry for entry in source["resources"]}
    models = {entry["tier"]: entry for entry in source["models"]}
    providers: dict[str, list[str]] = {identifier: [] for identifier in capabilities}

    resolved_root = root.resolve()
    for index, resource in enumerate(source["resources"]):
        path = f"$.resources[{index}]"
        for capability in resource["provides"]:
            if capability not in capabilities:
                problems.append(f"{path}.provides: unknown capability {capability!r}")
            else:
                providers[capability].append(resource["id"])
        for capability in resource["requires"]:
            if capability not in capabilities:
                problems.append(f"{path}.requires: unknown capability {capability!r}")
        for conflict in resource["conflicts"]:
            if conflict not in resources:
                problems.append(f"{path}.conflicts: unknown resource {conflict!r}")
        if not _compatible(resource["piCompatibility"], version):
            problems.append(
                f"{path}.piCompatibility: {resource['id']!r} is incompatible with Pi {pi_version}"
            )

        authored_path = Path(resource["path"])
        if authored_path.is_absolute() or ".." in authored_path.parts:
            problems.append(f"{path}.path: must resolve within the catalog root")
            continue
        if not authored_path.parts or authored_path.parts[0] not in _RESOURCE_ROOTS[resource["kind"]]:
            roots = ", ".join(sorted(_RESOURCE_ROOTS[resource["kind"]]))
            problems.append(
                f"{path}.path: {resource['kind']} resources must be under approved root(s): {roots}"
            )
            continue
        try:
            candidate = (resolved_root / authored_path).resolve()
            candidate.relative_to(resolved_root)
        except (OSError, RuntimeError, ValueError):
            problems.append(f"{path}.path: must resolve within the catalog root")
            continue
        if not candidate.is_file():
            problems.append(f"{path}.path: resource does not exist or is not a file")
            continue
        try:
            content = candidate.read_bytes()
        except OSError as error:
            problems.append(f"{path}.path: cannot read resource: {error}")
            continue
        actual_hash = "sha256:" + hashlib.sha256(content).hexdigest()
        if actual_hash != resource["sha256"]:
            problems.append(
                f"{path}.sha256: hash mismatch for {resource['path']!r}; "
                f"expected {resource['sha256']}, observed {actual_hash}"
            )

    for identifier, capability in sorted(capabilities.items()):
        count = len(providers[identifier])
        if count == 0:
            problems.append(f"$.capabilities: capability {identifier!r} has no provider")
        elif capability["mode"] == "exclusive" and count != 1:
            problems.append(
                f"$.capabilities: exclusive capability {identifier!r} has {count} providers: "
                + ", ".join(sorted(providers[identifier]))
            )

    for index, recipe in enumerate(source["recipes"]):
        path = f"$.recipes[{index}]"
        profile = recipe["permissionProfile"]
        expected_workspace, maximum, allowed_tools = _PROFILE_POLICY[profile]
        if recipe["workspaceMode"] != expected_workspace:
            problems.append(
                f"{path}.workspaceMode: must be {expected_workspace!r} for {profile}"
            )
        if recipe["budget"]["timeoutSeconds"] > maximum:
            problems.append(f"{path}.budget.timeoutSeconds: exceeds {profile} maximum {maximum}")
        denied = sorted(set(recipe["tools"]) - allowed_tools)
        if denied:
            problems.append(f"{path}.tools: permission widening: {', '.join(denied)}")
        if recipe["modelTier"] not in models:
            problems.append(f"{path}.modelTier: unavailable tier {recipe['modelTier']!r}")

        selected = []
        for resource_id in recipe["resourceIds"]:
            resource = resources.get(resource_id)
            if resource is None:
                problems.append(f"{path}.resourceIds: unknown resource {resource_id!r}")
                continue
            selected.append(resource)
            if profile not in resource["permissions"]:
                problems.append(
                    f"{path}.resourceIds: {resource_id!r} does not permit profile {profile!r}"
                )
        selected_ids = {resource["id"] for resource in selected}
        for resource in selected:
            conflicts = selected_ids.intersection(resource["conflicts"])
            if conflicts:
                problems.append(
                    f"{path}.resourceIds: {resource['id']!r} conflicts with "
                    + ", ".join(sorted(conflicts))
                )
        selected_capabilities = {
            capability for resource in selected for capability in resource["provides"]
        }
        for capability in recipe["capabilities"]["required"]:
            if capability not in capabilities:
                problems.append(f"{path}.capabilities.required: unknown capability {capability!r}")
            elif capability not in selected_capabilities:
                problems.append(
                    f"{path}.capabilities.required: no selected provider for {capability!r}"
                )
        for capability in recipe["capabilities"]["optional"]:
            if capability not in capabilities:
                problems.append(f"{path}.capabilities.optional: unknown capability {capability!r}")

    if problems:
        raise CatalogCompileError(problems)


def compile_catalog(
    catalog_root: str | Path,
    output: str | Path,
    *,
    pi_version: str = "0.84.0",
):
    """Compile a closed authored catalog and atomically write its validated lock."""

    root = Path(catalog_root)
    if not root.is_dir():
        raise CatalogCompileError([f"{root}: catalog root is not a directory"])
    source = _load_source(root)
    _validate_semantics(root, source, pi_version)
    source_hash = "sha256:" + hashlib.sha256(canonical_bytes(source)).hexdigest()
    candidate = {
        "schemaVersion": 1,
        "lockId": _ZERO_HASH,
        "sourceHash": source_hash,
        "capabilities": copy.deepcopy(source["capabilities"]),
        "resources": copy.deepcopy(source["resources"]),
        "recipes": copy.deepcopy(source["recipes"]),
        "models": copy.deepcopy(source["models"]),
    }
    lock = bind_content_identity("catalog-lock", candidate)
    write_canonical(output, lock.to_dict())
    return lock


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m forge.catalog")
    subcommands = parser.add_subparsers(dest="verb", required=True)
    compile_parser = subcommands.add_parser("compile", help="compile audited YAML to a JSON lockfile")
    compile_parser.add_argument("catalog")
    compile_parser.add_argument("--output", required=True)
    compile_parser.add_argument("--pi-version", default="0.84.0")
    args = parser.parse_args(argv)
    try:
        lock = compile_catalog(args.catalog, args.output, pi_version=args.pi_version)
    except MissingCatalogDependency as error:
        print(str(error), file=sys.stderr)
        return 2
    except CatalogCompileError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"wrote {args.output} ({lock.to_dict()['lockId']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
