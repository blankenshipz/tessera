"""Keep public CI checks aligned with the declared source, without live effects."""

import ast
import asyncio
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_distribution_checker_requires_the_exact_current_mcp_tool_surface():
    pytest.importorskip("mcp")
    from tessera_mcp.server import create_server

    tree = ast.parse((ROOT / "dev/check_distribution.py").read_text())
    declarations = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "expected_tools"
            for target in node.targets
        )
    ]
    assert len(declarations) == 1
    expected = ast.literal_eval(declarations[0])
    actual = {tool.name for tool in asyncio.run(create_server(None).list_tools())}
    assert actual == expected
    assert len(actual) == 18


def test_docker_context_includes_public_test_inputs_without_runtime_state():
    patterns = (ROOT / ".dockerignore").read_text().splitlines()
    assert patterns[0] == "**"
    for path in ("docs", "deploy", "examples", "release"):
        assert f"!{path}/" in patterns
        assert f"!{path}/**" in patterns
    for path in ("Dockerfile", "Dockerfile.guard", "compose.yaml", "SECURITY.md"):
        assert f"!{path}" in patterns
    for excluded in (".tessera", ".git", ".venv", "node_modules"):
        assert not any(
            pattern.startswith("!") and pattern[1:].split("/", 1)[0] == excluded
            for pattern in patterns
        )
