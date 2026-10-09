import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
from build_release_candidate import inputs
from check_installed_release import installed_starter_resources
from release_input_parity import verify_wheel
from release_resources import STARTER_FILES, STARTER_INSTALLED, STARTER_SOURCE, starter_resource_map

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fixture_root(tmp_path):
    root = tmp_path / "fixture"
    root.mkdir()
    for name in (
        "README.md",
        "DESIGN.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "pyproject.toml",
        "uv.lock",
    ):
        (root / name).write_text("synthetic package input")
    directory = root / STARTER_SOURCE
    directory.mkdir(parents=True)
    for name in STARTER_FILES:
        (directory / name).write_text("synthetic resource: " + name)
    return root


def test_config_exact_resource_contract():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert set(config["tool"]["setuptools"]["data-files"][STARTER_INSTALLED]) == {
        f"{STARTER_SOURCE}/{name}" for name in STARTER_FILES
    }
    manifest = (ROOT / "MANIFEST.in").read_text()
    assert f"{STARTER_SOURCE}/requirements.lock" in manifest
    assert f"{STARTER_SOURCE}/Dockerfile.dockerignore" in manifest


def test_synthetic_source_includes_lock_and_dockerignore_only_explicit_path(fixture_root):
    other = fixture_root / "examples/other"
    other.mkdir()
    (other / "requirements.lock").write_text("not admitted by extension")
    (other / "Dockerfile.dockerignore").write_text("not admitted by extension")
    selected = inputs(fixture_root)
    assert {f"{STARTER_SOURCE}/{name}" for name in STARTER_FILES} <= selected.keys()
    assert "examples/other/requirements.lock" not in selected
    assert "examples/other/Dockerfile.dockerignore" not in selected


def test_private_state_excluded_in_synthetic_starter(fixture_root):
    private = fixture_root / STARTER_SOURCE / "credentials/IMAGE.lock.json"
    private.parent.mkdir()
    private.write_text("synthetic denied state")
    assert private.relative_to(fixture_root).as_posix() not in inputs(fixture_root)


@pytest.mark.parametrize("missing", [False, True])
def test_synthetic_wheel_resource_byte_parity(fixture_root, tmp_path, missing):
    mapping = starter_resource_map("0.1.0rc2")
    wheel = tmp_path / "synthetic.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for source, member in mapping.items():
            if not missing or not source.endswith("requirements.lock"):
                archive.writestr(member, (fixture_root / source).read_bytes())
    if missing:
        with pytest.raises(KeyError):
            verify_wheel(fixture_root, wheel, mapping)
    else:
        assert verify_wheel(fixture_root, wheel, mapping)["verified_files"] == 6


@pytest.mark.parametrize("missing", [False, True])
def test_installed_resource_resolution_without_repository(tmp_path, missing):
    directory = tmp_path / STARTER_INSTALLED
    directory.mkdir(parents=True)
    for name in STARTER_FILES:
        if not missing or name != "requirements.lock":
            (directory / name).write_text("synthetic installed fixture")
    if missing:
        with pytest.raises(ValueError, match="missing"):
            installed_starter_resources(tmp_path)
    else:
        resolved = installed_starter_resources(tmp_path)
        assert set(resolved) == STARTER_FILES
        assert all(path.is_relative_to(tmp_path) for path in resolved.values())
