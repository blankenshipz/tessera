"""Static guard: function-level relative imports must name things that exist.

A lazy ``from .mod import name`` inside a function only fails when that call
path runs, so a rename of ``name`` can ship unnoticed. This test parses every
module under ``src/tessera_control`` and checks each nested relative import
against the names the target module defines, without importing anything.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
PACKAGE_ROOT = SRC_ROOT / "tessera_control"

_FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _module_file(parts: tuple[str, ...]) -> Path | None:
    base = SRC_ROOT.joinpath(*parts)
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


def _target_names(node: ast.AST, names: set[str], flags: dict[str, bool]) -> None:
    """Collect names bound by module-level statements (recursing into if/try)."""
    if isinstance(node, (*_FUNC_TYPES, ast.ClassDef)):
        names.add(node.name)
        if node.name == "__getattr__":
            flags["dynamic"] = True
    elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
    elif isinstance(node, ast.Import):
        for alias in node.names:
            names.add(alias.asname or alias.name.split(".")[0])
    elif isinstance(node, ast.ImportFrom):
        for alias in node.names:
            if alias.name == "*":
                flags["dynamic"] = True
            else:
                names.add(alias.asname or alias.name)
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        for sub in ast.walk(node.target):
            if isinstance(sub, ast.Name):
                names.add(sub.id)
        for child in [*node.body, *node.orelse]:
            _target_names(child, names, flags)
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            if item.optional_vars is not None:
                for sub in ast.walk(item.optional_vars):
                    if isinstance(sub, ast.Name):
                        names.add(sub.id)
        for child in node.body:
            _target_names(child, names, flags)
    elif isinstance(node, (ast.If, ast.While)):
        for child in [*node.body, *node.orelse]:
            _target_names(child, names, flags)
    elif isinstance(node, ast.Try) or node.__class__.__name__ == "TryStar":
        for child in [
            *node.body,
            *node.orelse,
            *node.finalbody,
            *(c for h in node.handlers for c in h.body),
        ]:
            _target_names(child, names, flags)
    elif isinstance(node, ast.Match):
        for case in node.cases:
            for sub in ast.walk(case.pattern):
                for attr in ("name", "rest"):
                    value = getattr(sub, attr, None)
                    if isinstance(value, str):
                        names.add(value)
            for child in case.body:
                _target_names(child, names, flags)


_defined_cache: dict[Path, tuple[set[str], bool]] = {}


def _defined(path: Path) -> tuple[set[str], bool]:
    if path not in _defined_cache:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names: set[str] = set()
        flags = {"dynamic": False}
        for stmt in tree.body:
            _target_names(stmt, names, flags)
        _defined_cache[path] = (names, flags["dynamic"])
    return _defined_cache[path]


def _nested_relative_imports(tree: ast.AST):
    """Yield ImportFrom nodes with level >= 1 that are not at module level."""

    def visit(node: ast.AST, nested: bool):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ImportFrom) and child.level >= 1 and nested:
                yield child
            inner = nested or isinstance(child, (*_FUNC_TYPES, ast.ClassDef))
            yield from visit(child, inner)

    yield from visit(tree, False)


def test_function_level_relative_imports_name_existing_objects() -> None:
    problems: list[str] = []
    checked = 0
    skipped = 0
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        rel_parts = path.relative_to(SRC_ROOT).with_suffix("").parts
        package = rel_parts[:-1]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in _nested_relative_imports(tree):
            where = f"{path.relative_to(SRC_ROOT.parent)}:{node.lineno}"
            keep = len(package) - (node.level - 1)
            if keep < 1:
                problems.append(f"{where}: relative import goes above the package")
                continue
            target = (*package[:keep], *(node.module.split(".") if node.module else ()))
            target_file = _module_file(target)
            if target_file is None:
                problems.append(f"{where}: module {'.'.join(target)} does not exist")
                continue
            names, dynamic = _defined(target_file)
            for alias in node.names:
                if alias.name == "*" or dynamic:
                    skipped += 1
                    continue
                checked += 1
                if alias.name in names:
                    continue
                if target_file.name == "__init__.py" and _module_file((*target, alias.name)):
                    continue
                problems.append(f"{where}: {'.'.join(target)} has no name {alias.name!r}")
    assert checked > 0, "no nested relative imports found; resolver is broken"
    assert not problems, (
        f"{len(problems)} stale lazy import(s) (checked {checked}, skipped {skipped}):\n"
        + "\n".join(problems)
    )
