"""Eve adapter tests: filesystem shape, guardrails, and golden trees."""

from __future__ import annotations

import filecmp
import json
from pathlib import Path

from forge import KNOWN_RUNTIMES
from forge.adapters.eve import generate
from forge.spec import load, validate


REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"
GOLDEN = REPO / "tests" / "golden" / "eve"


def _tree_files(root: Path):
    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())


def _diff_tree(actual: Path, expected: Path):
    actual_files, expected_files = _tree_files(actual), _tree_files(expected)
    problems = [
        f"missing from generated: {rel}" for rel in sorted(set(expected_files) - set(actual_files))
    ]
    problems += [
        f"unexpected generated file: {rel}" for rel in sorted(set(actual_files) - set(expected_files))
    ]
    problems += [
        f"content differs: {rel}"
        for rel in sorted(set(actual_files) & set(expected_files))
        if not filecmp.cmp(actual / rel, expected / rel, shallow=False)
    ]
    return problems


def test_eve_is_registered_and_validator_accepts():
    assert "eve" in KNOWN_RUNTIMES
    data = json.loads((EXAMPLES / "sitter-spec.json").read_text(encoding="utf-8"))
    data["runtimes"] = ["eve"]
    assert validate(data, EXAMPLES).runtimes == ["eve"]


def test_golden_trees(tmp_path):
    for spec_name, golden_name in (
        ("sitter-spec.json", "sitter"),
        ("assistant-spec.json", "assistant"),
    ):
        actual = tmp_path / golden_name
        generate(load(EXAMPLES / spec_name), actual)
        problems = _diff_tree(actual, GOLDEN / golden_name)
        assert not problems, (
            "generated Eve tree drifted. If intended, re-bless with "
            "python3 tests/bless_golden.py\n" + "\n".join(problems)
        )


def test_guardrails_and_external_tools_are_mechanical(tmp_path):
    out = tmp_path / "assistant"
    generate(load(EXAMPLES / "assistant-spec.json"), out)
    agent = (out / "agent" / "agent.ts").read_text(encoding="utf-8")
    guardrails = (out / "agent" / "guardrails.ts").read_text(encoding="utf-8")
    mcp = json.loads(
        (out / "agent" / "tools" / "mcp-servers.json").read_text(encoding="utf-8")
    )

    assert "runGuarded" in agent
    for call_site in ("checkStopFile", "checkTool", "requireSideEffect", "writeReceipt"):
        assert f"GUARDRAILS.{call_site}" in agent
    for policy in ("stopFile", "allowedTools", "allowedSideEffects", "maxActions", "receipt"):
        assert policy in guardrails
    assert mcp["mcpServers"]["filesystem"]["command"] == "npx"


def test_cron_emits_schedule_and_skips_mcp(tmp_path):
    out = tmp_path / "sitter"
    generate(load(EXAMPLES / "sitter-spec.json"), out)
    schedule = out / "agent" / "schedules" / "hn-ai-sitter.ts"
    text = schedule.read_text(encoding="utf-8")
    assert 'import { defineSchedule } from "eve/schedules";' in text
    assert "defineSchedule({" in text
    assert "17 8 * * *" in text
    assert not (out / "agent" / "tools").exists()
