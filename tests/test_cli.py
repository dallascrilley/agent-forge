"""CLI tests for the non-interactive producer path."""

import json
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def run_forge(*args, cwd=REPO):
    return subprocess.run(
        [sys.executable, "-m", "forge", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def test_module_help_works():
    result = run_forge("--help")
    assert "new" in result.stdout


def test_new_then_validate_and_generate(tmp_path):
    spec_path = tmp_path / "daily-spec.json"
    result = run_forge(
        "new",
        "--name",
        "daily-summarizer",
        "--purpose",
        "Summarize the daily inbox.",
        "--model",
        "openai/gpt-5-mini",
        "--runtime",
        "pimono",
        "--runtime",
        "langgraph",
        "--runtime",
        "eve",
        "--out",
        str(spec_path),
        "--cron",
        "17 8 * * *",
        "--mcp",
        "filesystem=npx -y @modelcontextprotocol/server-filesystem ./docs",
        "--side-effect",
        "write-file:inbox/",
        "--system-prompt",
        "Summarize only the supplied inbox.",
    )

    assert f"wrote {spec_path}" in result.stdout
    data = json.loads(spec_path.read_text(encoding="utf-8"))
    assert data["runtimes"] == ["pimono", "langgraph", "eve"]
    assert data["trigger"] == {"type": "cron", "schedule": "17 8 * * *"}
    assert data["mcp_servers"]["filesystem"] == {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "./docs"],
    }

    validated = run_forge("validate", str(spec_path))
    assert "ok: daily-summarizer" in validated.stdout

    bundle = tmp_path / "bundle"
    generated = run_forge(
        "generate",
        str(spec_path),
        "--runtime",
        "pimono",
        "--out",
        str(bundle),
    )
    assert "wrote" in generated.stdout
    assert (bundle / "run.sh").is_file()

    eve_bundle = tmp_path / "eve-bundle"
    run_forge(
        "generate",
        str(spec_path),
        "--runtime",
        "eve",
        "--out",
        str(eve_bundle),
    )
    assert (eve_bundle / "agent" / "agent.ts").is_file()
