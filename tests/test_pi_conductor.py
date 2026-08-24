"""No-model RPC canary for the single conductor tool."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("pi") is None, reason="pi CLI is not installed")
def test_rpc_canary_observes_only_conductor_tool(tmp_path):
    wrapper = tmp_path / "wrapper.ts"
    extension = (REPO / "extensions" / "agent-forge-conductor.ts").as_posix()
    wrapper.write_text(
        f'import conductor from "{extension}";\n'
        'export default function (pi) {\n'
        '  conductor(pi);\n'
        '  pi.on("session_start", () => pi.appendEntry("canary-tools", { tools: pi.getActiveTools() }));\n'
        '}\n',
        encoding="utf-8",
    )
    command = [
        "pi",
        "--mode",
        "rpc",
        "--session-dir",
        str(tmp_path / "sessions"),
        "--no-extensions",
        "--tools",
        "read,grep,find,ls,delegate",
        "--extension",
        str(wrapper),
    ]
    process = subprocess.run(
        command,
        cwd=REPO,
        input='{"id":"entries","type":"get_entries"}\n',
        text=True,
        capture_output=True,
        env={**os.environ, "PI_OFFLINE": "1"},
        timeout=30,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    responses = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
    response = next(item for item in responses if item.get("id") == "entries")
    assert response["success"] is True
    canary = next(entry for entry in response["data"]["entries"] if entry.get("customType") == "canary-tools")
    assert canary["data"]["tools"] == ["read", "grep", "find", "ls", "delegate"]
    assert not set(canary["data"]["tools"]) & {"bash", "edit", "write"}


@pytest.mark.skipif(shutil.which("pi") is None, reason="pi CLI is not installed")
def test_worker_submit_rpc_canary_exposes_only_observe_and_submission_tools(tmp_path):
    wrapper = tmp_path / "worker-wrapper.ts"
    extension = (REPO / "catalog" / "resources" / "worker-submit.ts").as_posix()
    wrapper.write_text(
        f'import workerSubmit from "{extension}";\n'
        'export default function (pi) {\n'
        '  workerSubmit(pi);\n'
        '  pi.on("session_start", () => pi.appendEntry("canary-tools", { tools: pi.getActiveTools() }));\n'
        '}\n',
        encoding="utf-8",
    )
    process = subprocess.run(
        [
            "pi",
            "--mode",
            "rpc",
            "--session-dir",
            str(tmp_path / "sessions"),
            "--no-extensions",
            "--tools",
            "read,grep,find,ls,submit_worker_result",
            "--extension",
            str(wrapper),
        ],
        cwd=REPO,
        input='{"id":"entries","type":"get_entries"}\n',
        text=True,
        capture_output=True,
        env={**os.environ, "PI_OFFLINE": "1"},
        timeout=30,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    responses = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
    response = next(item for item in responses if item.get("id") == "entries")
    canary = next(
        entry for entry in response["data"]["entries"] if entry.get("customType") == "canary-tools"
    )
    assert canary["data"]["tools"] == [
        "read",
        "grep",
        "find",
        "ls",
        "submit_worker_result",
    ]
    assert not set(canary["data"]["tools"]) & {"bash", "edit", "write", "delegate"}
