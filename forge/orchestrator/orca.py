"""Orca-first backend adapter using the version-matched JSON CLI contract."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .ledger import LedgerError, RunLedger


class OrcaError(RuntimeError):
    """A typed Orca command or capability failure."""


class OrcaSettlementError(OrcaError):
    """A Delivery or worker report cannot establish a valid settlement."""


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

    def terminal_create(self, command: str, *, worktree: str, title: str) -> dict[str, Any]:
        return self._invoke(
            (
                "terminal",
                "create",
                "--worktree",
                worktree,
                "--title",
                title,
                "--command",
                command,
            ),
            mutating=True,
        ).result

    def terminal_show(self, handle: str) -> dict[str, Any]:
        return self._invoke(("terminal", "show", "--terminal", handle)).result

    def terminal_wait(self, handle: str, *, timeout_ms: int = 30000) -> dict[str, Any]:
        return self._invoke(
            (
                "terminal",
                "wait",
                "--terminal",
                handle,
                "--for",
                "tui-idle",
                "--timeout-ms",
                str(timeout_ms),
            )
        ).result

    def terminal_close(self, handle: str) -> dict[str, Any]:
        return self._invoke(
            ("terminal", "close", "--terminal", handle, "--tab"),
            mutating=True,
        ).result

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

    def worker_done(
        self,
        *,
        task_id: str,
        dispatch_id: str,
        outcome: str,
        subject: str,
        body: str,
        report_path: str,
    ) -> dict[str, Any]:
        return self._invoke(
            (
                "orchestration",
                "send",
                "--type",
                "worker_done",
                "--subject",
                subject,
                "--body",
                body,
                "--task-id",
                task_id,
                "--dispatch-id",
                dispatch_id,
                "--outcome",
                outcome,
                "--report-path",
                report_path,
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

    def check(
        self,
        *,
        run_id: str,
        wait: bool = False,
        acknowledge: str | None = None,
        types: Sequence[str] = (),
        timeout_ms: int = 900000,
    ) -> dict[str, Any]:
        args = ("orchestration", "check", "--run", run_id)
        if wait:
            args += ("--wait", "--timeout-ms", str(timeout_ms))
        if acknowledge:
            args += ("--ack", acknowledge)
        if types:
            args += ("--types", ",".join(types))
        return self._invoke(args).result


@dataclass(frozen=True)
class DeliveryObservation:
    delivery_id: str | None
    messages: tuple[dict[str, Any], ...]
    timed_out: bool = False


@dataclass(frozen=True)
class SettledWorker:
    task_id: str
    dispatch_id: str
    outcome: str
    report_path: str
    result: Any


class OrcaObserver:
    """Consume settlement deliveries before acknowledging them."""

    def __init__(self, client: OrcaClient, *, report_root: str | Path):
        self.client = client
        self.report_root = Path(report_root).resolve()
        self._processed_deliveries: dict[str, SettledWorker] = {}

    def wait(self, run_id: str, *, timeout_ms: int = 900000) -> DeliveryObservation:
        result = self.client.check(
            run_id=run_id,
            wait=True,
            types=("worker_done", "escalation", "question"),
            timeout_ms=timeout_ms,
        )
        messages = result.get("messages", [])
        if not isinstance(messages, list):
            raise OrcaSettlementError("Orca Delivery messages are not an array")
        delivery_id = result.get("deliveryId")
        return DeliveryObservation(
            delivery_id if isinstance(delivery_id, str) else None,
            tuple(message for message in messages if isinstance(message, dict)),
            bool(result.get("timedOut")),
        )

    def process(self, delivery: DeliveryObservation) -> tuple[SettledWorker, ...]:
        if delivery.timed_out:
            return ()
        if delivery.delivery_id and delivery.delivery_id in self._processed_deliveries:
            return (self._processed_deliveries[delivery.delivery_id],)
        done = [message for message in delivery.messages if message.get("type") == "worker_done"]
        if len(done) != 1:
            raise OrcaSettlementError("a Delivery must contain exactly one worker_done message")
        message = done[0]
        payload = message.get("payload", {})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as error:
                raise OrcaSettlementError("worker_done payload is not JSON") from error
        if not isinstance(payload, dict):
            raise OrcaSettlementError("worker_done payload must be an object")
        task_id = payload.get("taskId") or message.get("task_id")
        dispatch_id = payload.get("dispatchId") or message.get("dispatch_id")
        outcome = payload.get("outcome")
        report_path = payload.get("reportPath") or payload.get("report_path")
        if not all(isinstance(item, str) and item for item in (task_id, dispatch_id, outcome, report_path)):
            raise OrcaSettlementError("worker_done must include taskId, dispatchId, outcome, and reportPath")
        report = self._read_report(report_path)
        settled = SettledWorker(task_id, dispatch_id, outcome, report_path, report)
        if delivery.delivery_id:
            self._processed_deliveries[delivery.delivery_id] = settled
        return (settled,)

    def acknowledge(self, run_id: str, delivery: DeliveryObservation) -> None:
        if delivery.delivery_id is None:
            raise OrcaSettlementError("cannot acknowledge a Delivery without deliveryId")
        self.client.check(run_id=run_id, acknowledge=delivery.delivery_id)

    def release(self, dispatch_id: str) -> dict[str, Any]:
        return self.client.worker_release(dispatch_id)

    def _read_report(self, report_path: str) -> Any:
        candidate = (self.report_root / report_path).resolve()
        try:
            candidate.relative_to(self.report_root)
        except ValueError as error:
            raise OrcaSettlementError("worker report path escapes the report root") from error
        if not candidate.is_file():
            raise OrcaSettlementError(f"worker report does not exist: {report_path}")
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise OrcaSettlementError(f"worker report is not valid JSON: {report_path}") from error
        try:
            from .contracts import validate_contract

            return validate_contract("worker-result", raw).to_dict()
        except Exception as error:
            raise OrcaSettlementError(f"worker report failed WorkerResult validation: {error}") from error


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

    def ensure_terminal(
        self,
        run_id: str,
        node_id: str,
        command: str,
        *,
        worktree: str,
        title: str,
        readiness_timeout_ms: int = 30000,
    ) -> str:
        backend_path = self.ledger.root / run_id / "backend.json"
        handle = None
        if backend_path.exists():
            existing = self.ledger.read_backend(run_id)
            handle = existing["identities"].get(f"terminal:{node_id}")
            if handle:
                self.client.terminal_show(handle)
        if handle is None:
            result = self.client.terminal_create(command, worktree=worktree, title=title)
            terminal = result.get("terminal", {})
            handle = result.get("handle") or (terminal.get("handle") if isinstance(terminal, dict) else None)
            if not isinstance(handle, str) or not handle:
                raise OrcaError("terminal create returned no terminal handle")
            self.ledger.update_backend(run_id, identities={f"terminal:{node_id}": handle})
        readiness = self.client.terminal_wait(handle, timeout_ms=readiness_timeout_ms)
        if readiness.get("timedOut") is True or readiness.get("timed_out") is True:
            raise OrcaError(f"terminal {handle!r} did not become ready before the timeout")
        return handle

    def launch(self, run_id: str, node_id: str, task_id: str, terminal: str) -> str:
        backend_path = self.ledger.root / run_id / "backend.json"
        if not backend_path.exists():
            raise OrcaError(f"no persisted Orca backend identities for run {run_id!r}")
        try:
            existing = self.ledger.read_backend(run_id)
        except LedgerError as error:
            raise OrcaError(f"cannot read persisted Orca identities for run {run_id!r}: {error}") from error
        existing_id = existing["identities"].get(f"dispatch:{node_id}")
        if existing_id:
            self.client.worker_show(existing_id)
            return existing_id
        native_run_id = existing["identities"].get("run")
        if not native_run_id:
            raise OrcaError(f"no persisted native Orca Run ID for run {run_id!r}")
        result = self.client.worker_start(task_id, run_id=native_run_id, terminal=terminal)
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
