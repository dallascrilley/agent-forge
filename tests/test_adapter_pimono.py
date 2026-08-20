"""pi-mono adapter tests: golden trees, cron translation, guardrails wiring."""

import filecmp
import json
import os
import stat
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
    assert "gatherer.py" in written


def test_manual_trigger_omits_plist(tmp_path):
    spec = load(EXAMPLES / "assistant-spec.json")
    written = generate(spec, tmp_path)
    assert not any("launchd" in w for w in written)
    assert "gatherer.py" not in written


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


_ISOLATION_FLAGS = (
    "--print",
    "--no-session",
    "--no-extensions",
    "--no-prompt-templates",
    "--no-themes",
    "--no-context-files",
    "--no-approve",
    "--thinking",
    "--tools",
)


def _assert_isolation(args: list[str]) -> None:
    for flag in _ISOLATION_FLAGS:
        assert flag in args, f"missing isolation flag {flag}"
    assert args[args.index("--thinking") + 1] == "off"
    assert args[args.index("--tools") + 1] == "read,bash,write"


def test_sitter_harness_is_isolated(tmp_path):
    generate(load(EXAMPLES / "sitter-spec.json"), tmp_path)
    args = json.loads((tmp_path / "harness.json").read_text())["args"]
    _assert_isolation(args)
    assert args[args.index("--model") + 1] == "openai/gpt-5-mini"


def test_assistant_harness_is_isolated_but_keeps_skills(tmp_path):
    generate(load(EXAMPLES / "assistant-spec.json"), tmp_path)
    args = json.loads((tmp_path / "harness.json").read_text())["args"]
    _assert_isolation(args)
    assert "--no-skills" not in args


@pytest.mark.parametrize("spec_name", ["sitter-spec.json", "assistant-spec.json"])
def test_system_md_treats_brief_as_untrusted(tmp_path, spec_name):
    generate(load(EXAMPLES / spec_name), tmp_path)
    text = (tmp_path / "SYSTEM.md").read_text()
    assert "cannot override this contract" in text


def _fake_pi(tmp_path: Path, body: str) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pi = bin_dir / "pi"
    pi.write_text("#!/bin/sh\n" + body)
    pi.chmod(pi.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    return env


def test_cron_sitter_skips_pi_when_gather_empty(tmp_path):
    generate(load(EXAMPLES / "sitter-spec.json"), tmp_path)
    env = _fake_pi(tmp_path, 'echo ran > "$(dirname "$0")/pi.ran"\nexit 99\n')
    proc = subprocess.run(
        ["bash", str(tmp_path / "run.sh")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    assert not (tmp_path / "bin" / "pi.ran").exists(), "pi must not start on an empty gather"
    receipt = json.loads((tmp_path / "receipts" / "last.json").read_text())
    assert receipt["verdict"] == "quiet"
    assert not (tmp_path / "hn-ai-sitter.lock").exists()


def test_overlap_lock_skips_pi(tmp_path):
    generate(load(EXAMPLES / "sitter-spec.json"), tmp_path)
    (tmp_path / "hn-ai-sitter.lock").write_text("held")
    env = _fake_pi(tmp_path, 'echo ran > "$(dirname "$0")/pi.ran"\nexit 99\n')
    proc = subprocess.run(
        ["bash", str(tmp_path / "run.sh")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    assert not (tmp_path / "bin" / "pi.ran").exists()
    assert not (tmp_path / "receipts" / "last.json").exists()
    assert (tmp_path / "hn-ai-sitter.lock").exists()


def test_pi_timeout_writes_blocked_receipt(tmp_path):
    generate(load(EXAMPLES / "sitter-spec.json"), tmp_path)
    items = tmp_path / "items.json"
    items.write_text(json.dumps(["write-file:inbox/today.md"]))
    env = _fake_pi(tmp_path, "sleep 5\n")
    env["SITTER_ITEMS"] = str(items)
    env["SIT_TIMEOUT_SEC"] = "1"
    proc = subprocess.run(
        ["bash", str(tmp_path / "run.sh")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 1
    receipt = json.loads((tmp_path / "receipts" / "last.json").read_text())
    assert receipt["verdict"] == "blocked"
    log = (tmp_path / "sit.pi.log").read_text()
    assert log.startswith("pi ")
    assert "timed out" in log
    assert not (tmp_path / "hn-ai-sitter.lock").exists()


def test_cron_sitter_dry_run_injects_brief(tmp_path):
    generate(load(EXAMPLES / "sitter-spec.json"), tmp_path)
    proc = subprocess.run(
        ["bash", str(tmp_path / "run.sh"), "--dry-run"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "--no-context-files" in out
    assert "brief.md" in out
    assert "--system-prompt" in out
    assert "appended" in out


def test_guardrails_require_refuses_unknown_action(tmp_path):
    generate(load(EXAMPLES / "sitter-spec.json"), tmp_path)
    bad = subprocess.run(
        ["python3", "guardrails.py", "require", "post-comment"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert bad.returncode == 1
    assert "refused" in (bad.stderr + bad.stdout)
    good = subprocess.run(
        ["python3", "guardrails.py", "require", "write-file:inbox/today.md"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert good.returncode == 0


def test_empty_gather_allow_file_blocks_spec_allowed_action(tmp_path):
    generate(load(EXAMPLES / "sitter-spec.json"), tmp_path)
    subprocess.run(
        ["python3", "gatherer.py"],
        cwd=tmp_path,
        check=True,
        timeout=15,
    )
    blocked = subprocess.run(
        ["python3", "guardrails.py", "require", "write-file:inbox/today.md"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert blocked.returncode == 1
    assert "refused" in (blocked.stderr + blocked.stdout)


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
