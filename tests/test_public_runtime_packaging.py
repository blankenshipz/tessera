import json
import shutil
import sys
import tomllib
import types
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dev"))
sys.path.insert(0, str(ROOT / "scripts"))
import check_installed_release  # noqa: E402 -- explicit repository dev tool imports
from build_release_candidate import build, inputs  # noqa: E402
from public_runtime_bundle import FILES, MANIFEST_SHA, NAMESPACE, stage, validate  # noqa: E402
from release_resources import STARTER_FILES, STARTER_INSTALLED  # noqa: E402
from run_release_candidate_gates import GateInputError, source_inventory, source_path  # noqa: E402


@pytest.fixture
def bundle(tmp_path):
    destination = tmp_path / "runtime-v8"
    shutil.copytree(ROOT / "release/opencode-observer/runtime-v8", destination)
    return destination


def test_actual_closed_bundle_schema_and_pins():
    result = validate(ROOT / "release/opencode-observer/runtime-v8")
    assert result["bundle_namespace_sha256"] == NAMESPACE
    assert len(result["files"]) == 14


def test_reviewed_style_repin_keeps_original_source_contract():
    assert MANIFEST_SHA == "45d2645a5bb29a4b2ebe771eb0c9b8802110e4a00423f7baf844d0e10ed9f9a5"
    assert NAMESPACE == "5ee39d0311d8a4b4f0f59298f7591bd8849e772ac69ba629dd3ff4e264c800b7"
    manifest = json.loads((ROOT / "release/opencode-observer/runtime-v8/baseline.json").read_text())
    assert manifest["target_namespace_sha256"] == (
        "c7c70b367eff949c5141979b5de4047f09892d4fdf15573ce9137c5e36fd8062"
    )
    assert len(manifest["baseline_files"]) == 50 and len(manifest["target_files"]) == 79
    assert len(manifest["licenses"]) == 2
    assert manifest["baseline_archive_sha256"] == (
        "67ef0809f9533620ff1382942e3378501ff6c7d2723e4d3d35469d008ecff4dd"
    )
    assert manifest["patch_sha256"] == (
        "97e3929c2c37e4424a2dfecebb7879b2d81e214c522cbbb8c3a6ba6fedbb2c79"
    )
    assert manifest["dependency_lock_sha256"] == (
        "78a78341d30d10fbee051b3440913a72454a4293706051b9b7cbf99d7bd6d42c"
    )
    assert {
        name: manifest["bundle_files"][name]
        for name in ("link_runtime.py", "reconstruct.py", "verify_python.py")
    } == {
        "link_runtime.py": "ef1f82a45d57af3d3af4843119571c3633e9900e4bb821ec61d973a3c87cf77c",
        "reconstruct.py": "c8edd5b9087445524bf00c16981ad7c312c76909f361258c7a418254677a655d",
        "verify_python.py": "7d74bdc7ac16c7ef7a74efb4b2d698c3fa2c8835c96d485a9b4f3db224a74d2b",
    }


@pytest.mark.parametrize(
    "name", ["credentials.json", "node_modules/extra.json", "private/guard.mjs"]
)
def test_extra_file_denied(bundle, name):
    path = bundle / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("synthetic denied")
    with pytest.raises(ValueError, match="inventory"):
        validate(bundle)


@pytest.mark.parametrize("name", ["baseline.tar.gz", "runtime-deps/bun.lock", "LICENSE"])
def test_missing_closed_file_denied(bundle, name):
    (bundle / name).unlink()
    with pytest.raises(ValueError, match="inventory"):
        validate(bundle)


def test_symlink_denied_without_read(bundle):
    (bundle / "LICENSE").unlink()
    (bundle / "LICENSE").symlink_to("nonexistent-cold-path")
    with pytest.raises(ValueError, match="symlink"):
        validate(bundle)


@pytest.mark.parametrize(
    "field,value",
    [("bundle_version", 7), ("unexpected", True), ("runtime_dependencies", {"effect": "unpinned"})],
)
def test_schema_or_reviewed_identity_drift(bundle, field, value):
    path = bundle / "baseline.json"
    manifest = json.loads(path.read_text())
    manifest[field] = value
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        validate(bundle)


def test_dependency_byte_drift_denied(bundle):
    (bundle / "runtime-deps/bun.lock").write_text("not approved lock")
    with pytest.raises(ValueError, match="pin"):
        validate(bundle)


def test_package_data_files_exact_fourteen_and_gate_paths():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    rows = config["tool"]["setuptools"]["data-files"]
    prefix = "release/opencode-observer/runtime-v8/"
    listed = {
        name.removeprefix(prefix)
        for key, values in rows.items()
        if key
        in {"share/tessera/" + prefix.rstrip("/"), "share/tessera/" + prefix + "runtime-deps"}
        for name in values
    }
    assert listed == FILES
    for name in FILES:
        assert source_path(prefix + name) == prefix + name
    with pytest.raises(GateInputError):
        source_path(prefix + "private.mjs")


def test_input_selection_ships_new_modules_and_closed_bundle():
    selected = inputs(ROOT)
    assert {"release/opencode-observer/runtime-v8/" + name for name in FILES} <= selected.keys()
    assert {
        "src/tessera_mcp/runtime_setup.py",
        "src/tessera_mcp/runtime_setup_models.py",
        "docs/runtime-setup.md",
        "docs/runtime-operator-setup.md",
    } <= selected.keys()


def test_gate_inventory_includes_all_public_runtime_files_even_if_ignored(tmp_path, monkeypatch):
    root = tmp_path / "root"
    shutil.copytree(
        ROOT / "release/opencode-observer/runtime-v8", root / "release/opencode-observer/runtime-v8"
    )
    (root / ".git").mkdir()
    import run_release_candidate_gates as gates

    monkeypatch.setattr(
        gates.subprocess, "run", lambda *args, **kwargs: types.SimpleNamespace(stdout=b"")
    )
    assert set(source_inventory(root)) == {
        "release/opencode-observer/runtime-v8/" + name for name in FILES
    }


def test_rc2_missing_native_bundle_fails_before_build(tmp_path):
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        (tmp_path / name).write_text("synthetic fixture")
    with pytest.raises(ValueError, match="bundle missing"):
        build(tmp_path, tmp_path / "candidate", "0.1.0rc2", 1767225600)
    assert not (tmp_path / "candidate").exists()


def test_public_stage_exact_files_and_refuses_overwrite(tmp_path):
    destination = tmp_path / "new-public-stage"
    result = stage(ROOT / "release/opencode-observer/runtime-v8", destination)
    assert result["bundle_namespace_sha256"] == NAMESPACE
    assert len([path for path in destination.rglob("*") if path.is_file()]) == 14
    with pytest.raises(ValueError, match="Fresh ordinary"):
        stage(ROOT / "release/opencode-observer/runtime-v8", destination)


@pytest.mark.parametrize("tamper", [False, True])
def test_installed_byte_checker_isolated_fixture(tmp_path, monkeypatch, tamper):
    site = tmp_path / "site"
    data = tmp_path / "data"
    site.mkdir()
    runtime = data / "share/tessera/release/opencode-observer/runtime-v8"
    shutil.copytree(ROOT / "release/opencode-observer/runtime-v8", runtime)
    docs = data / "share/tessera/docs"
    docs.mkdir(parents=True)
    for name in ("runtime-setup.md", "runtime-operator-setup.md"):
        (docs / name).write_text("synthetic resource fixture")
    starter = data / STARTER_INSTALLED
    starter.mkdir(parents=True)
    for name in STARTER_FILES:
        (starter / name).write_text("synthetic starter resource")
    module = site / "tessera_starter/__init__.py"
    module.parent.mkdir()
    module.write_text("# fixture")
    wheel = tmp_path / "fixture.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("tessera_starter/__init__.py", b"# fixture")
        for path in data.rglob("*"):
            if path.is_file():
                archive.writestr(
                    "tessera_control-0.1.0rc2.data/data/" + path.relative_to(data).as_posix(),
                    path.read_bytes(),
                )
    monkeypatch.setattr(
        check_installed_release.sysconfig,
        "get_path",
        lambda name: str(site if name == "purelib" else data),
    )
    monkeypatch.setattr(
        check_installed_release.importlib.metadata,
        "distribution",
        lambda name: types.SimpleNamespace(version="0.1.0rc2"),
    )
    monkeypatch.setattr(
        check_installed_release.importlib,
        "import_module",
        lambda name: types.SimpleNamespace(__file__=str(module)),
    )
    if tamper:
        module.write_text("changed")
        with pytest.raises(ValueError, match="byte mismatch"):
            check_installed_release.verify(wheel)
    else:
        result = check_installed_release.verify(wheel)
        assert result["public_runtime"]["bundle_namespace_sha256"] == NAMESPACE
        assert result["verified_files"] == 23
        assert result["starter_resource_count"] == 6
