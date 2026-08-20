"""Load, validate, and normalize an agent-forge spec.

Stdlib-only by design: any agent with a python3 can run the generator.
The hand-rolled validator below is the authority; schema/agent-spec.schema.json
is the editor/documentation aid. tests/test_spec.py keeps the two in agreement
on every example spec.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import KNOWN_RUNTIMES, SPEC_VERSION
from .errors import SpecError

_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_CRON_RE = re.compile(r"^\S+( \S+){4}$")

_TOP_KEYS = {
    "spec_version", "name", "description", "purpose", "system_prompt",
    "skills", "mcp_servers", "plugins", "model", "runtimes", "trigger",
    "guardrails",
}


@dataclass
class Spec:
    """A validated spec with defaults applied."""

    name: str
    purpose: str
    model: str
    runtimes: list[str]
    description: str = ""
    system_prompt: dict | None = None  # {"inline": ...} or {"file": ...}
    skills: list[dict] = field(default_factory=list)
    mcp_servers: dict = field(default_factory=dict)
    plugins: list[dict] = field(default_factory=list)
    trigger: dict = field(default_factory=lambda: {"type": "manual"})
    guardrails: dict = field(default_factory=dict)
    spec_dir: Path = field(default_factory=Path)

    def __post_init__(self):
        g = self.guardrails
        g.setdefault("allowed_side_effects", [])
        g.setdefault("stop_file", f"{self.name}.stop")
        g.setdefault("receipt", {})
        g["receipt"].setdefault("path", "receipts/last.json")
        g.setdefault("llm_optional", True)
        g.setdefault("max_actions", 3)

    def system_prompt_text(self) -> str:
        """Resolve the system prompt to text (inline, or read relative to the spec)."""
        if self.system_prompt is None:
            return ""
        if "inline" in self.system_prompt:
            return self.system_prompt["inline"]
        return (self.spec_dir / self.system_prompt["file"]).read_text(
            encoding="utf-8"
        )


def _is_str_map(v) -> bool:
    return isinstance(v, dict) and all(
        isinstance(k, str) and isinstance(x, str) for k, x in v.items()
    )


def validate(data, spec_dir: Path) -> Spec:
    """Validate raw parsed JSON; return a normalized Spec or raise SpecError."""
    problems: list[str] = []

    def err(path: str, msg: str):
        problems.append(f"{path}: {msg}")

    if not isinstance(data, dict):
        raise SpecError(["$: spec must be a JSON object"])

    for key in sorted(set(data) - _TOP_KEYS):
        err(f"$.{key}", "unknown field")

    # spec_version
    if data.get("spec_version") != SPEC_VERSION:
        err("$.spec_version", f"must be {SPEC_VERSION}")

    # name
    name = data.get("name")
    if not isinstance(name, str) or not _SLUG_RE.match(name or ""):
        err("$.name", "required; kebab-case slug matching ^[a-z][a-z0-9-]*$")

    # purpose / model
    for field_name in ("purpose", "model"):
        v = data.get(field_name)
        if not isinstance(v, str) or not v.strip():
            err(f"$.{field_name}", "required; non-empty string")

    # description
    if "description" in data and not isinstance(data["description"], str):
        err("$.description", "must be a string")

    # runtimes
    runtimes = data.get("runtimes")
    if not isinstance(runtimes, list) or not runtimes:
        err("$.runtimes", "required; non-empty array")
    elif isinstance(runtimes, list):
        for i, rt in enumerate(runtimes):
            if rt not in KNOWN_RUNTIMES:
                err(
                    f"$.runtimes[{i}]",
                    f"unknown runtime {rt!r}; known: {', '.join(KNOWN_RUNTIMES)}",
                )
        if len(set(map(str, runtimes))) != len(runtimes):
            err("$.runtimes", "entries must be unique")

    # system_prompt
    sp = data.get("system_prompt")
    if sp is not None:
        if not isinstance(sp, dict) or len(sp) != 1 or not (
            {"inline", "file"} & sp.keys()
        ):
            err("$.system_prompt", 'must be {"inline": "..."} or {"file": "..."}')
        else:
            v = next(iter(sp.values()))
            if not isinstance(v, str) or not v.strip():
                err("$.system_prompt", "value must be a non-empty string")
            elif "file" in sp and not (spec_dir / v).is_file():
                err("$.system_prompt.file", f"no such file relative to spec: {v}")

    # skills
    skills = data.get("skills", [])
    if not isinstance(skills, list):
        err("$.skills", "must be an array")
        skills = []
    seen_skill_names: set[str] = set()
    for i, sk in enumerate(skills):
        p = f"$.skills[{i}]"
        if not isinstance(sk, dict):
            err(p, "must be an object")
            continue
        sname = sk.get("name")
        if not isinstance(sname, str) or not _SLUG_RE.match(sname or ""):
            err(f"{p}.name", "required; kebab-case slug")
        elif sname in seen_skill_names:
            err(f"{p}.name", f"duplicate skill name {sname!r}")
        else:
            seen_skill_names.add(sname)
        if not isinstance(sk.get("description"), str) or not (
            sk.get("description") or ""
        ).strip():
            err(f"{p}.description", "required; non-empty string")
        has_body = isinstance(sk.get("body"), str) and sk["body"].strip()
        has_file = isinstance(sk.get("file"), str) and sk["file"].strip()
        if has_body and has_file:
            err(p, "declare 'body' or 'file', not both")
        elif not has_body and not has_file:
            err(p, "one of 'body' or 'file' is required")
        elif has_file and not (spec_dir / sk["file"]).is_file():
            err(f"{p}.file", f"no such file relative to spec: {sk['file']}")

    # mcp_servers
    mcp = data.get("mcp_servers", {})
    if not isinstance(mcp, dict):
        err("$.mcp_servers", "must be an object")
        mcp = {}
    for sname, srv in mcp.items():
        p = f"$.mcp_servers.{sname}"
        if not isinstance(srv, dict):
            err(p, "must be an object")
            continue
        has_cmd = bool(srv.get("command"))
        has_url = bool(srv.get("url"))
        if has_cmd and has_url:
            err(p, "stdio (command) or remote (url), not both")
        elif has_cmd:
            if not isinstance(srv["command"], str):
                err(f"{p}.command", "must be a string")
            if "args" in srv and not (
                isinstance(srv["args"], list)
                and all(isinstance(a, str) for a in srv["args"])
            ):
                err(f"{p}.args", "must be an array of strings")
            if "env" in srv and not _is_str_map(srv["env"]):
                err(f"{p}.env", "must be an object of string values")
        elif has_url:
            if not isinstance(srv["url"], str):
                err(f"{p}.url", "must be a string")
            if "headers" in srv and not _is_str_map(srv["headers"]):
                err(f"{p}.headers", "must be an object of string values")
        else:
            err(p, "one of 'command' (stdio) or 'url' (remote) is required")

    # plugins
    plugins = data.get("plugins", [])
    if not isinstance(plugins, list):
        err("$.plugins", "must be an array")
        plugins = []
    for i, pl in enumerate(plugins):
        p = f"$.plugins[{i}]"
        if not isinstance(pl, dict):
            err(p, "must be an object")
            continue
        if not isinstance(pl.get("name"), str) or not pl["name"].strip():
            err(f"{p}.name", "required; non-empty string")
        for j, rt in enumerate(pl.get("runtimes", [])):
            if rt not in KNOWN_RUNTIMES:
                err(f"{p}.runtimes[{j}]", f"unknown runtime {rt!r}")
        if "config" in pl and not isinstance(pl["config"], dict):
            err(f"{p}.config", "must be an object")

    # trigger
    trigger = data.get("trigger", {"type": "manual"})
    if not isinstance(trigger, dict) or trigger.get("type") not in (
        "manual",
        "cron",
    ):
        err("$.trigger", 'must be {"type": "manual"|"cron", "schedule"?}')
        trigger = {"type": "manual"}
    elif trigger["type"] == "cron":
        sched = trigger.get("schedule")
        if not isinstance(sched, str) or not _CRON_RE.match(sched):
            err(
                "$.trigger.schedule",
                "required when type is cron; 5-field cron expression",
            )
    elif "schedule" in trigger:
        err("$.trigger.schedule", "only meaningful when type is cron")

    # guardrails
    guardrails = data.get("guardrails", {})
    if not isinstance(guardrails, dict):
        err("$.guardrails", "must be an object")
        guardrails = {}
    else:
        ase = guardrails.get("allowed_side_effects", [])
        if not (isinstance(ase, list) and all(isinstance(a, str) for a in ase)):
            err("$.guardrails.allowed_side_effects", "must be an array of strings")
        if "stop_file" in guardrails and not isinstance(
            guardrails["stop_file"], str
        ):
            err("$.guardrails.stop_file", "must be a string path")
        receipt = guardrails.get("receipt", {})
        if not isinstance(receipt, dict):
            err("$.guardrails.receipt", "must be an object")
        elif "path" in receipt and not isinstance(receipt["path"], str):
            err("$.guardrails.receipt.path", "must be a string path")
        if "llm_optional" in guardrails and not isinstance(
            guardrails["llm_optional"], bool
        ):
            err("$.guardrails.llm_optional", "must be a boolean")
        ma = guardrails.get("max_actions", 3)
        if not isinstance(ma, int) or isinstance(ma, bool) or ma < 0:
            err("$.guardrails.max_actions", "must be an integer >= 0")

    if problems:
        raise SpecError(problems)

    return Spec(
        name=data["name"],
        purpose=data["purpose"],
        model=data["model"],
        runtimes=list(data["runtimes"]),
        description=data.get("description", ""),
        system_prompt=data.get("system_prompt"),
        skills=list(data.get("skills", [])),
        mcp_servers=dict(data.get("mcp_servers", {})),
        plugins=list(data.get("plugins", [])),
        trigger=dict(data.get("trigger", {"type": "manual"})),
        guardrails=dict(data.get("guardrails", {})),
        spec_dir=spec_dir,
    )


def load(path: str | Path) -> Spec:
    """Load and validate a spec file."""
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SpecError([f"{p}: invalid JSON: {e}"]) from e
    return validate(data, p.resolve().parent)
