"""Offline vertical-slice helpers for read-only workers."""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import verify_content_identity
from .contracts import ContractError, WorkerManifest, WorkerResult, validate_contract
from .ledger import RunLedger, utc_now
from .orca import OrcaBackend, OrcaClient, OrcaObserver
from .reducer import reduce_events
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


@dataclass(frozen=True)
class PiLaunchCommand:
    argv: tuple[str, ...]

    @property
    def shell(self) -> str:
        return shlex.join(self.argv)

    def shell_with_environment(self, values: dict[str, str]) -> str:
        for name, value in values.items():
            if not name.startswith("AGENT_FORGE_") or "\0" in value:
                raise VerticalSliceError("invalid worker launch environment")
        assignments = [f"{name}={values[name]}" for name in sorted(values)]
        return shlex.join(("env", *assignments, *self.argv))


@dataclass(frozen=True)
class RepoScoutLaunchReceipt:
    run_id: str
    worker_id: str
    native_run_id: str
    task_id: str
    terminal_handle: str
    dispatch_id: str


def _locked_path(root: Path, relative: str, expected_hash: str, label: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise VerticalSliceError(f"{label} path escapes its trusted root: {relative}") from error
    try:
        digest = "sha256:" + hashlib.sha256(candidate.read_bytes()).hexdigest()
    except OSError as error:
        raise VerticalSliceError(f"cannot read {label} path {relative!r}: {error}") from error
    if digest != expected_hash:
        raise VerticalSliceError(f"{label} path {relative!r} does not match its locked hash")
    return candidate


def compile_pi_launch_command(
    manifest: dict[str, Any] | WorkerManifest,
    catalog_lock: dict[str, Any],
    *,
    catalog_root: str | Path,
    repository_root: str | Path,
) -> PiLaunchCommand:
    """Compile exact, preinstalled Pi argv without ambient resource discovery."""

    try:
        locked = validate_contract("catalog-lock", catalog_lock).to_dict()
        worker = validate_contract(
            "worker-manifest",
            manifest.to_dict() if isinstance(manifest, WorkerManifest) else manifest,
        ).to_dict()
    except ContractError as error:
        raise VerticalSliceError(str(error)) from error
    if not verify_content_identity("catalog-lock", locked):
        raise VerticalSliceError("catalog lock content does not match lockId")
    if not verify_content_identity("worker-manifest", worker):
        raise VerticalSliceError("worker manifest content does not match manifestId")
    if worker["catalogLockHash"] != locked["lockId"]:
        raise VerticalSliceError("worker manifest does not reference the supplied catalog lock")

    catalog_path = Path(catalog_root).resolve()
    repository_path = Path(repository_root).resolve()
    resources = {item["id"]: item for item in locked["resources"]}
    bindings = worker["resources"]
    by_hash: dict[str, Path] = {}
    for field in ("skills", "extensions", "promptTemplates", "systemFragments"):
        for binding in bindings[field]:
            resource = resources.get(binding["id"])
            if resource is None or resource["sha256"] != binding["sha256"]:
                raise VerticalSliceError(f"manifest resource {binding['id']!r} is not in the supplied lock")
            by_hash[binding["sha256"]] = _locked_path(
                catalog_path,
                resource["path"],
                binding["sha256"],
                f"catalog resource {binding['id']!r}",
            )

    argv = [
        "pi",
        "--model",
        f"{worker['model']['provider']}/{worker['model']['id']}",
        "--thinking",
        worker["model"]["thinking"],
        "--no-session",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--tools",
        ",".join(worker["tools"]["allow"]),
    ]
    for binding in bindings["extensions"]:
        argv.extend(("--extension", str(by_hash[binding["sha256"]])))
    for binding in bindings["skills"]:
        argv.extend(("--skill", str(by_hash[binding["sha256"]])))
    for fragment_hash in worker["prompt"]["orderedFragmentHashes"]:
        fragment = by_hash.get(fragment_hash)
        if fragment is None:
            raise VerticalSliceError(f"prompt fragment {fragment_hash!r} has no selected resource")
        argv.extend(("--append-system-prompt", str(fragment)))
    for context in bindings["contextFiles"]:
        argv.extend(
            (
                "--append-system-prompt",
                str(
                    _locked_path(
                        repository_path,
                        context["path"],
                        context["sha256"],
                        "context file",
                    )
                ),
            )
        )
    return PiLaunchCommand(tuple(argv))


def _ensure_worker_event(
    ledger: RunLedger,
    run_id: str,
    worker_id: str,
    event_type: str,
    data: dict[str, Any],
) -> None:
    recovery = ledger.read_events(run_id)
    for event in recovery.events:
        value = event.to_dict()
        if value["workerId"] == worker_id and value["type"] == event_type:
            return
    sequence = recovery.events[-1].to_dict()["sequence"] + 1 if recovery.events else 0
    ledger.append_event(
        {
            "schemaVersion": 1,
            "eventId": uuid.uuid4().hex,
            "runId": run_id,
            "workerId": worker_id,
            "sequence": sequence,
            "timestamp": utc_now(),
            "type": event_type,
            "idempotencyKey": f"{run_id}/{worker_id}/{event_type}/1",
            "data": data,
        }
    )


def _status_digest(status: str) -> str:
    return "sha256:" + hashlib.sha256(status.encode("utf-8")).hexdigest()


def launch_repo_scout(
    plan: RepoScoutPlan,
    catalog_lock: dict[str, Any],
    *,
    catalog_root: str | Path,
    repository_root: str | Path,
    ledger: RunLedger,
    client: OrcaClient,
) -> RepoScoutLaunchReceipt:
    """Launch a compiled repo scout; callers control whether the client is live or trapped."""

    manifest = plan.manifest.to_dict()
    run_id = manifest["runId"]
    worker_id = manifest["workerId"]
    persisted = ledger.read_manifest(run_id, worker_id).to_dict()
    if persisted != manifest:
        raise VerticalSliceError("persisted repo-scout manifest does not match the launch plan")
    workspace_now = snapshot_workspace(repository_root)
    assert_workspace_unchanged(plan.workspace_before, workspace_now)
    command = compile_pi_launch_command(
        plan.manifest,
        catalog_lock,
        catalog_root=catalog_root,
        repository_root=repository_root,
    )
    report_path = (ledger.root / run_id / "results" / f"{worker_id}.json").resolve()
    ledger.update_backend(
        run_id,
        identities={
            f"workspace-revision:{worker_id}": workspace_now.revision,
            f"workspace-status:{worker_id}": _status_digest(workspace_now.status),
            f"report:{worker_id}": str(report_path),
        },
    )
    for event_type, status in (
        ("worker.compiled", "compiled"),
        ("worker.policy-approved", "policy-approved"),
        ("worker.queued", "queued"),
        ("worker.prepare.requested", "preparing"),
    ):
        _ensure_worker_event(ledger, run_id, worker_id, event_type, {"status": status})

    backend = OrcaBackend(client, ledger)
    task_spec = json.dumps(
        {
            "manifestId": manifest["manifestId"],
            "workerId": worker_id,
            "task": manifest["task"],
            "acceptanceCriteria": manifest["acceptanceCriteria"],
            "returnContract": manifest["returnContract"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    task_id = backend.ensure_task(run_id, worker_id, task_spec)
    native_run_id = ledger.read_backend(run_id)["identities"]["run"]
    terminal = backend.ensure_terminal(
        run_id,
        worker_id,
        command.shell_with_environment(
            {
                "AGENT_FORGE_DELEGATIONS": str(ledger.root.resolve()),
                "AGENT_FORGE_RUN_ID": run_id,
                "AGENT_FORGE_WORKER_ID": worker_id,
            }
        ),
        worktree=f"path:{Path(repository_root).resolve()}",
        title=f"Agent Forge {worker_id} {run_id}",
    )
    _ensure_worker_event(
        ledger, run_id, worker_id, "worker.prepared", {"backendId": terminal}
    )
    _ensure_worker_event(
        ledger, run_id, worker_id, "worker.launch.requested", {"backendId": task_id}
    )
    dispatch_id = backend.launch(run_id, worker_id, task_id, terminal)
    _ensure_worker_event(
        ledger,
        run_id,
        worker_id,
        "worker.launched",
        {"backendId": dispatch_id},
    )
    _ensure_worker_event(
        ledger,
        run_id,
        worker_id,
        "worker.running",
        {"backendId": dispatch_id},
    )
    return RepoScoutLaunchReceipt(
        run_id,
        worker_id,
        native_run_id,
        task_id,
        terminal,
        dispatch_id,
    )


def collect_repo_scout(
    run_id: str,
    worker_id: str,
    *,
    repository_root: str | Path,
    ledger: RunLedger,
    observer: OrcaObserver,
    timeout_ms: int = 900000,
) -> WorkerResult | None:
    """Collect, validate, durably settle, and clean up one launched repo scout."""

    backend = ledger.read_backend(run_id)
    identities = backend["identities"]
    required = {
        "run": identities.get("run"),
        "task": identities.get(f"task:{worker_id}"),
        "dispatch": identities.get(f"dispatch:{worker_id}"),
        "terminal": identities.get(f"terminal:{worker_id}"),
        "revision": identities.get(f"workspace-revision:{worker_id}"),
        "status": identities.get(f"workspace-status:{worker_id}"),
    }
    if any(not isinstance(value, str) or not value for value in required.values()):
        raise VerticalSliceError("repo-scout backend identities are incomplete")
    delivery = observer.wait(required["run"], timeout_ms=timeout_ms)
    if delivery.timed_out:
        return None
    if not delivery.delivery_id:
        raise VerticalSliceError("repo-scout settlement has no durable Delivery ID")
    settled = observer.process(delivery)
    if len(settled) != 1:
        raise VerticalSliceError("repo-scout settlement must contain exactly one worker")
    worker = settled[0]
    if worker.task_id != required["task"] or worker.dispatch_id != required["dispatch"]:
        raise VerticalSliceError("repo-scout settlement provenance does not match persisted identities")
    current = snapshot_workspace(repository_root)
    validated = validate_repo_scout_result(
        worker.result,
        worker_id=worker_id,
        workspace_before=WorkspaceSnapshot(required["revision"], required["status"]),
        workspace_after=WorkspaceSnapshot(current.revision, _status_digest(current.status)),
    )
    ledger.write_result(run_id, worker_id, validated)
    projection = reduce_events(ledger.read_events(run_id).events)
    if projection.worker(worker_id).status not in {"completed", "partial"}:
        status = validated.to_dict()["status"]
        ledger.mark_terminal(
            run_id,
            status,
            validated.to_dict()["outcome"],
            worker_id=worker_id,
        )
    ledger.update_backend(run_id, identities={f"delivery:{worker_id}": delivery.delivery_id})
    observer.acknowledge(required["run"], delivery)
    observer.release(required["dispatch"])
    observer.client.terminal_close(required["terminal"])
    return validated


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
    workspace_before = snapshot_workspace(repository_root)
    if manifest.to_dict()["workspace"]["baseRevision"] != workspace_before.revision:
        raise VerticalSliceError("repo-scout request baseRevision does not match repository HEAD")
    return RepoScoutPlan(manifest, workspace_before)


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
