"""MCP newline JSON-RPC framing tests without a live MCP dependency."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from forge.adapters.pimono import generate
from forge.spec import load


REPO = Path(__file__).resolve().parent.parent
FAKE_SERVER = REPO / "tests" / "fake_mcp_server.py"
EXAMPLES = REPO / "examples"


def _request(proc, request):
    proc.stdin.write(json.dumps(request) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


def test_fake_mcp_server_covers_initialize_list_and_call():
    proc = subprocess.Popen(
        [sys.executable, str(FAKE_SERVER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        initialized = _request(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {},
            },
        )
        assert initialized["result"]["serverInfo"]["name"] == "fake-mcp"

        listed = _request(
            proc,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert listed["result"]["tools"][0]["name"] == "read_file"

        called = _request(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "read_file", "arguments": {"path": "docs/a.md"}},
            },
        )
        assert called["result"]["content"][0]["text"] == "fake contents: docs/a.md"
    finally:
        if proc.stdin:
            proc.stdin.close()
        proc.terminate()
        proc.wait(timeout=5)


def test_generated_mcp_extension_keeps_both_wire_framings(tmp_path):
    generate(load(EXAMPLES / "assistant-spec.json"), tmp_path)
    mcp_ts = (tmp_path / "mcp.ts").read_text(encoding="utf-8")

    assert 'return JSON.stringify(msg) + "\\n";' in mcp_ts
    assert "if (/^Content-Length:/i.test(this.buf))" in mcp_ts
    assert 'const idx = this.buf.indexOf("\\r\\n\\r\\n")' in mcp_ts
    assert 'const nl = this.buf.indexOf("\\n")' in mcp_ts
    assert "this.dispatch(JSON.parse(line));" in mcp_ts
