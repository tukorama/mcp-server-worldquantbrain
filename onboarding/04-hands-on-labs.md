# 04 — Hands-On Labs

Every lab below runs offline on your workstation with `uv`—no live WorldQuant BRAIN credentials or network calls needed.

---

## Lab 1 — CLI Introspection & Security Gate Check

Inspect the default tool surface (`24` tools, `submit_alpha` hidden), then enable submission explicitly in a subshell command:

```bash
uv run mcp-server-worldquantbrain tools
WQB_ALLOW_SUBMIT=true uv run mcp-server-worldquantbrain tools
```

Notice `submit_alpha` appears only in the second output.

---

## Lab 2 — Architectural Boundaries & Repository Hygiene

Inspect and execute [tests/test_coverage.py](../tests/test_coverage.py):

```bash
uv run pytest tests/test_coverage.py -v
```

This runs:
- `test_strict_architectural_layer_dag` — parses AST imports across all source modules and verifies the 6-layer dependency ordering.
- `test_gitleaks_config_and_custom_patterns_pass_repo_files` — compiles custom patterns from [.gitleaks.toml](../.gitleaks.toml) and checks source, tests, docs, workflows, and manifests.

---

## Lab 3 — Simulation Ledger & Stateful Property Tests

Run [tests/unit/test_invariants.py](../tests/unit/test_invariants.py) with Hypothesis statistics enabled:

```bash
uv run pytest tests/unit/test_invariants.py -v --hypothesis-show-statistics
```

This exercises:
- Whitespace-invariant FASTEXPR fingerprinting across randomized whitespace and key orderings.
- Path-segment URL escaping (`segment()`) and pagination boundary guarantees (`MAX_OFFSET = 10_000`).
- Secret, email, base64, and HTML redaction invariants.
- `LedgerStateMachine` stateful sequences verifying that statuses outside the alphabet are mapped onto it, non-erasing `alpha_id`, and POSIX `0o600` database file permissions.

---

## Lab 4 — Full Verification & Distribution Build

Run the exact quality and build pipeline enforced by CI:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -v
uv run pip-audit
uv build --no-sources
```

Then run the wheel smoke test and clean up temporary build artifacts:

```bash
uvx --from dist/*.whl mcp-server-worldquantbrain tools
rm -rf dist
```
