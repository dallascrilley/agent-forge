# Contributing

Small, focused PRs. The generator core is stdlib-only Python — do not add
runtime dependencies. Dev dependencies (pytest, jsonschema) are fine.

## Ground rules

- The spec is the contract. Changes to spec fields need: schema update,
  validator update, `docs/spec-v1.md` update, and a test — in one PR.
- Adapter output is golden-file tested. After an intended change, run
  `python3 tests/bless_golden.py` and review the golden diff before
  committing.
- Guardrails enforcement is load-bearing. PRs that weaken it (fewer call
  sites, opt-out defaults) need an explicit rationale in the PR description.
- Keep generated output free of machine-specific paths and personal facts;
  `tests/test_no_private_facts.py` enforces this.

## Run the tests

```bash
pip install -r requirements-dev.txt
python3 -m pytest -q
ruff check forge tests
```

## Adding a runtime

See `docs/adapters.md`.
