"""Orca-first backend adapter using the version-matched JSON CLI contract."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from .ledger import RunLedger


class OrcaError(RuntimeError):
    """A typed Orca command or capability failure."""


class OrcaUnknownEffect(OrcaError):
    """A mutating Orca request may have committed an unknown effect."""

    def __init__(self, message: str, *, retry_request: str | None = None):
        super().__init__(message)
        self.retry_request = retry_request


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str = ""


@dataclass(frozen=True)
class OrcaReceipt:
    command: tuple[str, ...]
    payload: dict[str, Any]

    @property
    def result(self) -> dict[str, Any]:
        result = self.payload.get("result")
        if not isinstance(result, dict):
            raise OrcaError("Orca JSON response has no object result")
        return result


Runner = Callable[[Sequence[str]], CommandResult]
_RETRY_REQUEST_RE = re.compile(r"(?:--retry-request|retry_request)[= ]([A-Za-z0-9._:-]+)")


def _run_local(argv: Sequence[str]) -> CommandResult:
    process = subprocess.run(argv, capture_output=True, text=True, check=False)
    return CommandResult(process.returncode, process.stdout, process.stderr)


class OrcaClient:
    """Safe JSON CLI client; mutating calls never retry implicitly."""

    def __init__(self, *, executable: str = "orca", runner: Runner | None = None):
        self.executable = executable
        self._runner = runner or _run_local

    def _invoke(self, args: Sequence[str], *, mutating: bool = False) -> OrcaReceipt:
        command = (self.executable, *args, "--json")
        response = self._runner(command)
        payload: dict[str, Any] | None = None
        for line in reversed(response.stdout.splitlines()):
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                payload = parsed
                break
        if response.returncode != 0 or not payload or payload.get("ok") is False:
            text = (response.stderr or response.stdout).strip()[-2000:]
            retry_request = None
            if payload:
                error = payload.get("error")
                if isinstance(error, dict):
                    retry_request = error.get("retryRequest") or error.get("retry_request")
                    text = str(error.get("message") or error)
            retry_request = retry_request or (_RETRY_REQUEST_RE.search(text).group(1) if _RETRY_REQUEST_RE.search(text) else None)
            if mutating and retry_request:
                raise OrcaUnknownEffect(text or "Orca mutation outcome is unknown", retry_request=retry_request)
            raise OrcaError(text or "Orca command failed")
        return OrcaReceipt(tuple(command), payload)

    def preflight(self) -> dict[str, Any]:
        receipt = self._invoke(("status",))
        capabilities = receipt.result.get("runtime", {}).get("capabilities", [])
        if "orchestration.contract.v1" not in capabilities:
            raise OrcaError("Orca runtime lacks orchestration.contract.v1")
        return receipt.result

    def run_create(self, objective: str) -> dict[str, Any]:
        return self._invoke(("orchestration", "run-create", "--objective", objective), mutating=True).result["run"]

    def task_create(self, spec: str, *, run_id: str, dependencies: Sequence[str] = ()) -> dict[str, Any]:
        args = (
            "orchestration",
            "task-create",
            "--run",
            run_id,
            "--spec",
            spec,
        )
        if dependencies:
            args += ("--deps", json.dumps(list(dependencies), separators=(",", ":")))
        return self._invoke(args, mutating=True).result["task"]

    def worker_start(self, task_id: str, *, run_id: str, terminal: str) -> dict[str, Any]:
        return self._invoke(
            (
                "orchestration",
                "worker-start",
                "--task",
                task_id,
                "--run",
                run_id,
                "--terminal",
                terminal,
                "--worktree",
                "current",
            ),
            mutating=True,
        ).result

    def worker_show(self, dispatch_id: str) -> dict[str, Any]:
        return self._invoke(("orchestration", "worker-show", "--dispatch", dispatch_id)).result

    def worker_read(self, dispatch_id: str, *, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        args = ("orchestration", "worker-read", "--dispatch", dispatch_id, "--limit", str(limit))
        if cursor:
            args += ("--cursor", cursor)
        return self._invoke(args).result

    def worker_release(self, dispatch_id: str) -> dict[str, Any]:
        return self._invoke(
            ("orchestration", "worker-release", "--dispatch", dispatch_id), mutating=True
        ).result


@dataclass(frozen=True)
class OrcaBinding:
    run_id: str
    task_ids: dict[str, str]
    dispatch_ids: dict[str, str]


class OrcaBackend:
    """Project Agent Forge identities into one native Orca Run and its attempts."""

    def __init__(self, client: OrcaClient, ledger: RunLedger):
        self.client = client
        self.ledger = ledger

    def ensure_run(self, run_id: str, objective: str) -> str:
        backend_path = self.ledger.root / run_id / "backend.json"
        if backend_path.exists():
            existing = self.ledger.read_backend(run_id)
            existing_id = existing["identities"].get("run")
            if existing_id:
                return existing_id
        self.client.preflight()
        run = self.client.run_create(objective)
        orca_id = run.get("id")
        if not isinstance(orca_id, str) or not orca_id:
            raise OrcaError("run-create returned no run id")
        self.ledger.update_backend(run_id, identities={"run": orca_id})
        return orca_id

    def ensure_task(self, run_id: str, node_id: str, spec: str, dependencies: Sequence[str] = ()) -> str:
        backend_path = self.ledger.root / run_id / "backend.json"
        if backend_path.exists():
            existing = self.ledger.read_backend(run_id)
            existing_id = existing["identities"].get(f"task:{node_id}")
            if existing_id:
                return existing_id
        run_id_orca = self.ensure_run(run_id, "Agent Forge delegated run")
        task = self.client.task_create(spec, run_id=run_id_orca, dependencies=dependencies)
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise OrcaError("task-create returned no task id")
        self.ledger.update_backend(run_id, identities={f"task:{node_id}": task_id})
        return task_id

    def launch(self, run_id: str, node_id: str, task_id: str, terminal: str) -> str:
        backend_path = self.ledger.root / run_id / "backend.json"
        if backend_path.exists():
            existing = self.ledger.read_backend(run_id)
            existing_id = existing["identities"].get(f"dispatch:{node_id}")
            if existing_id:
                self.client.worker_show(existing_id)
                return existing_id
        result = self.client.worker_start(task_id, run_id=run_id, terminal=terminal)
        dispatch = result.get("dispatchId") or result.get("dispatch", {}).get("id")
        if not isinstance(dispatch, str) or not dispatch:
            raise OrcaError("worker-start returned no dispatch id")
        self.ledger.update_backend(run_id, identities={f"dispatch:{node_id}": dispatch})
        return dispatch

    def reconcile(self, run_id: str, node_id: str) -> dict[str, Any]:
        backend = self.ledger.read_backend(run_id)
        dispatch_id = backend["identities"].get(f"dispatch:{node_id}")
        if not dispatch_id:
            raise OrcaError(f"no persisted dispatch for node {node_id!r}")
        return self.client.worker_show(dispatch_id)
