"""Offline vertical-slice helpers for read-only workers."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import ContractError, WorkerManifest, WorkerResult, validate_contract
from .resolver import ResolutionError, resolve_request


class VerticalSliceError(ValueError):
    """The vertical-slice contract or workspace invariant failed."""


@dataclass(frozen=True)
class WorkspaceSnapshot:
    revision: str
    status: str


@dataclass(frozen=True)
class RepoScoutPlan:
    manifest: WorkerManifest
    workspace_before: WorkspaceSnapshot


def snapshot_workspace(root: str | Path) -> WorkspaceSnapshot:
    path = Path(root).resolve()
    try:
        revision = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain=v1", "--untracked-files=all"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise VerticalSliceError(f"cannot snapshot repository workspace: {error}") from error
    if not revision:
        raise VerticalSliceError("repository has no HEAD revision")
    return WorkspaceSnapshot(revision, status)


def assert_workspace_unchanged(before: WorkspaceSnapshot, after: WorkspaceSnapshot) -> None:
    if before != after:
        raise VerticalSliceError(
            "read-only worker changed the workspace: "
            f"revision {before.revision!r}->{after.revision!r}, "
            f"status changed={before.status != after.status}"
        )


def compile_repo_scout(
    request: dict[str, Any],
    catalog_lock: dict[str, Any],
    *,
    run_id: str,
    worker_id: str,
    repository_id: str,
    repository_root: str | Path,
    backend: str = "orca-pi",
) -> RepoScoutPlan:
    if request.get("recipe") != "repo-scout":
        raise VerticalSliceError("repo-scout vertical requires the exact repo-scout recipe")
    if request.get("permissionProfile") != "observe":
        raise VerticalSliceError("repo-scout vertical requires the observe permission profile")
    try:
        manifest = resolve_request(
            request,
            catalog_lock,
            run_id=run_id,
            worker_id=worker_id,
            repository_id=repository_id,
            repository_root=repository_root,
            backend=backend,
        )
    except ResolutionError:
        raise
    if manifest.to_dict()["permissionProfile"] != "observe":
        raise VerticalSliceError("resolver widened repo-scout permission profile")
    return RepoScoutPlan(manifest, snapshot_workspace(repository_root))


def validate_repo_scout_result(
    result: dict[str, Any] | WorkerResult,
    *,
    worker_id: str,
    workspace_before: WorkspaceSnapshot,
    workspace_after: WorkspaceSnapshot,
) -> WorkerResult:
    try:
        validated = validate_contract(
            "worker-result",
            result.to_dict() if isinstance(result, WorkerResult) else result,
        )
    except ContractError as error:
        raise VerticalSliceError(str(error)) from error
    value = validated.to_dict()
    if value["workerId"] != worker_id:
        raise VerticalSliceError("repo-scout result workerId does not match the plan")
    if value["status"] not in {"completed", "partial"}:
        raise VerticalSliceError(f"repo-scout result is not an acceptable read-only outcome: {value['status']}")
    if not value["evidence"]:
        raise VerticalSliceError("repo-scout result must cite repository evidence")
    if value["changes"]:
        raise VerticalSliceError("repo-scout observe result must not report changes")
    assert_workspace_unchanged(workspace_before, workspace_after)
    return validated  # type: ignore[return-value]


def fake_repo_scout_result(worker_id: str, inspected_path: str) -> WorkerResult:
    """Create deterministic offline evidence for contract tests only."""

    value = {
        "schemaVersion": 1,
        "workerId": worker_id,
        "status": "completed",
        "outcome": f"Inspected {inspected_path} without modifying the workspace.",
        "claims": [
            {
                "id": "repo-scout-inspection",
                "statement": f"The repository evidence at {inspected_path} was inspected.",
                "evidenceIds": ["repo-scout-evidence"],
            }
        ],
        "evidence": [
            {
                "id": "repo-scout-evidence",
                "kind": "file",
                "summary": f"Inspected repository path: {inspected_path}",
                "artifactId": "repo-scout-artifact",
            }
        ],
        "changes": [],
        "verification": [],
        "artifacts": [],
        "blockers": [],
        "usage": {},
    }
    return validate_contract("worker-result", value)  # type: ignore[return-value]
