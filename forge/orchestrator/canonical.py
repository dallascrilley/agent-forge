"""Canonical JSON, content-derived identities, and atomic document output."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .contracts import ContractDocument, validate_contract

_SAFE_INTEGER_MAX = (1 << 53) - 1
_IDENTITY_FIELDS = {
    "catalog-lock": ("lockId",),
    # Run and worker IDs route one attempt but do not change executable semantics.
    "worker-manifest": ("manifestId", "runId", "workerId"),
}
_ID_FIELD = {"catalog-lock": "lockId", "worker-manifest": "manifestId"}
_ZERO_HASH = "sha256:" + "0" * 64


class CanonicalizationError(ValueError):
    """A value cannot be represented by the project's canonical JSON subset."""

    def __init__(self, path: str, message: str):
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")


def _check_json_value(value: Any, path: str) -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if abs(value) > _SAFE_INTEGER_MAX:
            raise CanonicalizationError(
                path, f"integer must be between {-_SAFE_INTEGER_MAX} and {_SAFE_INTEGER_MAX}"
            )
        return
    if isinstance(value, float):
        raise CanonicalizationError(path, "floating-point numbers are not canonical")
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise CanonicalizationError(path, "string must contain valid Unicode scalar values") from error
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise CanonicalizationError(path, "object keys must be strings")
        for key in sorted(value):
            _check_json_value(value[key], f"{path}.{key}")
        return
    raise CanonicalizationError(path, f"unsupported JSON value type {type(value).__name__}")


def canonical_bytes(document: Any) -> bytes:
    """Return deterministic UTF-8 JSON bytes for the portable integer-only subset.

    Objects sort keys lexicographically, arrays retain order, strings are emitted as
    UTF-8 without ASCII escaping, and insignificant whitespace/trailing newlines are
    omitted. Floats and integers outside the interoperable JSON range fail closed.
    """

    _check_json_value(document, "$")
    text = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return text.encode("utf-8")


def _identity_payload(kind: str, document: dict[str, Any]) -> dict[str, Any]:
    if kind not in _IDENTITY_FIELDS:
        raise ValueError(f"contract has no content identity: {kind}")
    payload = copy.deepcopy(document)
    for field in _IDENTITY_FIELDS[kind]:
        payload.pop(field, None)
    return payload


def content_identity(kind: str, document: dict[str, Any]) -> str:
    """Validate a locked document and derive its stable SHA-256 identity."""

    normalized = validate_contract(kind, document).to_dict()
    digest = hashlib.sha256(canonical_bytes(_identity_payload(kind, normalized))).hexdigest()
    return f"sha256:{digest}"


def bind_content_identity(kind: str, document: dict[str, Any]) -> ContractDocument:
    """Replace a draft identity with its derived value and return an immutable contract."""

    if kind not in _ID_FIELD:
        raise ValueError(f"contract has no content identity: {kind}")
    candidate = copy.deepcopy(document)
    candidate[_ID_FIELD[kind]] = _ZERO_HASH
    normalized = validate_contract(kind, candidate).to_dict()
    normalized[_ID_FIELD[kind]] = content_identity(kind, normalized)
    return validate_contract(kind, normalized)


def verify_content_identity(kind: str, document: dict[str, Any]) -> bool:
    """Return whether a valid locked document carries its derived identity."""

    normalized = validate_contract(kind, document).to_dict()
    return normalized[_ID_FIELD[kind]] == content_identity(kind, normalized)


def atomic_write(path: str | Path, data: bytes, *, mode: int = 0o644) -> None:
    """Atomically replace ``path`` after flushing a same-directory temporary file."""

    target = Path(path)
    parent = target.parent
    existing_mode = target.stat().st_mode & 0o777 if target.exists() else mode
    descriptor, temporary_name = tempfile.mkstemp(
        dir=parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, existing_mode)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(parent, directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def write_canonical(path: str | Path, document: Any, *, mode: int = 0o644) -> bytes:
    """Canonicalize and atomically write a document, returning the exact bytes."""

    data = canonical_bytes(document)
    atomic_write(path, data, mode=mode)
    return data
