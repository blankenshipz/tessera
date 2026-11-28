"""Static guard: function-level package imports must name things that exist.

A lazy ``from .mod import name`` (or ``from tessera_control.mod import name``)
inside a function only fails when that call path runs, so a rename of ``name``
can ship unnoticed. This test parses every module under ``src/tessera_control``
and checks each nested relative or absolute package import against the names
the target module defines, without importing anything.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
PACKAGE_NAME = "tessera_control"
PACKAGE_ROOT = SRC_ROOT / PACKAGE_NAME

_FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _module_file(src_root: Path, parts: tuple[str, ...]) -> Path | None:
    base = src_root.joinpath(*parts)
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


def _scope_walrus_names(node: ast.AST, names: set[str]) -> None:
    """Collect ``:=`` targets bound in the module scope (comprehensions leak them)."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (*_FUNC_TYPES, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(child, ast.NamedExpr) and isinstance(child.target, ast.Name):
            names.add(child.target.id)
        _scope_walrus_names(child, names)


_defined_cache: dict[Path, tuple[set[str], bool]] = {}


def _defined(path: Path) -> tuple[set[str], bool]:
    if path not in _defined_cache:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names: set[str] = set()
        flags = {"dynamic": False}
        for stmt in tree.body:
            _target_names(stmt, names, flags)
        _scope_walrus_names(tree, names)
        # ``global X`` in any function can bind module-level X. ``del`` is not a
        # definition and is deliberately ignored.
        for sub in ast.walk(tree):
            if isinstance(sub, ast.Global):
                names.update(sub.names)
        _defined_cache[path] = (names, flags["dynamic"])
    return _defined_cache[path]


def _nested_package_imports(tree: ast.AST, package_name: str = PACKAGE_NAME):
    """Yield non-module-level ImportFrom nodes: relative, or absolute into the package."""

    def visit(node: ast.AST, nested: bool):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ImportFrom) and nested:
                mod = child.module or ""
                if child.level >= 1 or mod == package_name or mod.startswith(package_name + "."):
                    yield child
            inner = nested or isinstance(child, (*_FUNC_TYPES, ast.ClassDef))
            yield from visit(child, inner)

    yield from visit(tree, False)


def _scan(src_root: Path, package_name: str = PACKAGE_NAME) -> dict:
    """Check every nested package import; return problems and counters."""
    problems: list[str] = []
    counts = {"relative": 0, "absolute": 0, "skipped": 0}
    for path in sorted((src_root / package_name).rglob("*.py")):
        rel_parts = path.relative_to(src_root).with_suffix("").parts
        package = rel_parts[:-1]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in _nested_package_imports(tree, package_name):
            where = f"{path.relative_to(src_root)}:{node.lineno}"
            module = tuple(node.module.split(".")) if node.module else ()
            if node.level >= 1:
                keep = len(package) - (node.level - 1)
                if keep < 1:
                    problems.append(f"{where}: relative import goes above the package")
                    continue
                target = (*package[:keep], *module)
                kind = "relative"
            else:
                target = module
                kind = "absolute"
            target_file = _module_file(src_root, target)
            if target_file is None:
                problems.append(f"{where}: module {'.'.join(target)} does not exist")
                continue
            names, dynamic = _defined(target_file)
            for alias in node.names:
                if alias.name == "*" or dynamic:
                    counts["skipped"] += 1
                    continue
                counts[kind] += 1
                if alias.name in names:
                    continue
                if target_file.name == "__init__.py" and _module_file(
                    src_root, (*target, alias.name)
                ):
                    continue
                problems.append(f"{where}: {'.'.join(target)} has no name {alias.name!r}")
    return {"problems": problems, **counts}


def test_function_level_package_imports_name_existing_objects() -> None:
    _defined_cache.clear()
    result = _scan(SRC_ROOT)
    checked = result["relative"] + result["absolute"]
    problems = result["problems"]
    assert checked > 0, "no nested package imports found; resolver is broken"
    assert not problems, (
        f"{len(problems)} stale lazy import(s) (checked {result['relative']} relative, "
        f"{result['absolute']} absolute, skipped {result['skipped']}):\n" + "\n".join(problems)
    )


def _fake_package(tmp_path: Path) -> Path:
    _defined_cache.clear()
    pkg = tmp_path / "fakepkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "sub").mkdir()
    (pkg / "sub" / "__init__.py").write_text("")
    (pkg / "sub" / "deep.py").write_text("def real(): ...\n")
    (pkg / "dyn.py").write_text(
        "def setup():\n    global LATE\n    LATE = 1\n\n"
        "if (WALRUS := 2):\n    pass\n"
        "ITEMS = [y for x in range(3) if (INNER := x)]\n"
        "gone = 1\ndel gone\n"
    )
    return tmp_path


def test_resolver_accepts_global_walrus_and_absolute_imports(tmp_path: Path) -> None:
    root = _fake_package(tmp_path)
    (root / "fakepkg" / "user.py").write_text(
        "def f():\n"
        "    from .dyn import LATE, WALRUS, INNER\n"
        "    from fakepkg.dyn import LATE as again\n"
        "    from fakepkg.sub.deep import real\n"
        "    from fakepkg import sub\n"
        "    import os\n"
        "    from os import path\n"
    )
    result = _scan(root, "fakepkg")
    assert result["problems"] == []
    assert result["relative"] == 3
    assert result["absolute"] == 3


def test_resolver_detects_missing_absolute_names(tmp_path: Path) -> None:
    root = _fake_package(tmp_path)
    (root / "fakepkg" / "user.py").write_text(
        "def f():\n"
        "    from fakepkg.sub.deep import renamed\n"
        "    from fakepkg.dyn import gone, ITEMS, x\n"
        "    from fakepkg.nowhere import thing\n"
    )
    result = _scan(root, "fakepkg")
    text = "\n".join(result["problems"])
    assert "no name 'renamed'" in text
    assert "no name 'x'" in text
    assert "'gone'" not in text  # still assigned; ``del`` never causes a report
    assert "'ITEMS'" not in text
    assert len(result["problems"]) == 3
    assert "fakepkg.nowhere does not exist" in text
