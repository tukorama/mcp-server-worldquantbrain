"""AST layer DAG and Gitleaks rule self-verification for mcp-server-worldquantbrain."""

import ast
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_SCAN_DIRS = ("src", "tests", "examples", "onboarding", ".github")
_SCAN_ROOT_FILES = (
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "pyproject.toml",
    "server.json",
)

_MODULE_LAYER: dict[str, int] = {
    "errors": 0,
    "redaction": 0,
    "logging_setup": 0,
    "prompts": 0,
    "knowledge": 0,
    "config": 1,
    "models": 1,
    "__init__": 2,
    "client": 2,
    "sim_manager": 3,
    "ops": 4,
    "http_app": 4,
    "resources": 5,
    "tools": 5,
    "server": 5,
    "cli": 5,
    "integrations": 5,
    "__main__": 5,
}


def _is_type_checking_guard(node: ast.If) -> bool:
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _runtime_imports(stmts: list[ast.stmt]) -> list[str]:
    targets: list[str] = []
    for stmt in stmts:
        if isinstance(stmt, ast.If) and _is_type_checking_guard(stmt):
            targets.extend(_runtime_imports(stmt.orelse))
            continue
        if isinstance(stmt, ast.Import):
            targets.extend(alias.name for alias in stmt.names)
        elif isinstance(stmt, ast.ImportFrom) and stmt.level == 0 and stmt.module:
            targets.append(stmt.module)
        for child in ast.iter_child_nodes(stmt):
            if isinstance(child, ast.stmt):
                targets.extend(_runtime_imports([child]))
            elif isinstance(child, ast.ExceptHandler):
                targets.extend(_runtime_imports(child.body))
    return targets


def _package_top_name(module_name: str) -> str | None:
    prefix = "mcp_server_worldquantbrain."
    if not module_name.startswith(prefix):
        return None
    return module_name[len(prefix) :].split(".", 1)[0]


def test_strict_architectural_layer_dag() -> None:
    pkg_dir = ROOT / "src" / "mcp_server_worldquantbrain"
    violations: list[str] = []
    for py_file in sorted(pkg_dir.rglob("*.py")):
        rel = py_file.relative_to(pkg_dir)
        src_mod = rel.stem if len(rel.parts) == 1 else rel.parts[0]
        assert src_mod in _MODULE_LAYER, f"Unregistered architectural module: {rel}"
        src_layer = _MODULE_LAYER[src_mod]
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(rel))
        for imp in _runtime_imports(tree.body):
            dst_mod = _package_top_name(imp)
            if dst_mod is None or dst_mod == src_mod:
                continue
            assert dst_mod in _MODULE_LAYER, f"Unknown internal import {imp} in {rel}"
            dst_layer = _MODULE_LAYER[dst_mod]
            if dst_layer > src_layer:
                violations.append(
                    f"{rel} (layer {src_layer}) imports {dst_mod} (layer {dst_layer})"
                )
    assert violations == [], f"Architectural layer DAG violations: {violations}"


def test_gitleaks_config_and_custom_patterns_pass_repo_files() -> None:
    cfg = tomllib.loads((ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))
    rules: dict[str, dict[str, str]] = {str(rule["id"]): dict(rule) for rule in list(cfg["rules"])}
    assert {
        "wqb-password-literal",
        "wqb-bearer-token",
        "wqb-session-cookie",
    } <= set(rules)

    compiled_rules = [(rid, re.compile(rule["regex"])) for rid, rule in rules.items()]
    allow_raw: list[str] = [str(pat) for pat in list(cfg["allowlist"]["regexes"])]
    allow_patterns = [re.compile(pat) for pat in allow_raw]

    targets: list[Path] = [ROOT / name for name in _SCAN_ROOT_FILES if (ROOT / name).exists()]
    for folder in _SCAN_DIRS:
        base = ROOT / folder
        if base.exists():
            targets.extend(
                path
                for path in base.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts
            )

    findings: list[str] = []
    for file_path in targets:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
        for rid, pattern in compiled_rules:
            for match in pattern.finditer(text):
                matched_text = match.group(1) if match.groups() else match.group(0)
                if any(
                    allow.search(match.group(0)) or allow.search(matched_text)
                    for allow in allow_patterns
                ):
                    continue
                findings.append(f"{file_path.relative_to(ROOT)}: matched {rid} -> {match.group(0)}")
    assert findings == [], f"Unexpected secret-like strings found: {findings}"
