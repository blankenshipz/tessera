"""Tests for dev/focused_tests.py (changed-module test selection)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "dev" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclass-free, but keeps tracebacks readable
    spec.loader.exec_module(module)
    return module


focused = load("focused_tests")


def write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def tree(tmp_path):
    write(tmp_path / "src/pkg/a.py", "def f(): ...\n")
    write(tmp_path / "src/pkg/b.py", "from .a import f\n")
    write(tmp_path / "src/pkg/c.py", "VALUE = 1\n")
    write(tmp_path / "src/pkg/d.py", "from pkg.c import VALUE\n")
    write(tmp_path / "tests/test_a.py", "")
    write(tmp_path / "tests/test_uses_b.py", "from pkg.b import f\n")
    write(tmp_path / "tests/test_uses_pkg_import.py", "from pkg import b\n")
    write(tmp_path / "tests/test_unrelated.py", "import os\n")
    write(tmp_path / "tests/test_docs_links.py", "")
    write(tmp_path / "tests/test_platform_validation_harness.py", "")
    return tmp_path


def test_module_key_only_for_flat_source_modules():
    assert focused.module_key("src/tessera_worker/service.py") == ("tessera_worker", "service")
    assert focused.module_key("src/tessera_worker/sub/x.py") is None
    assert focused.module_key("tests/test_x.py") is None
    assert focused.module_key("docs/x.md") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("from tessera_worker.service import X\n", True),
        ("import tessera_worker.service as s\n", True),
        ("from tessera_worker import service\n", True),
        ("from tessera_worker import cli, service\n", True),
        ("from tessera_worker.services import X\n", False),
        ("from tessera_worker import services\n", False),
        ("from other.service import X\n", False),
        ("import os\n", False),
    ],
)
def test_import_detection(text, expected):
    assert focused.imports(text, "tessera_worker", "service") is expected


def test_a_changed_module_selects_its_tests_and_its_direct_dependents_tests(tree):
    chosen = focused.select_tests(tree, ["src/pkg/a.py"])
    # test_a by name; b imports a directly, so b's importers (two styles) come along.
    assert chosen == [
        "tests/test_a.py",
        "tests/test_uses_b.py",
        "tests/test_uses_pkg_import.py",
    ]


def test_only_one_level_of_dependents_and_unrelated_tests_stay_out(tree):
    # d imports c; nothing imports d, so only tests naming/importing c or d are chosen.
    assert focused.select_tests(tree, ["src/pkg/c.py"]) == []
    write(tree / "tests/test_d.py", "")
    assert focused.select_tests(tree, ["src/pkg/c.py"]) == ["tests/test_d.py"]


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        ("test_service", True),
        ("test_service_receipts", True),
        ("test_services", False),
        ("test_servicex", False),
        ("test_docs_links", False),
        ("service_test", False),
    ],
)
def test_test_file_naming_rule(stem, expected):
    assert focused.named_for(stem, "service") is expected


def test_changed_tests_docs_pyproject_and_harness_select_their_own_tests(tree):
    assert focused.select_tests(tree, ["tests/test_unrelated.py"]) == ["tests/test_unrelated.py"]
    for change in ("docs/x.md", "README.md", "pyproject.toml"):
        assert focused.select_tests(tree, [change]) == ["tests/test_docs_links.py"]
    assert focused.select_tests(tree, ["scripts/platform_validation/real_run.py"]) == [
        "tests/test_platform_validation_harness.py"
    ]
    # A deleted test or an unknown path selects nothing and never the whole suite.
    assert focused.select_tests(tree, ["tests/test_gone.py", "Makefile"]) == []


def test_git_changed_includes_committed_uncommitted_and_untracked_files(tmp_path):
    def git(*args):
        return subprocess.run(
            [
                "git",
                "-c", "user.name=t",
                "-c", "user.email=t@example.invalid",
                "-c", "commit.gpgsign=false",
                "-c", "tag.gpgsign=false",
                *args,
            ],
            cwd=tmp_path, check=True, capture_output=True, text=True,
        )  # fmt: skip

    git("init", "-q", "-b", "main")
    write(tmp_path / "src/pkg/a.py", "x = 1\n")
    write(tmp_path / "src/pkg/z.py", "z = 1\n")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    git("checkout", "-q", "-b", "feature")
    write(tmp_path / "src/pkg/a.py", "x = 2\n")
    git("commit", "-q", "-am", "committed change")
    write(tmp_path / "src/pkg/z.py", "z = 2\n")  # uncommitted
    write(tmp_path / "tests/test_new.py", "")  # untracked
    assert focused.git_changed(tmp_path, "main") == [
        "src/pkg/a.py",
        "src/pkg/z.py",
        "tests/test_new.py",
    ]


def test_nothing_selected_and_nothing_unmapped_runs_nothing_and_succeeds(tree, capsys, monkeypatch):
    monkeypatch.setattr(focused, "git_changed", lambda root, base: ["tests/test_deleted.py"])
    assert focused.main(["--root", str(tree)]) == 0
    assert "nothing run" in capsys.readouterr().out


def test_unmapped_changes_with_no_selection_stop_with_status_2_and_name_the_files(
    tree, capsys, monkeypatch
):
    monkeypatch.setattr(
        focused, "git_changed", lambda root, base: ["Makefile", "src/pkg/data.json"]
    )
    assert focused.main(["--root", str(tree)]) == 2
    err = capsys.readouterr().err
    assert "no test maps to: Makefile, src/pkg/data.json" in err
    assert "run the full suite" in err


def test_unmapped_changes_alongside_a_selection_warn_but_still_select(tree, capsys, monkeypatch):
    monkeypatch.setattr(focused, "git_changed", lambda root, base: ["Makefile", "src/pkg/a.py"])
    assert focused.main(["--root", str(tree), "--list"]) == 0
    captured = capsys.readouterr()
    assert "no test maps to: Makefile" in captured.err
    assert "tests/test_a.py" in captured.out


def test_list_prints_the_selection_without_running(tree, capsys, monkeypatch):
    monkeypatch.setattr(focused, "git_changed", lambda root, base: ["src/pkg/a.py"])
    assert focused.main(["--root", str(tree), "--list"]) == 0
    assert capsys.readouterr().out.split() == [
        "tests/test_a.py",
        "tests/test_uses_b.py",
        "tests/test_uses_pkg_import.py",
    ]


@pytest.fixture
def wide(tmp_path):
    """Subpackages, __init__ files, a second package, helpers, conftest, dev, scripts, deploy."""
    write(tmp_path / "src/pkg/__init__.py", "")
    write(tmp_path / "src/pkg/sub/__init__.py", "")
    write(tmp_path / "src/pkg/sub/mod.py", "def f(): ...\n")
    write(tmp_path / "src/pkg/user.py", "from pkg.sub.mod import f\n")
    write(tmp_path / "src/other/__init__.py", "")
    write(tmp_path / "src/other/thing.py", "from pkg.sub import mod\n")
    write(tmp_path / "src/pkg/data.json", "{}")
    write(tmp_path / "tests/test_mod.py", "")
    write(tmp_path / "tests/test_thing.py", "")
    write(tmp_path / "tests/test_user_flow.py", "")
    write(tmp_path / "tests/test_imports_sub.py", "import pkg.sub\n")
    write(tmp_path / "tests/test_imports_root.py", "import pkg\n")
    write(tmp_path / "tests/test_tooling.py", "load('some_tool')\nrun('dev/other_tool.py')\n")
    write(tmp_path / "tests/test_deploys.py", "check('deploy/worker')\n")
    write(tmp_path / "tests/test_docs_links.py", "")
    write(tmp_path / "tests/helper_fixtures.py", "")
    write(tmp_path / "tests/test_uses_helper.py", "from helper_fixtures import x\n")
    write(tmp_path / "tests/nested/test_inner.py", "")
    write(tmp_path / "tests/nested/test_other_inner.py", "")
    write(tmp_path / "dev/some_tool.py", "")
    write(tmp_path / "dev/orphan_tool.py", "")
    write(tmp_path / "dev/other_tool.py", "")
    write(tmp_path / "scripts/check_thing.py", "")
    write(tmp_path / "deploy/worker/unit.service", "")
    write(tmp_path / "deploy/lonely/unit.service", "")
    return tmp_path


def test_source_module_handles_subpackages_and_init_files():
    assert focused.source_module("src/pkg/sub/mod.py") == ("pkg", "sub.mod")
    assert focused.source_module("src/pkg/sub/__init__.py") == ("pkg", "sub")
    assert focused.source_module("src/pkg/__init__.py") == ("pkg", "")
    assert focused.source_module("src/pkg/data.json") is None
    assert focused.source_module("tests/test_x.py") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("from pkg.sub.mod import f\n", True),
        ("import pkg.sub.mod as m\n", True),
        ("from pkg.sub import mod\n", True),
        ("from pkg.sub import other, mod\n", True),
        ("from pkg.sub.mod2 import f\n", False),
        ("from pkg import mod\n", False),
        ("from pkg.sub import modern\n", False),
    ],
)
def test_dotted_import_detection(text, expected):
    assert focused.imports(text, "pkg", "sub.mod") is expected


def test_a_subpackage_module_selects_its_test_importers_and_cross_package_dependents(wide):
    chosen = focused.select_tests(wide, ["src/pkg/sub/mod.py"])
    # test_mod by name; user.py (same package) and other/thing.py (another package) import it,
    # so tests named for them come along.
    assert chosen == ["tests/test_mod.py", "tests/test_thing.py", "tests/test_user_flow.py"]


def test_init_files_stand_for_their_package_and_the_root_is_reported_as_broad(wide):
    sub = focused.analyze(wide, ["src/pkg/sub/__init__.py"])
    assert "tests/test_imports_sub.py" in sub.tests and sub.broad == []
    root = focused.analyze(wide, ["src/pkg/__init__.py"])
    assert {"tests/test_imports_root.py", "tests/test_imports_sub.py"} <= root.tests
    assert root.broad == ["src/pkg/__init__.py"]


def test_conftest_selects_everything_below_it_and_the_root_one_is_broad(wide):
    write(wide / "tests/nested/conftest.py", "")
    nested = focused.analyze(wide, ["tests/nested/conftest.py"])
    assert nested.files == ["tests/nested/test_inner.py", "tests/nested/test_other_inner.py"]
    assert nested.broad == []
    write(wide / "tests/conftest.py", "")
    top = focused.analyze(wide, ["tests/conftest.py"])
    assert "tests/test_mod.py" in top.tests and top.broad == ["tests/conftest.py"]


def test_helper_modules_dev_scripts_and_deploy_map_to_tests_that_mention_them(wide):
    assert focused.analyze(wide, ["tests/helper_fixtures.py"]).files == [
        "tests/test_uses_helper.py"
    ]
    assert focused.analyze(wide, ["dev/some_tool.py"]).files == ["tests/test_tooling.py"]
    assert focused.analyze(wide, ["dev/other_tool.py"]).files == ["tests/test_tooling.py"]
    deploy = focused.analyze(wide, ["deploy/worker/unit.service"])
    assert deploy.files == ["tests/test_deploys.py", "tests/test_docs_links.py"]
    assert deploy.unmapped == []


def test_files_without_a_mapped_test_are_reported_not_dropped(wide):
    selection = focused.analyze(
        wide,
        [
            "Makefile",
            ".gitignore",
            "src/pkg/data.json",
            "dev/orphan_tool.py",
            "scripts/check_thing.py",
            "tests/data/blob.bin",
            "src/pkg/sub/mod.py",
        ],
    )
    assert selection.unmapped == [
        "Makefile",
        ".gitignore",
        "src/pkg/data.json",
        "dev/orphan_tool.py",
        "scripts/check_thing.py",
        "tests/data/blob.bin",
    ]
    assert "tests/test_mod.py" in selection.tests  # the mapped change still selects


def test_a_source_module_nothing_names_or_imports_is_unmapped(wide):
    write(wide / "src/pkg/lonely.py", "")
    assert focused.analyze(wide, ["src/pkg/lonely.py"]).unmapped == ["src/pkg/lonely.py"]
