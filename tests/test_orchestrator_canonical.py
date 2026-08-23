"""Canonical JSON, content identities, and atomic output are deterministic."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from forge.orchestrator.canonical import (
    CanonicalizationError,
    atomic_write,
    bind_content_identity,
    canonical_bytes,
    content_identity,
    verify_content_identity,
)
from forge.orchestrator.contracts import ContractError

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures" / "orchestrator"
VALID = json.loads((FIXTURES / "valid-contracts.json").read_text(encoding="utf-8"))
IDENTITIES = json.loads((FIXTURES / "identity-golden.json").read_text(encoding="utf-8"))


def test_canonical_bytes_match_utf8_golden_without_trailing_newline():
    document = json.loads((FIXTURES / "canonical-input.json").read_text(encoding="utf-8"))
    expected = (FIXTURES / "canonical.golden.json").read_bytes()
    assert canonical_bytes(document) == expected
    assert "é雪".encode() in expected
    assert not expected.endswith(b"\n")


def test_separate_processes_and_hash_seeds_emit_identical_bytes():
    code = """
import json, sys
from forge.orchestrator.canonical import canonical_bytes
with open(sys.argv[1], encoding='utf-8') as source:
    sys.stdout.buffer.write(canonical_bytes(json.load(source)))
"""
    outputs = []
    for seed in ("1", "777"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        outputs.append(
            subprocess.check_output(
                [sys.executable, "-c", code, str(FIXTURES / "canonical-input.json")],
                cwd=REPO,
                env=env,
            )
        )
    assert outputs[0] == outputs[1] == (FIXTURES / "canonical.golden.json").read_bytes()


@pytest.mark.parametrize(
    "document,path",
    [
        ({"value": 1.0}, "$.value"),
        ({"value": float("nan")}, "$.value"),
        ({"value": 1 << 53}, "$.value"),
        ({1: "non-string key"}, "$"),
        ({"value": "\ud800"}, "$.value"),
    ],
)
def test_non_portable_json_values_fail_closed(document, path):
    with pytest.raises(CanonicalizationError) as exc:
        canonical_bytes(document)
    assert exc.value.path == path


@pytest.mark.parametrize("kind", ["catalog-lock", "worker-manifest"])
def test_content_identity_matches_golden(kind):
    bound = bind_content_identity(kind, VALID[kind])
    assert bound.to_dict()["lockId" if kind == "catalog-lock" else "manifestId"] == IDENTITIES[kind]
    assert content_identity(kind, bound.to_dict()) == IDENTITIES[kind]
    assert verify_content_identity(kind, bound.to_dict()) is True


def test_runtime_assignment_changes_do_not_change_manifest_identity():
    first = bind_content_identity("worker-manifest", VALID["worker-manifest"])
    changed = first.to_dict()
    changed["runId"] = "run-another-attempt"
    changed["workerId"] = "another-worker"
    changed["manifestId"] = "sha256:" + "0" * 64
    second = bind_content_identity("worker-manifest", changed)
    assert second.to_dict()["manifestId"] == first.to_dict()["manifestId"]


def test_semantically_relevant_change_alters_manifest_identity():
    first = bind_content_identity("worker-manifest", VALID["worker-manifest"])
    changed = first.to_dict()
    changed["task"] = "Inspect a different parser."
    second = bind_content_identity("worker-manifest", changed)
    assert second.to_dict()["manifestId"] != first.to_dict()["manifestId"]


def test_runtime_handles_cannot_enter_or_influence_a_manifest():
    document = copy.deepcopy(VALID["worker-manifest"])
    document["backendHandle"] = "terminal-unsafe"
    with pytest.raises(ContractError) as exc:
        bind_content_identity("worker-manifest", document)
    assert "$.backendHandle" in [problem.path for problem in exc.value.problems]


def test_verify_identity_detects_tampering():
    document = bind_content_identity("catalog-lock", VALID["catalog-lock"]).to_dict()
    document["sourceHash"] = "sha256:" + "9" * 64
    assert verify_content_identity("catalog-lock", document) is False


def test_atomic_write_replaces_bytes_and_leaves_no_temporary_file(tmp_path):
    target = tmp_path / "catalog.lock.json"
    target.write_bytes(b"old")
    atomic_write(target, b"new")
    assert target.read_bytes() == b"new"
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_write_retains_previous_file_on_replace_failure(tmp_path, monkeypatch):
    target = tmp_path / "catalog.lock.json"
    target.write_bytes(b"previous-valid-lock")

    def fail_replace(source, destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        atomic_write(target, b"new-invalid-lock")
    assert target.read_bytes() == b"previous-valid-lock"
    assert list(tmp_path.iterdir()) == [target]
