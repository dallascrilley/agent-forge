"""Crash-tolerant durable storage for orchestration runs.

The ledger is deliberately model- and backend-neutral. It owns atomic JSON
snapshots, an append-only event journal, and the small active/recent index;
scheduling and state-transition policy live in later Phase 2 components.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .canonical import atomic_write, canonical_bytes, verify_content_identity
from .contracts import BACKENDS, ContractDocument, ContractError, RunEvent, validate_contract

try:  # pragma: no cover - exercised on the supported POSIX runtimes
    import fcntl
except ImportError:  # pragma: no cover - keeps the module importable elsewhere
    fcntl = None


_SCHEMA_VERSION = 1
_REDACTED = "[REDACTED]"
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_SECRET_KEY_RE = re.compile(
    r"(?:api[_-]?key|access[_-]?key|secret|token|password|passwd|credential|auth)",
    re.IGNORECASE,
)
_TERMINAL_STATUSES = {
    "completed",
    "partial",
    "blocked",
    "failed",
    "cancelled",
    "verified",
    "verified-with-caveats",
    "refuted",
    "integrated",
    "retained-isolated",
    "rejected",
}
_TERMINAL_EVENT_TYPES = {
    "completed": "worker.completed",
    "partial": "worker.partial",
    "blocked": "worker.blocked",
    "failed": "worker.failed",
    "cancelled": "worker.cancelled",
    "verified": "worker.verified",
    "verified-with-caveats": "worker.verified-with-caveats",
    "refuted": "worker.refuted",
    "integrated": "worker.integrated",
    "retained-isolated": "worker.retained-isolated",
    "rejected": "worker.rejected",
}
_EMPTY_INDEX = {
    "schemaVersion": _SCHEMA_VERSION,
    "active": [],
    "recent": [],
}
_CANCELLATION_STATUSES = {
    "requested",
    "stopped",
    "abandoned",
    "already-stopped",
    "outcome-unknown",
    "operation-failed",
}
_CANCELLATION_TRANSITIONS = {
    "requested": _CANCELLATION_STATUSES - {"requested"},
    "outcome-unknown": {"stopped", "abandoned", "operation-failed"},
    "operation-failed": {"stopped", "abandoned", "outcome-unknown"},
    "stopped": set(),
    "abandoned": set(),
    "already-stopped": set(),
}


class LedgerError(RuntimeError):
    """Base class for durable-ledger failures."""


class LedgerValidationError(LedgerError):
    """A document failed its contract or internal ledger schema."""


class LedgerConflictError(LedgerError):
    """An idempotent write was attempted with different content."""


class LedgerCorruptionError(LedgerError):
    """A non-tail document or event-journal corruption was found."""

    def __init__(self, message: str, *, path: Path, tail_path: Path | None = None):
        super().__init__(message)
        self.path = path
        self.tail_path = tail_path


@dataclass(frozen=True)
class EventRecovery:
    """Valid events plus any preserved incomplete/corrupt final record."""

    events: tuple[RunEvent, ...]
    incomplete_tail: bytes | None = None
    corrupt_path: Path | None = None

    @property
    def truncated(self) -> bool:
        return self.incomplete_tail is not None

    def to_dicts(self) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self.events]


def utc_now() -> str:
    """Return the shared RFC3339 UTC timestamp used by durable records."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _check_run_id(value: str, label: str = "run id") -> str:
    if not isinstance(value, str) or not _RUN_ID_RE.fullmatch(value):
        raise LedgerValidationError(f"{label} must match {_RUN_ID_RE.pattern}")
    return value


def _check_timestamp(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _TIMESTAMP_RE.fullmatch(value):
        raise LedgerValidationError(f"{path} must be an RFC3339 UTC timestamp")
    return value


def _redact(value: Any, secret_values: tuple[str, ...], *, key: str = "") -> Any:
    """Redact configured values and values under credential-shaped keys."""

    if isinstance(value, dict):
        result = {}
        for name, item in value.items():
            if isinstance(name, str) and _SECRET_KEY_RE.search(name):
                result[name] = _REDACTED if isinstance(item, str) else _redact(item, secret_values)
            else:
                result[name] = _redact(item, secret_values, key=name if isinstance(name, str) else "")
        return result
    if isinstance(value, list):
        return [_redact(item, secret_values, key=key) for item in value]
    if isinstance(value, str):
        result = value
        for secret in secret_values:
            if secret:
                result = result.replace(secret, _REDACTED)
        return result
    return value


def _json_document(value: Any, path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        canonical_bytes(parsed)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise LedgerCorruptionError(f"invalid JSON document: {error}", path=path) from error
    if not isinstance(parsed, dict):
        raise LedgerCorruptionError("JSON document must be an object", path=path)
    return parsed


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, canonical_bytes(document))


def _fsync_directory(path: Path) -> None:
    """Persist a newly created journal entry in its parent directory."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    """Serialize read/modify/write operations across processes when possible."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _validate_index(document: dict[str, Any]) -> dict[str, Any]:
    if set(document) != {"schemaVersion", "active", "recent"}:
        raise LedgerValidationError("index has unknown or missing fields")
    if document["schemaVersion"] != _SCHEMA_VERSION:
        raise LedgerValidationError("index schemaVersion must be 1")
    seen: set[str] = set()
    normalized = {"schemaVersion": 1, "active": [], "recent": []}
    for field in ("active", "recent"):
        entries = document[field]
        if not isinstance(entries, list):
            raise LedgerValidationError(f"index.{field} must be an array")
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or set(entry) != {"runId", "status", "updatedAt"}:
                raise LedgerValidationError(f"index.{field}[{index}] has invalid fields")
            run_id = _check_run_id(entry["runId"], f"index.{field}[{index}].runId")
            status = entry["status"]
            if not isinstance(status, str) or not status:
                raise LedgerValidationError(f"index.{field}[{index}].status must be non-empty")
            updated_at = _check_timestamp(entry["updatedAt"], f"index.{field}[{index}].updatedAt")
            if run_id in seen:
                raise LedgerValidationError(f"run {run_id!r} appears more than once in index")
            seen.add(run_id)
            normalized[field].append(
                {"runId": run_id, "status": status, "updatedAt": updated_at}
            )
    return normalized


def _validate_map(value: Any, *, integer_values: bool, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LedgerValidationError(f"{path} must be an object")
    result = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise LedgerValidationError(f"{path} keys must be non-empty strings")
        if integer_values:
            if not isinstance(item, int) or isinstance(item, bool) or item < 0:
                raise LedgerValidationError(f"{path}.{key} must be a non-negative integer")
        elif not isinstance(item, str) or not item:
            raise LedgerValidationError(f"{path}.{key} must be a non-empty string")
        result[key] = item
    return result


def _validate_backend(document: dict[str, Any]) -> dict[str, Any]:
    required = {"schemaVersion", "runId", "backend", "identities", "cursors", "idempotencyKeys"}
    if set(document) != required:
        raise LedgerValidationError("backend document has unknown or missing fields")
    if document["schemaVersion"] != _SCHEMA_VERSION:
        raise LedgerValidationError("backend schemaVersion must be 1")
    run_id = _check_run_id(document["runId"], "backend.runId")
    if document["backend"] not in BACKENDS:
        raise LedgerValidationError(f"backend.backend must be one of {BACKENDS!r}")
    return {
        "schemaVersion": 1,
        "runId": run_id,
        "backend": document["backend"],
        "identities": _validate_map(document["identities"], integer_values=False, path="backend.identities"),
        "cursors": _validate_map(document["cursors"], integer_values=True, path="backend.cursors"),
        "idempotencyKeys": _validate_map(
            document["idempotencyKeys"], integer_values=False, path="backend.idempotencyKeys"
        ),
    }


def _validate_cancellation(document: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schemaVersion",
        "runId",
        "workerId",
        "dispatchId",
        "reason",
        "status",
        "requestedAt",
        "updatedAt",
        "evidenceSource",
        "evidenceCursor",
        "evidenceDigest",
        "backendState",
        "lastError",
    }
    if set(document) != required:
        raise LedgerValidationError("cancellation document has unknown or missing fields")
    if document["schemaVersion"] != _SCHEMA_VERSION:
        raise LedgerValidationError("cancellation schemaVersion must be 1")
    run_id = _check_run_id(document["runId"], "cancellation.runId")
    worker_id = _check_run_id(document["workerId"], "cancellation.workerId")
    dispatch_id = _check_run_id(document["dispatchId"], "cancellation.dispatchId")
    reason = document["reason"]
    if not isinstance(reason, str) or not reason:
        raise LedgerValidationError("cancellation.reason must be non-empty")
    status = document["status"]
    if status not in _CANCELLATION_STATUSES:
        raise LedgerValidationError(
            f"cancellation.status must be one of {sorted(_CANCELLATION_STATUSES)!r}"
        )
    strings = {}
    for field in ("evidenceSource", "evidenceCursor", "evidenceDigest", "backendState", "lastError"):
        value = document[field]
        if not isinstance(value, str):
            raise LedgerValidationError(f"cancellation.{field} must be a string")
        strings[field] = value
    return {
        "schemaVersion": 1,
        "runId": run_id,
        "workerId": worker_id,
        "dispatchId": dispatch_id,
        "reason": reason,
        "status": status,
        "requestedAt": _check_timestamp(document["requestedAt"], "cancellation.requestedAt"),
        "updatedAt": _check_timestamp(document["updatedAt"], "cancellation.updatedAt"),
        **strings,
    }


def _validate_disposition(document: dict[str, Any]) -> dict[str, Any]:
    required = {"schemaVersion", "runId", "status", "reason", "updatedAt"}
    if set(document) != required:
        raise LedgerValidationError("disposition document has unknown or missing fields")
    if document["schemaVersion"] != _SCHEMA_VERSION:
        raise LedgerValidationError("disposition schemaVersion must be 1")
    run_id = _check_run_id(document["runId"], "disposition.runId")
    if not isinstance(document["status"], str) or not document["status"]:
        raise LedgerValidationError("disposition.status must be non-empty")
    if not isinstance(document["reason"], str) or not document["reason"]:
        raise LedgerValidationError("disposition.reason must be non-empty")
    return {
        "schemaVersion": 1,
        "runId": run_id,
        "status": document["status"],
        "reason": document["reason"],
        "updatedAt": _check_timestamp(document["updatedAt"], "disposition.updatedAt"),
    }


class RunLedger:
    """Persist one run namespace and the process-safe active-run projection."""

    def __init__(self, root: str | Path, *, secret_values: tuple[str, ...] | list[str] = ()):
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.secret_values = tuple(value for value in secret_values if value)
        self.index_path = self.root / "index.json"
        self._index_lock = self.root / ".index.lock"
        with _exclusive_lock(self._index_lock):
            if not self.index_path.exists():
                _write_json(self.index_path, _EMPTY_INDEX)

    def _run_dir(self, run_id: str) -> Path:
        return self.root / _check_run_id(run_id)

    def _ensure_run(self, run_id: str) -> Path:
        run_dir = self._run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        for name in ("manifests", "results", "reviews", "artifacts"):
            (run_dir / name).mkdir(exist_ok=True)
        return run_dir

    def _contract(self, kind: str, document: dict[str, Any] | ContractDocument) -> ContractDocument:
        raw = document.to_dict() if isinstance(document, ContractDocument) else copy.deepcopy(document)
        raw = _redact(raw, self.secret_values)
        try:
            return validate_contract(kind, raw)
        except ContractError as error:
            raise LedgerValidationError(str(error)) from error

    def _write_contract(
        self,
        run_id: str,
        relative_path: str,
        kind: str,
        document: dict[str, Any] | ContractDocument,
    ) -> ContractDocument:
        run_dir = self._ensure_run(run_id)
        validated = self._contract(kind, document)
        path = run_dir / relative_path
        data = canonical_bytes(validated.to_dict())
        if path.exists():
            existing = self._contract(kind, _json_document(path, path))
            if canonical_bytes(existing.to_dict()) != data:
                raise LedgerConflictError(f"{path} already contains different {kind} content")
            return existing
        atomic_write(path, data)
        return validated

    def _read_contract(self, run_id: str, relative_path: str, kind: str) -> ContractDocument:
        path = self._run_dir(run_id) / relative_path
        if not path.is_file():
            raise LedgerError(f"missing {kind} document: {path}")
        return self._contract(kind, _json_document(path, path))

    def _load_index_unlocked(self) -> dict[str, Any]:
        if not self.index_path.exists():
            _write_json(self.index_path, _EMPTY_INDEX)
        return _validate_index(_json_document(self.index_path, self.index_path))

    def _set_index(self, run_id: str, status: str, *, terminal: bool, updated_at: str | None = None) -> None:
        _check_run_id(run_id)
        if not isinstance(status, str) or not status:
            raise LedgerValidationError("index status must be non-empty")
        timestamp = _check_timestamp(updated_at, "index.updatedAt") if updated_at else utc_now()
        entry = {"runId": run_id, "status": status, "updatedAt": timestamp}
        with _exclusive_lock(self._index_lock):
            index = self._load_index_unlocked()
            index["active"] = [item for item in index["active"] if item["runId"] != run_id]
            index["recent"] = [item for item in index["recent"] if item["runId"] != run_id]
            index["recent" if terminal else "active"].append(entry)
            _write_json(self.index_path, _validate_index(index))

    def read_index(self) -> dict[str, Any]:
        with _exclusive_lock(self._index_lock):
            return _validate_index(_json_document(self.index_path, self.index_path))

    def create_run(
        self,
        run_id: str,
        request: dict[str, Any] | ContractDocument,
        *,
        timestamp: str | None = None,
    ) -> ContractDocument:
        """Durably write a request and register the run before external effects."""

        validated = self._contract("worker-request", request)
        run_dir = self._ensure_run(run_id)
        request_path = run_dir / "request.json"
        data = canonical_bytes(validated.to_dict())
        if request_path.exists():
            existing = self._contract("worker-request", _json_document(request_path, request_path))
            if canonical_bytes(existing.to_dict()) != data:
                raise LedgerConflictError(f"{request_path} already contains a different request")
            recovery = self.read_events(run_id)
            if not recovery.events:
                self._set_index(run_id, "requested", terminal=False, updated_at=timestamp)
                self.append_event(
                    {
                        "schemaVersion": 1,
                        "eventId": uuid.uuid4().hex,
                        "runId": run_id,
                        "workerId": run_id,
                        "sequence": 0,
                        "timestamp": timestamp or utc_now(),
                        "type": "run.requested",
                        "idempotencyKey": f"{run_id}/request/1",
                        "data": {},
                    }
                )
            else:
                from .reducer import reduce_events

                projection = reduce_events(recovery.events)
                disposition_path = run_dir / "disposition.json"
                if disposition_path.exists():
                    disposition = self.read_disposition(run_id)
                    self._set_index(
                        run_id,
                        disposition["status"],
                        terminal=True,
                        updated_at=disposition["updatedAt"],
                    )
                else:
                    current_entry = None
                    if self.index_path.exists():
                        current_index = self.read_index()
                        current_entry = next(
                            (item for item in current_index["active"] if item["runId"] == run_id),
                            None,
                        )
                    self._set_index(
                        run_id,
                        projection.status,
                        terminal=False,
                        updated_at=current_entry["updatedAt"] if current_entry else None,
                    )
            return existing
        atomic_write(request_path, data)
        self._set_index(run_id, "requested", terminal=False, updated_at=timestamp)
        event_time = timestamp or utc_now()
        self.append_event(
            {
                "schemaVersion": 1,
                "eventId": uuid.uuid4().hex,
                "runId": run_id,
                "workerId": run_id,
                "sequence": 0,
                "timestamp": event_time,
                "type": "run.requested",
                "idempotencyKey": f"{run_id}/request/1",
                "data": {},
            }
        )
        return validated

    def write_manifest(self, run_id: str, worker_id: str, manifest: dict[str, Any] | ContractDocument) -> ContractDocument:
        _check_run_id(worker_id, "worker id")
        validated = self._contract("worker-manifest", manifest)
        value = validated.to_dict()
        if value["runId"] != run_id or value["workerId"] != worker_id:
            raise LedgerValidationError("manifest runId/workerId do not match its ledger path")
        if not verify_content_identity("worker-manifest", value):
            raise LedgerValidationError("manifest content does not match manifestId")
        return self._write_contract(run_id, f"manifests/{worker_id}.json", "worker-manifest", validated)

    def write_result(self, run_id: str, worker_id: str, result: dict[str, Any] | ContractDocument) -> ContractDocument:
        _check_run_id(worker_id, "worker id")
        validated = self._contract("worker-result", result)
        if validated.to_dict()["workerId"] != worker_id:
            raise LedgerValidationError("result workerId does not match its ledger path")
        return self._write_contract(run_id, f"results/{worker_id}.json", "worker-result", validated)

    def write_review_packet(self, run_id: str, worker_id: str, packet: dict[str, Any] | ContractDocument) -> ContractDocument:
        _check_run_id(worker_id, "worker id")
        validated = self._contract("review-packet", packet)
        if validated.to_dict()["workerId"] != worker_id:
            raise LedgerValidationError("review packet workerId does not match its ledger path")
        return self._write_contract(run_id, f"reviews/{worker_id}.packet.json", "review-packet", validated)

    def write_review_result(self, run_id: str, worker_id: str, result: dict[str, Any] | ContractDocument) -> ContractDocument:
        _check_run_id(worker_id, "worker id")
        validated = self._contract("review-result", result)
        if validated.to_dict()["workerId"] != worker_id:
            raise LedgerValidationError("review result workerId does not match its ledger path")
        return self._write_contract(run_id, f"reviews/{worker_id}.result.json", "review-result", validated)

    def write_backend(
        self,
        run_id: str,
        backend: str,
        *,
        identities: dict[str, str] | None = None,
        cursors: dict[str, int] | None = None,
        idempotency_keys: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        document = _validate_backend(
            _redact(
                {
                    "schemaVersion": 1,
                    "runId": run_id,
                    "backend": backend,
                    "identities": identities or {},
                    "cursors": cursors or {},
                    "idempotencyKeys": idempotency_keys or {},
                },
                self.secret_values,
            )
        )
        run_dir = self._ensure_run(run_id)
        path = run_dir / "backend.json"
        return self._write_internal(path, document, _validate_backend)

    def update_backend(
        self,
        run_id: str,
        *,
        identities: dict[str, str] | None = None,
        cursors: dict[str, int] | None = None,
        idempotency_keys: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Merge newly observed Orca identities without replacing prior receipts."""

        backend_path = self._run_dir(run_id) / "backend.json"
        if backend_path.exists():
            current = self.read_backend(run_id)
        else:
            current = {
                "schemaVersion": 1,
                "runId": run_id,
                "backend": "orca-pi",
                "identities": {},
                "cursors": {},
                "idempotencyKeys": {},
            }
        current["identities"].update(identities or {})
        current["cursors"].update(cursors or {})
        current["idempotencyKeys"].update(idempotency_keys or {})
        document = _validate_backend(current)
        _write_json(backend_path, document)
        return document

    def request_cancellation(
        self,
        run_id: str,
        worker_id: str,
        dispatch_id: str,
        reason: str,
        *,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        """Persist cancellation intent before any backend lifecycle mutation."""

        now = timestamp or utc_now()
        document = _validate_cancellation(
            _redact(
                {
                    "schemaVersion": 1,
                    "runId": run_id,
                    "workerId": worker_id,
                    "dispatchId": dispatch_id,
                    "reason": reason,
                    "status": "requested",
                    "requestedAt": now,
                    "updatedAt": now,
                    "evidenceSource": "",
                    "evidenceCursor": "",
                    "evidenceDigest": "",
                    "backendState": "",
                    "lastError": "",
                },
                self.secret_values,
            )
        )
        run_dir = self._ensure_run(run_id)
        path = run_dir / "cancellation.json"
        with _exclusive_lock(run_dir / ".cancellation.lock"):
            if path.exists():
                existing = _validate_cancellation(_json_document(path, path))
                identity = ("runId", "workerId", "dispatchId", "reason")
                if any(existing[field] != document[field] for field in identity):
                    raise LedgerConflictError("cancellation intent already binds different identity or reason")
                return existing
            _write_json(path, document)
        return document

    def update_cancellation(
        self,
        run_id: str,
        status: str,
        *,
        evidence_source: str = "",
        evidence_cursor: str = "",
        evidence_digest: str = "",
        backend_state: str = "",
        last_error: str = "",
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        """Advance a persisted cancellation through its closed recovery states."""

        run_dir = self._run_dir(run_id)
        path = run_dir / "cancellation.json"
        with _exclusive_lock(run_dir / ".cancellation.lock"):
            current = _validate_cancellation(_json_document(path, path))
            if status != current["status"] and status not in _CANCELLATION_TRANSITIONS[current["status"]]:
                raise LedgerConflictError(
                    f"illegal cancellation transition {current['status']!r} -> {status!r}"
                )
            updated = _validate_cancellation(
                _redact(
                    {
                        **current,
                        "status": status,
                        "updatedAt": timestamp or utc_now(),
                        "evidenceSource": evidence_source or current["evidenceSource"],
                        "evidenceCursor": evidence_cursor or current["evidenceCursor"],
                        "evidenceDigest": evidence_digest or current["evidenceDigest"],
                        "backendState": backend_state or current["backendState"],
                        "lastError": last_error or current["lastError"],
                    },
                    self.secret_values,
                )
            )
            if status == current["status"]:
                stable_fields = set(updated) - {"updatedAt"}
                if any(updated[field] != current[field] for field in stable_fields):
                    raise LedgerConflictError("terminal cancellation state cannot be rewritten")
                return current
            _write_json(path, updated)
        return updated

    def read_cancellation(self, run_id: str) -> dict[str, Any]:
        path = self._run_dir(run_id) / "cancellation.json"
        return _validate_cancellation(_json_document(path, path))

    def write_disposition(
        self,
        run_id: str,
        status: str,
        reason: str,
        *,
        updated_at: str | None = None,
    ) -> dict[str, Any]:
        document = _validate_disposition(
            _redact(
                {
                    "schemaVersion": 1,
                    "runId": run_id,
                    "status": status,
                    "reason": reason,
                    "updatedAt": updated_at or utc_now(),
                },
                self.secret_values,
            )
        )
        run_dir = self._ensure_run(run_id)
        return self._write_internal(run_dir / "disposition.json", document, _validate_disposition)

    def mark_terminal(
        self,
        run_id: str,
        status: str,
        reason: str,
        *,
        worker_id: str | None = None,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        """Write terminal evidence and its event before moving the run to recent."""

        if status not in _TERMINAL_STATUSES:
            raise LedgerValidationError(f"{status!r} is not a terminal status")
        disposition = self.write_disposition(run_id, status, reason, updated_at=timestamp)
        report = self.read_events(run_id)
        if report.truncated:
            raise LedgerCorruptionError(
                "cannot append terminal event while the event journal has a corrupt tail",
                path=self._run_dir(run_id) / "events.jsonl",
                tail_path=report.corrupt_path,
            )
        sequence = report.events[-1].to_dict()["sequence"] + 1 if report.events else 0
        self.append_event(
            {
                "schemaVersion": 1,
                "eventId": uuid.uuid4().hex,
                "runId": run_id,
                "workerId": worker_id or run_id,
                "sequence": sequence,
                "timestamp": timestamp or disposition["updatedAt"],
                "type": _TERMINAL_EVENT_TYPES[status],
                "idempotencyKey": f"{run_id}/terminal/{status}",
                "data": {"status": status, "message": reason},
            }
        )
        self._set_index(run_id, status, terminal=True, updated_at=disposition["updatedAt"])
        return disposition

    def _write_internal(self, path: Path, document: dict[str, Any], validator) -> dict[str, Any]:
        data = canonical_bytes(document)
        if path.exists():
            existing = validator(_json_document(path, path))
            if canonical_bytes(existing) != data:
                raise LedgerConflictError(f"{path} already contains different content")
            return existing
        _write_json(path, document)
        return document

    def append_event(self, event: dict[str, Any] | ContractDocument) -> RunEvent:
        """Append one validated event, enforcing sequence and idempotency."""

        validated = self._contract("run-event", event)
        value = validated.to_dict()
        run_id = value["runId"]
        run_dir = self._ensure_run(run_id)
        path = run_dir / "events.jsonl"
        lock_path = run_dir / ".events.lock"
        with _exclusive_lock(lock_path):
            recovery = self._scan_events(path, preserve=True)
            if recovery.truncated:
                raise LedgerCorruptionError(
                    "event journal has an incomplete or corrupt trailing record",
                    path=path,
                    tail_path=recovery.corrupt_path,
                )
            for existing in recovery.events:
                existing_value = existing.to_dict()
                if existing_value["idempotencyKey"] == value["idempotencyKey"]:
                    if canonical_bytes(existing_value) == canonical_bytes(value):
                        return existing
                    raise LedgerConflictError(
                        f"idempotency key {value['idempotencyKey']!r} already has different event content"
                    )
            expected = recovery.events[-1].to_dict()["sequence"] + 1 if recovery.events else 0
            if value["sequence"] != expected:
                raise LedgerConflictError(
                    f"event sequence must be {expected}, got {value['sequence']} for run {run_id!r}"
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("ab") as output:
                output.write(canonical_bytes(value) + b"\n")
                output.flush()
                os.fsync(output.fileno())
            _fsync_directory(path.parent)
            return validated

    def _scan_events(self, path: Path, *, preserve: bool) -> EventRecovery:
        if not path.exists():
            return EventRecovery(())
        try:
            content = path.read_bytes()
        except OSError as error:
            raise LedgerCorruptionError(f"cannot read event journal: {error}", path=path) from error
        if not content:
            return EventRecovery(())
        lines = content.splitlines(keepends=True)
        events: list[RunEvent] = []
        for index, line in enumerate(lines):
            is_last = index == len(lines) - 1
            if not line.endswith(b"\n"):
                return self._tail_recovery(path, events, line, preserve=preserve)
            payload = line[:-1]
            if payload.endswith(b"\r"):
                payload = payload[:-1]
            try:
                parsed = json.loads(payload.decode("utf-8"))
                validated = self._contract("run-event", parsed)
            except (UnicodeError, json.JSONDecodeError, LedgerError) as error:
                if is_last:
                    return self._tail_recovery(path, events, line, preserve=preserve)
                raise LedgerCorruptionError(
                    f"invalid non-terminal event record {index}: {error}", path=path
                ) from error
            events.append(validated)  # type: ignore[arg-type]
        return EventRecovery(tuple(events))

    def _tail_recovery(
        self,
        path: Path,
        events: list[RunEvent],
        tail: bytes,
        *,
        preserve: bool,
    ) -> EventRecovery:
        corrupt_path = None
        if preserve:
            digest = hashlib.sha256(tail).hexdigest()[:16]
            corrupt_path = path.with_name(path.name + f".corrupt-{digest}")
            if not corrupt_path.exists():
                atomic_write(corrupt_path, tail)
        return EventRecovery(tuple(events), incomplete_tail=tail, corrupt_path=corrupt_path)

    def read_events(self, run_id: str) -> EventRecovery:
        run_dir = self._run_dir(run_id)
        path = run_dir / "events.jsonl"
        with _exclusive_lock(run_dir / ".events.lock"):
            return self._scan_events(path, preserve=True)

    def read_request(self, run_id: str) -> ContractDocument:
        return self._read_contract(run_id, "request.json", "worker-request")

    def read_manifest(self, run_id: str, worker_id: str) -> ContractDocument:
        validated = self._read_contract(
            run_id, f"manifests/{_check_run_id(worker_id, 'worker id')}.json", "worker-manifest"
        )
        if not verify_content_identity("worker-manifest", validated.to_dict()):
            raise LedgerValidationError("manifest content does not match manifestId")
        return validated

    def read_result(self, run_id: str, worker_id: str) -> ContractDocument:
        return self._read_contract(run_id, f"results/{_check_run_id(worker_id, 'worker id')}.json", "worker-result")

    def read_review_packet(self, run_id: str, worker_id: str) -> ContractDocument:
        return self._read_contract(run_id, f"reviews/{_check_run_id(worker_id, 'worker id')}.packet.json", "review-packet")

    def read_review_result(self, run_id: str, worker_id: str) -> ContractDocument:
        return self._read_contract(run_id, f"reviews/{_check_run_id(worker_id, 'worker id')}.result.json", "review-result")

    def read_backend(self, run_id: str) -> dict[str, Any]:
        path = self._run_dir(run_id) / "backend.json"
        return _validate_backend(_json_document(path, path))

    def read_disposition(self, run_id: str) -> dict[str, Any]:
        path = self._run_dir(run_id) / "disposition.json"
        return _validate_disposition(_json_document(path, path))
