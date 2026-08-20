"""LangGraph adapter tests: golden tree, MCP wiring, guardrails wiring."""

import filecmp
import json
from pathlib import Path

from forge.adapters.langgraph import _langchain_model_id, generate
from forge.spec import load

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"
GOLDEN = REPO / "tests" / "golden" / "langgraph"


def _tree_files(root: Path):
    return sorted(
        str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()
    )


def test_golden_tree(tmp_path):
    spec = load(EXAMPLES / "assistant-spec.json")
    out = tmp_path / "out"
    generate(spec, out)
    golden = GOLDEN / "assistant"
    gen_files, gold_files = _tree_files(out), _tree_files(golden)
    assert gen_files == gold_files, (
        f"file sets differ: {gen_files} vs {gold_files}. If intended, "
        "re-bless: python3 tests/bless_golden.py"
    )
    diffs = [
        rel
        for rel in gen_files
        if not filecmp.cmp(out / rel, golden / rel, shallow=False)
    ]
    assert not diffs, (
        "content drifted: " + ", ".join(diffs) +
        ". If intended, re-bless: python3 tests/bless_golden.py"
    )


def test_mcp_block_present_iff_servers(tmp_path):
    assistant = load(EXAMPLES / "assistant-spec.json")
    generate(assistant, tmp_path / "with")
    agent_py = (tmp_path / "with" / "my_agent" / "agent.py").read_text()
    assert "MultiServerMCPClient" in agent_py
    assert "GUARDRAILS.wrap" in agent_py  # guardrails call site
    assert "async def graph()" in agent_py  # factory: no import-time MCP connect

    sitter_data = json.loads((EXAMPLES / "sitter-spec.json").read_text())
    sitter_data["runtimes"] = ["langgraph"]
    from forge.spec import validate

    sitter = validate(sitter_data, EXAMPLES)
    generate(sitter, tmp_path / "without")
    agent_py2 = (tmp_path / "without" / "my_agent" / "agent.py").read_text()
    assert "MultiServerMCPClient" not in agent_py2
    assert "tools=[]" in agent_py2
    assert "graph = create_agent(" in agent_py2  # plain variable, no factory


def test_langgraph_json_pointer_resolves(tmp_path):
    spec = load(EXAMPLES / "assistant-spec.json")
    generate(spec, tmp_path)
    cfg = json.loads((tmp_path / "langgraph.json").read_text())
    path, _, var = cfg["graphs"][spec.name].partition(":")
    assert (tmp_path / path).is_file()
    assert var == "graph"


def test_model_id_conversion():
    assert _langchain_model_id("anthropic/claude-sonnet-4-5") == (
        "anthropic:claude-sonnet-4-5"
    )
    assert _langchain_model_id("gpt-5") == "gpt-5"


def test_guardrails_allowed_tools_enforced(tmp_path):
    spec = load(EXAMPLES / "assistant-spec.json")
    generate(spec, tmp_path)
    script = (
        "import sys; sys.path.insert(0, %r);"
        "from my_agent.guardrails import Guardrails;"
        "g = Guardrails();"
        "assert g.check('read_file') is None;"
        "assert g.check('write_file') is not None, 'not in allowed_tools';"
        "assert not g.stopped()"
        % str(tmp_path)
    )
    import subprocess

    subprocess.run(["python3", "-c", script], check=True, cwd=tmp_path)


def test_provider_package_inferred(tmp_path):
    spec = load(EXAMPLES / "assistant-spec.json")
    generate(spec, tmp_path)
    toml = (tmp_path / "pyproject.toml").read_text()
    assert "langchain-anthropic" in toml  # from model "anthropic/..."


def test_no_langsmith_in_output(tmp_path):
    spec = load(EXAMPLES / "assistant-spec.json")
    generate(spec, tmp_path)
    for p in tmp_path.rglob("*"):
        if p.is_file() and p.suffix in (".py", ".toml", ".json"):
            assert "langsmith" not in p.read_text().lower(), p
