"""pi-mono adapter tests: golden trees, cron translation, guardrails wiring."""

import filecmp
import json
import subprocess
from pathlib import Path

import pytest

from forge.adapters.pimono import _cron_to_calendar_interval, generate
from forge.spec import load

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"
GOLDEN = REPO / "tests" / "golden" / "pimono"


def _tree_files(root: Path):
    return sorted(
        str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()
    )


def _diff_trees(left: Path, right: Path) -> list[str]:
    problems = []
    left_files, right_files = _tree_files(left), _tree_files(right)
    if left_files != right_files:
        problems.append(
            f"file sets differ: only-left={sorted(set(left_files)-set(right_files))} "
            f"only-right={sorted(set(right_files)-set(left_files))}"
        )
    for rel in sorted(set(left_files) & set(right_files)):
        if not filecmp.cmp(left / rel, right / rel, shallow=False):
            problems.append(f"content differs: {rel}")
    return problems


@pytest.mark.parametrize(
    "spec_name,golden_name",
    [("sitter-spec.json", "sitter"), ("assistant-spec.json", "assistant")],
)
def test_golden_tree(tmp_path, spec_name, golden_name):
    spec = load(EXAMPLES / spec_name)
    out = tmp_path / "out"
    generate(spec, out)
    problems = _diff_trees(out, GOLDEN / golden_name)
    assert not problems, (
        "generated tree drifted from golden. If the change is intended, "
        "re-bless: python3 tests/bless_golden.py\n" + "\n".join(problems)
    )


def test_cron_trigger_emits_plist(tmp_path):
    spec = load(EXAMPLES / "sitter-spec.json")
    written = generate(spec, tmp_path)
    assert f"launchd/local.{spec.name}.plist" in written


def test_manual_trigger_omits_plist(tmp_path):
    spec = load(EXAMPLES / "assistant-spec.json")
    written = generate(spec, tmp_path)
    assert not any("launchd" in w for w in written)


def test_mcp_json_only_when_servers(tmp_path):
    sitter = load(EXAMPLES / "sitter-spec.json")
    assistant = load(EXAMPLES / "assistant-spec.json")
    assert "mcp.json" not in generate(sitter, tmp_path / "a")
    assert "mcp.json" in generate(assistant, tmp_path / "b")
    mcp = json.loads((tmp_path / "b" / "mcp.json").read_text())
    assert mcp["mcpServers"]["filesystem"]["command"] == "npx"


def test_guardrails_call_sites_present(tmp_path):
    spec = load(EXAMPLES / "sitter-spec.json")
    generate(spec, tmp_path)
    run_sh = (tmp_path / "run.sh").read_text()
    system_md = (tmp_path / "SYSTEM.md").read_text()
    assert "guardrails.py" in run_sh  # stop-file paused receipt call site
    assert "guardrails" in system_md  # operating contract section
    assert "hn-ai-sitter.stop" in system_md


def test_no_skills_flag_only_when_no_skills(tmp_path):
    sitter = load(EXAMPLES / "sitter-spec.json")
    assistant = load(EXAMPLES / "assistant-spec.json")
    generate(sitter, tmp_path / "a")
    generate(assistant, tmp_path / "b")
    a = json.loads((tmp_path / "a" / "harness.json").read_text())
    b = json.loads((tmp_path / "b" / "harness.json").read_text())
    assert "--no-skills" in a["args"]
    assert "--no-skills" not in b["args"]


def test_guardrails_helper_runtime_behavior(tmp_path):
    spec = load(EXAMPLES / "sitter-spec.json")
    generate(spec, tmp_path)
    script = (
        "import sys; sys.path.insert(0, %r);"
        "import guardrails as g;"
        "b = g.Budget();"
        "assert b.allow('write-file:inbox/today.md');"
        "assert not b.allow('write-file:inbox/other.md'), 'budget of 1';"
        "assert not b.allow('post-comment');"
        "assert g.allow('write-file:inbox/x');"
        "assert not g.allow('delete-everything');"
        "g.write_receipt('quiet', 'nothing to do')"
        % str(tmp_path)
    )
    subprocess.run(["python3", "-c", script], check=True, cwd=tmp_path)
    receipt = json.loads((tmp_path / "receipts" / "last.json").read_text())
    assert receipt["verdict"] == "quiet"


# --- cron translation -------------------------------------------------------


def test_cron_simple():
    assert _cron_to_calendar_interval("17 8 * * *") == {"Minute": 17, "Hour": 8}


def test_cron_step_minutes():
    assert _cron_to_calendar_interval("*/15 * * * *") == {
        "Minute": [0, 15, 30, 45]
    }


def test_cron_weekday_7_is_sunday_0():
    assert _cron_to_calendar_interval("0 9 * * 7")["Weekday"] == 0


def test_cron_day_of_month():
    assert _cron_to_calendar_interval("0 9 1 * *")["Day"] == 1


def test_cron_step_rejected_for_day():
    with pytest.raises(ValueError):
        _cron_to_calendar_interval("0 0 */2 * *")
