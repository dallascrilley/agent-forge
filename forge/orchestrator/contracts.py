"""Authoritative stdlib validators for companion orchestration contracts.

The dictionaries in ``SCHEMAS`` are the source for the matching editor JSON
Schemas under ``schema/orchestrator``. Validation here intentionally uses no
third-party package; jsonschema is a development-only agreement check.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, ClassVar

SCHEMA_VERSION = 1
PERMISSION_PROFILES = ("observe", "research", "modify-isolated", "review")
MODEL_TIERS = ("fast", "balanced", "deep")
BACKENDS = ("orca-pi", "local-pi")
THINKING_LEVELS = ("off", "low", "medium", "high")
WORKSPACE_MODES = ("shared-readonly", "isolated-worktree", "isolated-readonly")
RESOURCE_KINDS = (
    "skill",
    "extension",
    "prompt-template",
    "system-fragment",
    "mcp-server",
    "role",
    "recipe",
)
TOOLS = (
    "read",
    "grep",
    "find",
    "ls",
    "web",
    "bash",
    "edit",
    "write",
    "verify",
    "mcp-gateway",
    "submit_worker_result",
)
RESULT_STATUSES = ("completed", "partial", "blocked", "failed")
REVIEW_VERDICTS = ("verified", "verified-with-caveats", "refuted")
RUN_EVENT_TYPES = (
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
    "worker.partial",
    "worker.blocked",
    "worker.failed",
    "worker.cancelled",
    "worker.review-pending",
    "worker.verified",
    "worker.verified-with-caveats",
    "worker.refuted",
    "worker.integrated",
    "worker.retained-isolated",
    "worker.rejected",
    "worker.reconciled",
)

_HASH_PATTERN = r"^sha256:[0-9a-f]{64}$"
_ID_PATTERN = r"^[a-z][a-z0-9.-]*$"
_RUN_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
_REVISION_PATTERN = r"^[0-9a-fA-F]{7,64}$"
_RELATIVE_PATH_PATTERN = r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$)).+$"
_TIMESTAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"


def _object(required: tuple[str, ...], properties: dict[str, Any], **extra):
    return {
        "type": "object",
        "required": list(required),
        "additionalProperties": False,
        "properties": properties,
        **extra,
    }


def _array(items: dict[str, Any], *, unique: bool = False, minimum: int | None = None):
    schema: dict[str, Any] = {"type": "array", "items": items}
    if unique:
        schema["uniqueItems"] = True
    if minimum is not None:
        schema["minItems"] = minimum
    return schema


def _string(*, enum=(), pattern: str | None = None, minimum: int | None = None):
    schema: dict[str, Any] = {"type": "string"}
    if enum:
        schema["enum"] = list(enum)
    if pattern:
        schema["pattern"] = pattern
    if minimum is not None:
        schema["minLength"] = minimum
    return schema


def _integer(*, minimum: int | None = None, maximum: int | None = None):
    schema: dict[str, Any] = {"type": "integer"}
    if minimum is not None:
        schema["minimum"] = minimum
    if maximum is not None:
        schema["maximum"] = maximum
    return schema


_VERSION = {"type": "integer", "const": SCHEMA_VERSION}
_ID = _string(pattern=_ID_PATTERN)
_NONEMPTY = _string(minimum=1)
_HASH = _string(pattern=_HASH_PATTERN)
_REVISION = _string(pattern=_REVISION_PATTERN)
_RELATIVE_PATH = _string(pattern=_RELATIVE_PATH_PATTERN)
_STRING_ARRAY = _array(_NONEMPTY, unique=True)
_ID_ARRAY = _array(_ID, unique=True)
_HASH_ARRAY = _array(_HASH)

_CAPABILITIES = _object(
    ("required", "optional"),
    {"required": _ID_ARRAY, "optional": _ID_ARRAY},
)
_BUDGET_REQUEST = _object(
    ("timeoutMinutes", "maxDelegationDepth"),
    {
        "timeoutMinutes": _integer(minimum=1, maximum=60),
        "maxDelegationDepth": {"type": "integer", "const": 0},
    },
)
_MODEL = _object(
    ("tier", "provider", "id", "thinking"),
    {
        "tier": _string(enum=MODEL_TIERS),
        "provider": _NONEMPTY,
        "id": _NONEMPTY,
        "thinking": _string(enum=THINKING_LEVELS),
    },
)
_RESOURCE_BINDING = _object(("id", "sha256"), {"id": _ID, "sha256": _HASH})
_CONTEXT_FILE = _object(
    ("path", "sha256"), {"path": _RELATIVE_PATH, "sha256": _HASH}
)
_MCP_BINDING = _object(
    ("id", "sha256", "tools"),
    {"id": _ID, "sha256": _HASH, "tools": _STRING_ARRAY},
)
_ARTIFACT = _object(
    ("id", "path", "sha256"),
    {"id": _ID, "path": _RELATIVE_PATH, "sha256": _HASH},
)
_CLAIM = _object(
    ("id", "statement", "evidenceIds"),
    {"id": _ID, "statement": _NONEMPTY, "evidenceIds": _ID_ARRAY},
)
_VERIFICATION = _object(
    ("id", "status", "summary", "artifactIds"),
    {
        "id": _ID,
        "status": _string(enum=("passed", "failed", "not-run")),
        "summary": _NONEMPTY,
        "artifactIds": _ID_ARRAY,
    },
)

_CATALOG_LOCK = _object(
    ("schemaVersion", "lockId", "sourceHash", "capabilities", "resources", "recipes", "models"),
    {
        "schemaVersion": _VERSION,
        "lockId": _HASH,
        "sourceHash": _HASH,
        "capabilities": _array(
            _object(
                ("id", "mode"),
                {"id": _ID, "mode": _string(enum=("additive", "exclusive"))},
            ),
            unique=True,
        ),
        "resources": _array(
            _object(
                (
                    "id", "kind", "version", "source", "path", "sha256",
                    "provides", "requires", "conflicts", "backends",
                    "piCompatibility", "trust", "startupCost", "runtimeRisk",
                    "permissions", "credentialEnv", "tools",
                ),
                {
                    "id": _ID,
                    "kind": _string(enum=RESOURCE_KINDS),
                    "version": _NONEMPTY,
                    "source": _NONEMPTY,
                    "path": _RELATIVE_PATH,
                    "sha256": _HASH,
                    "provides": _ID_ARRAY,
                    "requires": _ID_ARRAY,
                    "conflicts": _ID_ARRAY,
                    "backends": _array(_string(enum=BACKENDS), unique=True, minimum=1),
                    "piCompatibility": _NONEMPTY,
                    "trust": {"const": "audited"},
                    "startupCost": _string(enum=("low", "medium", "high")),
                    "runtimeRisk": _string(enum=("none", "filesystem", "network", "code-execution")),
                    "permissions": _array(_string(enum=PERMISSION_PROFILES), unique=True, minimum=1),
                    "credentialEnv": _array(_string(pattern=r"^[A-Z][A-Z0-9_]*$"), unique=True),
                    "tools": _array(_string(enum=TOOLS), unique=True),
                    "mcpTools": _STRING_ARRAY,
                },
            )
        ),
        "recipes": _array(
            _object(
                ("id", "capabilities", "resourceIds", "permissionProfile", "workspaceMode", "modelTier", "tools", "budget"),
                {
                    "id": _ID,
                    "capabilities": _CAPABILITIES,
                    "resourceIds": _ID_ARRAY,
                    "permissionProfile": _string(enum=PERMISSION_PROFILES),
                    "workspaceMode": _string(enum=WORKSPACE_MODES),
                    "modelTier": _string(enum=MODEL_TIERS),
                    "tools": _array(_string(enum=TOOLS), unique=True),
                    "budget": _object(
                        ("timeoutSeconds",),
                        {"timeoutSeconds": _integer(minimum=1, maximum=3600)},
                    ),
                },
            )
        ),
        "models": _array(_MODEL, minimum=1),
    },
)

_CATALOG_SOURCE = copy.deepcopy(_CATALOG_LOCK)
_CATALOG_SOURCE["required"] = [
    field for field in _CATALOG_SOURCE["required"] if field not in {"lockId", "sourceHash"}
]
del _CATALOG_SOURCE["properties"]["lockId"]
del _CATALOG_SOURCE["properties"]["sourceHash"]
# Authored paths are checked after filesystem resolution so absolute, traversal,
# and symlink escapes receive one grounded catalog-root error.
_CATALOG_SOURCE["properties"]["resources"]["items"]["properties"]["path"] = _NONEMPTY


_WORKER_REQUEST = _object(
    (
        "schemaVersion", "task", "acceptanceCriteria", "capabilities",
        "permissionProfile", "workspace", "modelTier", "budget", "dependsOn",
        "resourceOverrides",
    ),
    {
        "schemaVersion": _VERSION,
        "task": _NONEMPTY,
        "acceptanceCriteria": _array(_NONEMPTY, minimum=1),
        "capabilities": _CAPABILITIES,
        "recipe": _ID,
        "permissionProfile": _string(enum=PERMISSION_PROFILES),
        "workspace": _object(
            ("repository", "baseRevision"),
            {"repository": {"const": "current"}, "baseRevision": _REVISION},
        ),
        "modelTier": _string(enum=MODEL_TIERS),
        "budget": _BUDGET_REQUEST,
        "dependsOn": _array(_string(pattern=_RUN_ID_PATTERN), unique=True),
        "resourceOverrides": _ID_ARRAY,
    },
    allOf=[
        {
            "if": {"properties": {"permissionProfile": {"const": profile}}},
            "then": {"properties": {"budget": {"properties": {"timeoutMinutes": {"maximum": maximum}}}}},
        }
        for profile, maximum in (("observe", 20), ("research", 30), ("modify-isolated", 60), ("review", 15))
    ],
)

_MANIFEST_RESOURCES = _object(
    ("skills", "extensions", "promptTemplates", "systemFragments", "contextFiles", "mcpServers"),
    {
        "skills": _array(_RESOURCE_BINDING),
        "extensions": _array(_RESOURCE_BINDING),
        "promptTemplates": _array(_RESOURCE_BINDING),
        "systemFragments": _array(_RESOURCE_BINDING),
        "contextFiles": _array(_CONTEXT_FILE),
        "mcpServers": _array(_MCP_BINDING),
    },
)
_WORKER_MANIFEST = _object(
    (
        "schemaVersion", "manifestId", "runId", "workerId", "catalogLockHash",
        "backend", "task", "acceptanceCriteria", "model", "permissionProfile",
        "workspace", "resources", "tools", "prompt", "budget", "returnContract",
    ),
    {
        "schemaVersion": _VERSION,
        "manifestId": _HASH,
        "runId": _string(pattern=_RUN_ID_PATTERN),
        "workerId": _string(pattern=_RUN_ID_PATTERN),
        "catalogLockHash": _HASH,
        "backend": _string(enum=BACKENDS),
        "task": _NONEMPTY,
        "acceptanceCriteria": _array(_NONEMPTY, minimum=1),
        "model": _MODEL,
        "permissionProfile": _string(enum=PERMISSION_PROFILES),
        "workspace": _object(
            ("mode", "repositoryId", "baseRevision"),
            {
                "mode": _string(enum=WORKSPACE_MODES),
                "repositoryId": _string(pattern=_RUN_ID_PATTERN),
                "baseRevision": _REVISION,
            },
        ),
        "resources": _MANIFEST_RESOURCES,
        "tools": _object(("allow",), {"allow": _array(_string(enum=TOOLS), unique=True)}),
        "prompt": _object(("orderedFragmentHashes",), {"orderedFragmentHashes": _HASH_ARRAY}),
        "budget": _object(
            ("timeoutSeconds", "maxDelegationDepth"),
            {
                "timeoutSeconds": _integer(minimum=1, maximum=3600),
                "maxDelegationDepth": {"type": "integer", "const": 0},
            },
        ),
        "returnContract": {"const": "worker-result-v1"},
    },
    allOf=[],
)

_PROFILE_POLICY = {
    "observe": (
        "shared-readonly",
        1200,
        ("read", "grep", "find", "ls", "submit_worker_result"),
    ),
    "research": ("shared-readonly", 1800, ("read", "grep", "find", "ls", "web", "mcp-gateway")),
    "modify-isolated": ("isolated-worktree", 3600, ("read", "grep", "find", "ls", "bash", "edit", "write", "verify")),
    "review": ("isolated-readonly", 900, ("read", "grep", "find", "ls", "verify")),
}
for _profile, (_workspace, _seconds, _tools) in _PROFILE_POLICY.items():
    _WORKER_MANIFEST["allOf"].append(
        {
            "if": {"properties": {"permissionProfile": {"const": _profile}}},
            "then": {
                "properties": {
                    "workspace": {"properties": {"mode": {"const": _workspace}}},
                    "budget": {"properties": {"timeoutSeconds": {"maximum": _seconds}}},
                    "tools": {"properties": {"allow": {"items": {"enum": list(_tools)}}}},
                }
            },
        }
    )

_RUN_EVENT = _object(
    ("schemaVersion", "eventId", "runId", "workerId", "sequence", "timestamp", "type", "idempotencyKey", "data"),
    {
        "schemaVersion": _VERSION,
        "eventId": _string(pattern=_RUN_ID_PATTERN),
        "runId": _string(pattern=_RUN_ID_PATTERN),
        "workerId": _string(pattern=_RUN_ID_PATTERN),
        "sequence": _integer(minimum=0),
        "timestamp": _string(pattern=_TIMESTAMP_PATTERN),
        "type": _string(enum=RUN_EVENT_TYPES),
        "idempotencyKey": _NONEMPTY,
        "data": _object(
            (),
            {
                "reason": _NONEMPTY,
                "backendId": _NONEMPTY,
                "status": _NONEMPTY,
                "cursor": _integer(minimum=0),
                "artifactId": _ID,
                "message": _NONEMPTY,
            },
        ),
    },
)

_WORKER_RESULT = _object(
    ("schemaVersion", "workerId", "status", "outcome", "claims", "evidence", "changes", "verification", "artifacts", "blockers", "usage"),
    {
        "schemaVersion": _VERSION,
        "workerId": _string(pattern=_RUN_ID_PATTERN),
        "status": _string(enum=RESULT_STATUSES),
        "outcome": _NONEMPTY,
        "claims": _array(_CLAIM),
        "evidence": _array(
            _object(
                ("id", "kind", "summary", "artifactId"),
                {
                    "id": _ID,
                    "kind": _string(enum=("command-output", "file", "url", "artifact", "backend-event")),
                    "summary": _NONEMPTY,
                    "artifactId": _ID,
                },
            )
        ),
        "changes": _array(_object(("path", "summary"), {"path": _RELATIVE_PATH, "summary": _NONEMPTY})),
        "verification": _array(_VERIFICATION),
        "artifacts": _array(_ARTIFACT),
        "blockers": _array(_NONEMPTY),
        "usage": _object(
            (),
            {
                "inputTokens": _integer(minimum=0),
                "outputTokens": _integer(minimum=0),
                "wallSeconds": _integer(minimum=0),
                "backendUnits": _integer(minimum=0),
            },
        ),
    },
)

_REVIEW_PACKET = _object(
    (
        "schemaVersion", "workerId", "originalTask", "acceptanceCriteria",
        "constraints", "workerManifestSummary", "baseRevision", "headRevision",
        "diffArtifact", "claims", "verificationCommands", "verificationOutputs",
        "artifacts",
    ),
    {
        "schemaVersion": _VERSION,
        "workerId": _string(pattern=_RUN_ID_PATTERN),
        "originalTask": _NONEMPTY,
        "acceptanceCriteria": _array(_NONEMPTY, minimum=1),
        "constraints": _array(_NONEMPTY),
        "workerManifestSummary": _object(
            ("manifestId", "permissionProfile", "resourceIds"),
            {
                "manifestId": _HASH,
                "permissionProfile": {"const": "modify-isolated"},
                "resourceIds": _ID_ARRAY,
            },
        ),
        "baseRevision": _REVISION,
        "headRevision": _REVISION,
        "diffArtifact": _object(("path", "sha256"), {"path": _RELATIVE_PATH, "sha256": _HASH}),
        "claims": _array(_CLAIM),
        "verificationCommands": _array(
            _object(
                ("id", "argv"),
                {"id": _ID, "argv": _array(_NONEMPTY, minimum=1)},
            )
        ),
        "verificationOutputs": _array(
            _object(
                ("commandId", "status", "artifactId"),
                {
                    "commandId": _ID,
                    "status": _string(enum=("passed", "failed", "not-run")),
                    "artifactId": _ID,
                },
            )
        ),
        "artifacts": _array(_ARTIFACT),
    },
)

_REVIEW_RESULT = _object(
    ("schemaVersion", "workerId", "verdict", "summary", "claimAssessments", "verification", "caveats"),
    {
        "schemaVersion": _VERSION,
        "workerId": _string(pattern=_RUN_ID_PATTERN),
        "verdict": _string(enum=REVIEW_VERDICTS),
        "summary": _NONEMPTY,
        "claimAssessments": _array(
            _object(
                ("claimId", "verdict", "rationale", "evidenceIds"),
                {
                    "claimId": _ID,
                    "verdict": _string(enum=("verified", "caveated", "refuted")),
                    "rationale": _NONEMPTY,
                    "evidenceIds": _ID_ARRAY,
                },
            )
        ),
        "verification": _array(_VERIFICATION),
        "caveats": _array(_NONEMPTY),
    },
    allOf=[
        {
            "if": {"properties": {"verdict": {"const": "verified-with-caveats"}}},
            "then": {"properties": {"caveats": {"minItems": 1}}},
        },
        {
            "if": {"properties": {"verdict": {"const": "verified"}}},
            "then": {"properties": {"caveats": {"maxItems": 0}}},
        },
    ],
)


def _root_schema(kind: str, title: str, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://github.com/dallascrilley/agent-forge/schema/orchestrator/{kind}.schema.json",
        "title": title,
        "description": "Agent Forge companion orchestration contract v1; separate from Agent Spec v1.",
        **body,
    }


SCHEMAS = {
    "catalog-source": _root_schema("catalog-source", "CatalogSource", _CATALOG_SOURCE),
    "catalog-lock": _root_schema("catalog-lock", "CatalogLock", _CATALOG_LOCK),
    "worker-request": _root_schema("worker-request", "WorkerRequest", _WORKER_REQUEST),
    "worker-manifest": _root_schema("worker-manifest", "WorkerManifest", _WORKER_MANIFEST),
    "run-event": _root_schema("run-event", "RunEvent", _RUN_EVENT),
    "worker-result": _root_schema("worker-result", "WorkerResult", _WORKER_RESULT),
    "review-packet": _root_schema("review-packet", "ReviewPacket", _REVIEW_PACKET),
    "review-result": _root_schema("review-result", "ReviewResult", _REVIEW_RESULT),
}


@dataclass(frozen=True, order=True)
class ContractProblem:
    """One stable-path validation problem."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


class ContractError(Exception):
    """A companion document failed validation with every useful problem."""

    def __init__(self, kind: str, problems: list[ContractProblem]):
        self.kind = kind
        self.problems = sorted(set(problems))
        super().__init__(
            f"{kind} validation failed:\n"
            + "\n".join(f"  - {problem}" for problem in self.problems)
        )


@dataclass(frozen=True)
class ContractDocument:
    """Validated, deeply immutable document detached from caller-owned input."""

    _value: Mapping[str, Any]
    contract_kind: ClassVar[str] = ""

    def to_dict(self) -> dict[str, Any]:
        return _thaw(self._value)


class CatalogSource(ContractDocument):
    contract_kind = "catalog-source"


class CatalogLock(ContractDocument):
    contract_kind = "catalog-lock"


class WorkerRequest(ContractDocument):
    contract_kind = "worker-request"


class WorkerManifest(ContractDocument):
    contract_kind = "worker-manifest"


class RunEvent(ContractDocument):
    contract_kind = "run-event"


class WorkerResult(ContractDocument):
    contract_kind = "worker-result"


class ReviewPacket(ContractDocument):
    contract_kind = "review-packet"


class ReviewResult(ContractDocument):
    contract_kind = "review-result"


CONTRACT_TYPES = {
    contract.contract_kind: contract
    for contract in (
        CatalogSource,
        CatalogLock,
        WorkerRequest,
        WorkerManifest,
        RunEvent,
        WorkerResult,
        ReviewPacket,
        ReviewResult,
    )
}


def _json_type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    return False


def _type_label(expected: str) -> str:
    return {"object": "an object", "array": "an array", "string": "a string", "integer": "an integer", "boolean": "a boolean"}[expected]


def _path_key(path: str, key: str) -> str:
    return f"{path}.{key}"


def _validate_schema(value: Any, schema: dict[str, Any], path: str, problems: list[ContractProblem]) -> None:
    expected = schema.get("type")
    if expected and not _json_type_matches(value, expected):
        problems.append(ContractProblem(path, f"must be {_type_label(expected)}"))
        return

    if "const" in schema and value != schema["const"]:
        problems.append(ContractProblem(path, f"must be {schema['const']!r}"))
        return
    if "enum" in schema and value not in schema["enum"]:
        choices = ", ".join(repr(choice) for choice in schema["enum"])
        problems.append(ContractProblem(path, f"must be one of: {choices}"))
        return

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            problems.append(ContractProblem(path, "must be a non-empty string"))
        pattern = schema.get("pattern")
        if pattern and not re.match(pattern, value):
            problems.append(ContractProblem(path, f"must match {pattern}"))

    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(ContractProblem(path, f"must be >= {schema['minimum']}"))
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(ContractProblem(path, f"must be <= {schema['maximum']}"))

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            problems.append(ContractProblem(path, f"must contain at least {schema['minItems']} item(s)"))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append(ContractProblem(path, f"must contain at most {schema['maxItems']} item(s)"))
        if schema.get("uniqueItems"):
            seen: set[str] = set()
            for index, item in enumerate(value):
                marker = repr(item)
                if marker in seen:
                    problems.append(ContractProblem(f"{path}[{index}]", "must be unique"))
                seen.add(marker)
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                _validate_schema(item, item_schema, f"{path}[{index}]", problems)

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in sorted(schema.get("required", [])):
            if key not in value:
                problems.append(ContractProblem(_path_key(path, key), "required field is missing"))
        if schema.get("additionalProperties") is False:
            for key in sorted(set(value) - set(properties)):
                problems.append(ContractProblem(_path_key(path, key), "unknown field"))
        for key in sorted(set(value) & set(properties)):
            _validate_schema(value[key], properties[key], _path_key(path, key), problems)


def _cross_worker_request(data: dict[str, Any], problems: list[ContractProblem]) -> None:
    profile = data.get("permissionProfile")
    budget = data.get("budget")
    if profile in _PROFILE_POLICY and isinstance(budget, dict):
        timeout = budget.get("timeoutMinutes")
        maximum = _PROFILE_POLICY[profile][1] // 60
        if isinstance(timeout, int) and not isinstance(timeout, bool) and timeout > maximum:
            problems.append(ContractProblem("$.budget.timeoutMinutes", f"must be <= {maximum} for {profile}"))


def _cross_worker_manifest(data: dict[str, Any], problems: list[ContractProblem]) -> None:
    profile = data.get("permissionProfile")
    if profile not in _PROFILE_POLICY:
        return
    workspace, maximum, allowed = _PROFILE_POLICY[profile]
    workspace_data = data.get("workspace")
    if isinstance(workspace_data, dict) and workspace_data.get("mode") != workspace:
        problems.append(ContractProblem("$.workspace.mode", f"must be {workspace!r} for {profile}"))
    budget = data.get("budget")
    timeout = budget.get("timeoutSeconds") if isinstance(budget, dict) else None
    if isinstance(timeout, int) and not isinstance(timeout, bool) and timeout > maximum:
        problems.append(ContractProblem("$.budget.timeoutSeconds", f"must be <= {maximum} for {profile}"))
    tools = data.get("tools")
    allow = tools.get("allow") if isinstance(tools, dict) else None
    if isinstance(allow, list):
        for index, tool in enumerate(allow):
            if isinstance(tool, str) and tool not in allowed:
                problems.append(ContractProblem(f"$.tools.allow[{index}]", f"tool {tool!r} is not allowed by {profile}"))


def _unique_ids(data: dict[str, Any], field: str, problems: list[ContractProblem]) -> None:
    entries = data.get(field)
    if not isinstance(entries, list):
        return
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        identifier = entry.get("id") if isinstance(entry, dict) else None
        if isinstance(identifier, str):
            if identifier in seen:
                problems.append(ContractProblem(f"$.{field}[{index}].id", f"duplicate id {identifier!r}"))
            seen.add(identifier)


def _cross_catalog_lock(data: dict[str, Any], problems: list[ContractProblem]) -> None:
    for field in ("capabilities", "resources", "recipes"):
        _unique_ids(data, field, problems)
    models = data.get("models")
    if isinstance(models, list):
        seen: set[str] = set()
        for index, model in enumerate(models):
            tier = model.get("tier") if isinstance(model, dict) else None
            if isinstance(tier, str):
                if tier in seen:
                    problems.append(ContractProblem(f"$.models[{index}].tier", f"duplicate model tier {tier!r}"))
                seen.add(tier)


def _cross_review_result(data: dict[str, Any], problems: list[ContractProblem]) -> None:
    verdict = data.get("verdict")
    caveats = data.get("caveats")
    if verdict == "verified" and isinstance(caveats, list) and caveats:
        problems.append(ContractProblem("$.caveats", "must be empty for verified"))
    if verdict == "verified-with-caveats" and isinstance(caveats, list) and not caveats:
        problems.append(ContractProblem("$.caveats", "must not be empty for verified-with-caveats"))


_CROSS_VALIDATORS = {
    "catalog-source": _cross_catalog_lock,
    "catalog-lock": _cross_catalog_lock,
    "worker-request": _cross_worker_request,
    "worker-manifest": _cross_worker_manifest,
    "review-result": _cross_review_result,
}


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def validate_contract(kind: str, data: Any) -> ContractDocument:
    """Validate and normalize one companion contract or raise ``ContractError``."""

    if kind not in SCHEMAS:
        raise ValueError(f"unknown orchestration contract: {kind}")
    problems: list[ContractProblem] = []
    _validate_schema(data, SCHEMAS[kind], "$", problems)
    if isinstance(data, dict):
        cross_validator = _CROSS_VALIDATORS.get(kind)
        if cross_validator:
            cross_validator(data, problems)
    if problems:
        raise ContractError(kind, problems)
    return CONTRACT_TYPES[kind](_freeze(copy.deepcopy(data)))
