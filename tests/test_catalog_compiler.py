"""Audited YAML catalogs compile deterministically and fail closed."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from forge.catalog import CatalogCompileError, compile_catalog
from forge.orchestrator.canonical import verify_content_identity

REPO = Path(__file__).resolve().parent.parent
CATALOG = REPO / "catalog"


def _copy_catalog(tmp_path):
    target = tmp_path / "catalog"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(CATALOG, target)
    (target / "catalog.lock.json").unlink(missing_ok=True)
    return target


def _resource(catalog, name="repo-scout"):
    return catalog / "resources" / f"{name}.yaml"


def _replace(path, old, new):
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


def _problems(catalog, **kwargs):
    with pytest.raises(CatalogCompileError) as exc:
        compile_catalog(catalog, catalog / "catalog.lock.json", **kwargs)
    return exc.value.problems


def test_representative_catalog_recompiles_byte_identically(tmp_path):
    catalog = _copy_catalog(tmp_path)
    output = catalog / "catalog.lock.json"
    lock = compile_catalog(catalog, output)
    first = output.read_bytes()
    compile_catalog(catalog, output)
    assert output.read_bytes() == first == (CATALOG / "catalog.lock.json").read_bytes()
    assert verify_content_identity("catalog-lock", lock.to_dict())
    assert [recipe["id"] for recipe in lock.to_dict()["recipes"]] == [
        "change-reviewer",
        "implementation-worker",
        "repo-scout",
        "web-researcher",
    ]


def test_separate_compiler_processes_emit_identical_lockfiles(tmp_path):
    outputs = []
    for index in range(2):
        catalog = _copy_catalog(tmp_path / str(index))
        output = catalog / "lock.json"
        env = dict(os.environ, PYTHONHASHSEED=str(index + 10))
        subprocess.run(
            [
                sys.executable,
                "-m",
                "forge.catalog",
                "compile",
                str(catalog),
                "--output",
                str(output),
            ],
            cwd=REPO,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        outputs.append(output.read_bytes())
    assert outputs[0] == outputs[1]


def test_missing_yaml_dependency_has_stable_error_and_preserves_output(tmp_path):
    catalog = _copy_catalog(tmp_path)
    output = catalog / "lock.json"
    output.write_bytes(b"previous-valid-lock")
    process = subprocess.run(
        [
            sys.executable,
            "-S",
            "-m",
            "forge.catalog",
            "compile",
            str(catalog),
            "--output",
            str(output),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert process.returncode == 2
    assert process.stdout == ""
    assert process.stderr.strip() == (
        "catalog compilation requires PyYAML; install it with: "
        "python3 -m pip install -r requirements-catalog.txt"
    )
    assert output.read_bytes() == b"previous-valid-lock"


@pytest.mark.parametrize("escape", ["../outside.md", "/tmp/outside.md"])
def test_path_escape_fails_closed(tmp_path, escape):
    catalog = _copy_catalog(tmp_path)
    _replace(_resource(catalog), "path: roles/repo-scout.md", f"path: {escape}")
    assert any("path" in problem and "catalog root" in problem for problem in _problems(catalog))


def test_symlink_escape_fails_closed(tmp_path):
    catalog = _copy_catalog(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    link = catalog / "roles" / "escape.md"
    link.symlink_to(outside)
    _replace(_resource(catalog), "roles/repo-scout.md", "roles/escape.md")
    assert any("catalog root" in problem for problem in _problems(catalog))


def test_missing_resource_fails_closed(tmp_path):
    catalog = _copy_catalog(tmp_path)
    _replace(_resource(catalog), "roles/repo-scout.md", "roles/missing.md")
    assert any("does not exist" in problem for problem in _problems(catalog))


def test_unhashed_resource_fails_closed(tmp_path):
    catalog = _copy_catalog(tmp_path)
    descriptor = _resource(catalog)
    lines = [
        line
        for line in descriptor.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("sha256:")
    ]
    descriptor.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert any("sha256" in problem and "required" in problem for problem in _problems(catalog))


def test_hash_mismatch_preserves_previous_lock(tmp_path):
    catalog = _copy_catalog(tmp_path)
    output = catalog / "catalog.lock.json"
    output.write_bytes(b"previous-valid-lock")
    descriptor = _resource(catalog)
    _replace(
        descriptor,
        "sha256:d0652cacbecfb2681d9d5e2df6b279831e1cb448cfde495408e4597a30e20786",
        "sha256:" + "0" * 64,
    )
    assert any("hash mismatch" in problem for problem in _problems(catalog))
    assert output.read_bytes() == b"previous-valid-lock"


def test_duplicate_exclusive_provider_fails_closed(tmp_path):
    catalog = _copy_catalog(tmp_path)
    duplicate = catalog / "resources" / "second-search.yaml"
    text = _resource(catalog).read_text(encoding="utf-8")
    duplicate.write_text(text.replace("role.repo-scout", "role.second-search"), encoding="utf-8")
    assert any("exclusive capability" in problem for problem in _problems(catalog))


def test_literal_credential_and_unknown_fields_fail_closed(tmp_path):
    catalog = _copy_catalog(tmp_path)
    descriptor = _resource(catalog)
    descriptor.write_text(
        descriptor.read_text(encoding="utf-8") + "env:\n  TOKEN: literal-secret\n",
        encoding="utf-8",
    )
    problems = _problems(catalog)
    assert any("env" in problem and "unknown field" in problem for problem in problems)
    assert not (catalog / "catalog.lock.json").exists()


def test_credential_field_accepts_names_not_literal_values(tmp_path):
    catalog = _copy_catalog(tmp_path)
    _replace(
        _resource(catalog),
        "credentialEnv: []",
        "credentialEnv: [OPENAI_API_KEY=literal-secret]",
    )
    assert any("credentialEnv" in problem and "must match" in problem for problem in _problems(catalog))


@pytest.mark.parametrize(
    "source",
    [
        "https://user:literal-secret@example.invalid/repo",
        "https://example.invalid/repo?access_token=literal-secret",
        "catalog:roles/repo-scout.md?token=literal-secret",
    ],
)
def test_credential_bearing_source_fails_closed_and_preserves_output(tmp_path, source):
    catalog = _copy_catalog(tmp_path)
    output = catalog / "catalog.lock.json"
    output.write_bytes(b"previous-valid-lock")
    _replace(_resource(catalog), "catalog:roles/repo-scout.md", source)

    problems = _problems(catalog)
    assert any("source" in problem and "credential" in problem for problem in problems)
    assert output.read_bytes() == b"previous-valid-lock"


def test_incompatible_pi_version_fails_closed(tmp_path):
    catalog = _copy_catalog(tmp_path)
    assert any("incompatible with Pi 0.85.0" in problem for problem in _problems(catalog, pi_version="0.85.0"))


def test_resource_code_is_hashed_but_never_executed(tmp_path):
    catalog = _copy_catalog(tmp_path)
    sentinel = tmp_path / "executed"
    code = f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('bad')\n"
    resource_path = catalog / "roles" / "repo-scout.py"
    resource_path.write_text(code, encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(resource_path.read_bytes()).hexdigest()
    descriptor = _resource(catalog)
    _replace(descriptor, "roles/repo-scout.md", "roles/repo-scout.py")
    lines = descriptor.read_text(encoding="utf-8").splitlines()
    lines = [
        f"  sha256: {digest}" if line.lstrip().startswith("sha256:") else line
        for line in lines
    ]
    descriptor.write_text("\n".join(lines) + "\n", encoding="utf-8")
    compile_catalog(catalog, catalog / "catalog.lock.json")
    assert not sentinel.exists()


def test_hostile_yaml_constructor_and_duplicate_keys_are_rejected(tmp_path):
    catalog = _copy_catalog(tmp_path)
    (catalog / "capabilities.yaml").write_text(
        "!!python/object/apply:os.system ['touch hostile']\n", encoding="utf-8"
    )
    assert any("invalid YAML" in problem for problem in _problems(catalog))

    catalog = _copy_catalog(tmp_path / "duplicate")
    descriptor = _resource(catalog)
    descriptor.write_text(
        descriptor.read_text(encoding="utf-8") + "  trust: audited\n",
        encoding="utf-8",
    )
    assert any("duplicate mapping key" in problem for problem in _problems(catalog))


def test_compiled_lock_contains_no_secret_values(tmp_path):
    catalog = _copy_catalog(tmp_path)
    output = catalog / "catalog.lock.json"
    compile_catalog(catalog, output)
    data = output.read_text(encoding="utf-8")
    assert "literal-secret" not in data
    lock = json.loads(data)
    assert all(
        name == name.upper()
        for resource in lock["resources"]
        for name in resource["credentialEnv"]
    )
