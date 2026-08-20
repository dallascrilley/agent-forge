"""Validator tests: examples pass, malformed specs fail with named errors,
defaults are applied, and the shipped JSON Schema agrees with the validator."""

import copy
import json
from pathlib import Path

import pytest

from forge.errors import SpecError
from forge.spec import load, validate

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"
SCHEMA_PATH = REPO / "schema" / "agent-spec.schema.json"


def load_example(name):
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def problems_of(data):
    with pytest.raises(SpecError) as exc:
        validate(data, EXAMPLES)
    return exc.value.problems


# --- valid specs ---------------------------------------------------------


def test_sitter_example_valid():
    spec = load(EXAMPLES / "sitter-spec.json")
    assert spec.name == "hn-ai-sitter"
    assert spec.trigger == {"type": "cron", "schedule": "17 8 * * *"}


def test_assistant_example_valid():
    spec = load(EXAMPLES / "assistant-spec.json")
    assert spec.runtimes == ["pimono", "langgraph"]
    assert "doc-assistant" in spec.system_prompt_text()
    assert spec.skills[0]["name"] == "summarize-doc"


def test_guardrails_defaults_applied():
    spec = load(EXAMPLES / "sitter-spec.json")
    g = spec.guardrails
    assert g["stop_file"] == "hn-ai-sitter.stop"
    assert g["receipt"]["path"] == "receipts/last.json"
    assert g["llm_optional"] is True
    assert g["allowed_side_effects"] == ["write-file:inbox/"]
    assert g["max_actions"] == 1


def test_guardrails_omitted_entirely():
    data = load_example("sitter-spec.json")
    del data["guardrails"]
    spec = validate(data, EXAMPLES)
    assert spec.guardrails["allowed_side_effects"] == []
    assert spec.guardrails["max_actions"] == 3
    assert spec.guardrails["stop_file"] == "hn-ai-sitter.stop"


def test_trigger_defaults_to_manual():
    data = load_example("sitter-spec.json")
    del data["trigger"]
    spec = validate(data, EXAMPLES)
    assert spec.trigger == {"type": "manual"}


# --- malformed specs ------------------------------------------------------


def test_rejects_wrong_spec_version():
    data = load_example("sitter-spec.json")
    data["spec_version"] = 2
    assert any("spec_version" in p for p in problems_of(data))


def test_rejects_unknown_top_level_key():
    data = load_example("sitter-spec.json")
    data["surprise"] = True
    assert any("surprise" in p and "unknown field" in p for p in problems_of(data))


def test_rejects_bad_name():
    data = load_example("sitter-spec.json")
    data["name"] = "Bad_Name"
    assert any("name" in p for p in problems_of(data))


def test_rejects_empty_purpose_and_model():
    data = load_example("sitter-spec.json")
    data["purpose"] = "  "
    data["model"] = ""
    probs = problems_of(data)
    assert any("purpose" in p for p in probs)
    assert any("model" in p for p in probs)


def test_rejects_unknown_runtime_and_duplicates():
    data = load_example("sitter-spec.json")
    data["runtimes"] = ["pimono", "pimono", "cobol"]
    probs = problems_of(data)
    assert any("cobol" in p for p in probs)
    assert any("unique" in p for p in probs)


def test_rejects_cron_without_schedule():
    data = load_example("sitter-spec.json")
    data["trigger"] = {"type": "cron"}
    assert any("schedule" in p for p in problems_of(data))


def test_rejects_schedule_on_manual_trigger():
    data = load_example("sitter-spec.json")
    data["trigger"] = {"type": "manual", "schedule": "0 9 * * *"}
    assert any("schedule" in p for p in problems_of(data))


def test_rejects_skill_with_body_and_file():
    data = load_example("assistant-spec.json")
    data["skills"][0]["file"] = "other.md"
    assert any("not both" in p for p in problems_of(data))


def test_rejects_skill_without_content():
    data = load_example("assistant-spec.json")
    del data["skills"][0]["body"]
    assert any("required" in p for p in problems_of(data))


def test_rejects_duplicate_skill_names():
    data = load_example("assistant-spec.json")
    data["skills"].append(copy.deepcopy(data["skills"][0]))
    assert any("duplicate" in p for p in problems_of(data))


def test_rejects_mcp_server_with_command_and_url():
    data = load_example("assistant-spec.json")
    data["mcp_servers"]["filesystem"]["url"] = "http://localhost:9999/mcp"
    assert any("not both" in p for p in problems_of(data))


def test_rejects_mcp_server_with_neither():
    data = load_example("assistant-spec.json")
    data["mcp_servers"]["filesystem"] = {}
    assert any("required" in p for p in problems_of(data))


def test_rejects_bool_max_actions():
    data = load_example("sitter-spec.json")
    data["guardrails"]["max_actions"] = True
    assert any("max_actions" in p for p in problems_of(data))


def test_rejects_bad_allowed_tools():
    data = load_example("assistant-spec.json")
    data["guardrails"]["allowed_tools"] = "read_file"
    assert any("allowed_tools" in p for p in problems_of(data))


def test_model_overrides_resolve_per_runtime():
    spec = load(EXAMPLES / "assistant-spec.json")
    assert spec.model == "openai-codex/gpt-5.4-mini"
    assert spec.model_for("pimono") == "openai-codex/gpt-5.4-mini"
    assert spec.model_for("langgraph") == "openai/gpt-5-mini"


def test_rejects_bad_model_overrides():
    data = load_example("sitter-spec.json")
    data["model_overrides"] = {"cobol": "x", "langgraph": "  "}
    probs = problems_of(data)
    assert any("cobol" in p for p in probs)
    assert any("non-empty" in p for p in probs)


def test_collects_multiple_problems():
    data = load_example("sitter-spec.json")
    data["name"] = "BAD"
    data["spec_version"] = 9
    data["trigger"] = {"type": "cron"}
    assert len(problems_of(data)) >= 3


def test_system_prompt_missing_file():
    data = load_example("assistant-spec.json")
    data["system_prompt"] = {"file": "nope.md"}
    assert any("no such file" in p for p in problems_of(data))


# --- schema agreement -----------------------------------------------------


def test_examples_validate_against_shipped_json_schema():
    jsonschema = pytest.importorskip(
        "jsonschema", reason="dev dependency; validator is the authority"
    )
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    for name in ("sitter-spec.json", "assistant-spec.json"):
        jsonschema.validate(load_example(name), schema)


def _assert_rejected_by_validator_and_schema(data):
    jsonschema = pytest.importorskip(
        "jsonschema", reason="dev dependency; validator is the authority"
    )
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    with pytest.raises(SpecError):
        validate(data, EXAMPLES)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, schema)


def test_schema_and_validator_reject_cron_without_schedule():
    data = load_example("sitter-spec.json")
    data["trigger"] = {"type": "cron"}
    _assert_rejected_by_validator_and_schema(data)


def test_schema_and_validator_reject_skill_body_and_file():
    data = load_example("assistant-spec.json")
    data["skills"][0]["file"] = "assistant-prompt.md"
    _assert_rejected_by_validator_and_schema(data)


def test_schema_and_validator_reject_skill_neither_body_nor_file():
    data = load_example("assistant-spec.json")
    del data["skills"][0]["body"]
    _assert_rejected_by_validator_and_schema(data)


def test_schema_and_validator_reject_unknown_model_overrides_key():
    data = load_example("sitter-spec.json")
    data["model_overrides"] = {"cobol": "x"}
    _assert_rejected_by_validator_and_schema(data)
