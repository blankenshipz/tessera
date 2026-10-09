"""Offline packaging mechanics; never accepted-release or live-reasoning evidence."""

import importlib.util
import json
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "rc_builder", Path(__file__).parents[1] / "dev/build_release_candidate.py"
)
builder = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(Path(__file__).parents[1] / "dev"))
try:
    spec.loader.exec_module(builder)
finally:
    sys.path.pop(0)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    (root / "src/tessera_example").mkdir(parents=True)
    (root / "src/tessera_example/__init__.py").write_text('VALUE = "packaged"\n')
    (root / "pyproject.toml").write_text("""[build-system]
requires = ["setuptools>=77,<82"]
build-backend = "setuptools.build_meta"
[project]
name = "tessera-example"
version = "0.1.0"
requires-python = ">=3.11"
license = "MIT"
license-files = ["LICENSE"]
[tool.setuptools.packages.find]
where = ["src"]
""")
    (root / "README.md").write_text("Synthetic distribution fixture\n")
    (root / "LICENSE").write_text("MIT fixture license\n")
    return root


def public_bundle(root):
    root.mkdir()
    license_bytes = b"MIT synthetic upstream license\n"
    patch = (
        b"diff --git a/packages/sdk/a.ts b/packages/sdk/a.ts\n"
        b"--- a/packages/sdk/a.ts\n+++ b/packages/sdk/a.ts\n@@ -1 +1 @@\n-old\n+new\n"
    )
    (root / "LICENSE").write_bytes(license_bytes)
    (root / "observer.patch").write_bytes(patch)
    (root / "baseline.json").write_text(
        json.dumps(
            {
                "schema_version": "tessera-opencode-observer-baseline-v1",
                "repository": "https://example.test/public/source",
                "commit": "a" * 40,
                "license_sha256": builder.digest(license_bytes),
                "patch_sha256": builder.digest(patch),
            }
        )
    )
    return root


def test_allowlist_excludes_state_credentials_worktrees_and_generated_metadata(source):
    for name in [
        ".tessera/owner.header",
        ".env",
        "src/credentials/private.py",
        "src/.worktrees/private.py",
        "src/__pycache__/cached.py",
        "src/thing.egg-info/PKG-INFO",
        "docs/receipts/private.md",
        "deploy/auth/private.json",
        "docs/run.log",
    ]:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("DO_NOT_PACKAGE")
    selected = builder.inputs(source)
    assert all(b"DO_NOT_PACKAGE" not in data for data in selected.values())
    assert "src/tessera_example/__init__.py" in selected


def test_symlink_input_rejected_without_reading_target(source):
    (source / "src/tessera_example/link.py").symlink_to(source / ".secret-not-present")
    # A dangling symlink is not selected; a symlink to an existing file is rejected.
    (source / "src/tessera_example/link.py").unlink()
    (source / "src/tessera_example/link.py").symlink_to(source / "README.md")
    with pytest.raises(ValueError, match="ordinary"):
        builder.inputs(source)


def test_documented_deploy_dockerfile_has_narrow_public_inventory(source):
    allowed = "deploy/remote-agent-platform/docker/Dockerfile"
    adjacent = allowed + ".dockerignore"
    excluded = [
        "deploy/other/Dockerfile",
        "deploy/auth/Dockerfile",
        "deploy/remote-agent-platform/docker/credentials/private.py",
        "deploy/remote-agent-platform/docker/unreviewed.bin",
    ]
    for name in [allowed, adjacent, *excluded]:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Synthetic public fixture\n")
    selected = builder.inputs(source)
    assert selected[allowed] == (source / allowed).read_bytes()
    assert selected[adjacent] == (source / adjacent).read_bytes()
    assert not set(excluded) & selected.keys()


def test_documented_deploy_dockerfile_symlink_is_rejected(source):
    path = source / "deploy/remote-agent-platform/docker/Dockerfile"
    path.parent.mkdir(parents=True)
    path.symlink_to(source / "README.md")
    with pytest.raises(ValueError, match="ordinary"):
        builder.inputs(source)


@pytest.mark.parametrize(
    "name",
    [
        "plugins/token.json",
        "docs/auth.json",
        "deploy/credentials.json",
        "docs/tokens.json",
        "docs/owner.header",
        "docs/worker.header",
        "deploy/credentials",
        "deploy/token",
        "docs/client.pem",
        "docs/client.key",
        "docs/control.sqlite3",
        "docs/control.db",
    ],
)
def test_credential_named_inputs_are_excluded_without_reading(source, monkeypatch, name):
    path = source / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"SYNTHETIC_EXCLUDED_NOT_A_SECRET\n")
    read_bytes = Path.read_bytes

    def guarded_read(candidate):
        assert candidate != path, "Excluded credential-shaped fixture must not be read"
        return read_bytes(candidate)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    assert not builder.safe(Path(name))
    assert name not in builder.inputs(source)


def test_credential_basename_policy_matches_source_gate():
    gate_spec = importlib.util.spec_from_file_location(
        "credential_policy_gate",
        Path(__file__).parents[1] / "scripts/run_release_candidate_gates.py",
    )
    gates = importlib.util.module_from_spec(gate_spec)
    gate_spec.loader.exec_module(gates)
    assert builder.AUTH_FILES == gates.AUTH_FILES
    for name in builder.AUTH_FILES:
        assert not builder.safe(Path("docs") / name)
        with pytest.raises(gates.GateInputError, match="excluded_source_path"):
            gates.source_path("docs/" + name)


def test_reproducible_source_and_wheel_and_manifest(source, tmp_path):
    deployment = "deploy/remote-agent-platform/docker/Dockerfile"
    dockerfile = source / deployment
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_bytes(b"# Synthetic public deployment fixture\nFROM scratch\n")
    excluded = ["plugins/token.json", "docs/auth.json", "deploy/credentials.json"]
    for name in excluded:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"SYNTHETIC_EXCLUDED_NOT_A_SECRET\n")
    lock = (
        'version = 1\n[[package]]\nname = "tessera-example"\n'
        'version = "0.1.0"\nsource = { editable = "." }\n'
    )
    (source / "uv.lock").write_text(lock)
    one = builder.build(source, tmp_path / "one", "0.1.0rc1", 1767225600, development=True)
    two = builder.build(source, tmp_path / "two", "0.1.0rc1", 1767225600, development=True)
    assert one == two
    assert one["acceptance"] == "candidate_only_not_accepted_release"
    assert one["development_snapshot"] is True and one["observer"] is None
    assert set(one["normalized_paths"]) == {"pyproject.toml", "uv.lock"}
    assert (source / "uv.lock").read_text() == lock
    assert (source / "pyproject.toml").read_text().find('version = "0.1.0"') >= 0
    with tarfile.open(tmp_path / "one/tessera-0.1.0rc1-source.tar.gz") as archive:
        assert not {"tessera-0.1.0rc1/" + name for name in excluded} & set(archive.getnames())
        assert all(item.mtime == 1767225600 and item.uid == item.gid == 0 for item in archive)
        staged_lock = archive.extractfile("tessera-0.1.0rc1/uv.lock").read()
        assert archive.extractfile("tessera-0.1.0rc1/" + deployment).read() == (
            dockerfile.read_bytes()
        )
        assert tomllib.loads(staged_lock.decode())["package"][0]["version"] == "0.1.0rc1"
        assert one["normalized_paths"]["uv.lock"]["staged_sha256"] == builder.digest(staged_lock)
    wheel = next((tmp_path / "one").glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        assert not any(
            b"SYNTHETIC_EXCLUDED_NOT_A_SECRET" in archive.read(name) for name in archive.namelist()
        )
        assert any("tessera_example/__init__.py" == name for name in archive.namelist())
        assert any("licenses/LICENSE" in name for name in archive.namelist())


def test_final_candidate_requires_pinned_observer(source, tmp_path):
    with pytest.raises(FileNotFoundError):
        builder.build(source, tmp_path / "final", "0.1.0rc1", 1767225600)
    assert not (tmp_path / "final").exists()


def test_production_optional_observer_data_files_support_development_wheel(source, tmp_path):
    config = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    files = config["tool"]["setuptools"]["data-files"]["share/tessera/release/opencode-observer"]
    observer = source / "release/opencode-observer"
    observer.mkdir(parents=True)
    (observer / "README.md").write_text("Public staging contract only\n")
    with (source / "pyproject.toml").open("a") as handle:
        handle.write(
            '\n[tool.setuptools.data-files]\n"share/tessera/release/opencode-observer" = '
            + json.dumps(files)
            + "\n"
        )
    result = builder.build(
        source, tmp_path / "development", "0.1.0rc1", 1767225600, development=True
    )
    assert result["observer"] is None
    with zipfile.ZipFile(next((tmp_path / "development").glob("*.whl"))) as archive:
        assert any(
            name.endswith("release/opencode-observer/README.md") for name in archive.namelist()
        )


@pytest.mark.parametrize("change", ["hash", "extra", "path", "private_uri"])
def test_observer_bundle_rejects_bad_pins_and_private_paths(tmp_path, change):
    root = public_bundle(tmp_path / "public")
    baseline = json.loads((root / "baseline.json").read_text())
    if change == "hash":
        baseline["patch_sha256"] = "b" * 64
    if change == "extra":
        baseline["approved"] = True
    if change == "private_uri":
        baseline["repository"] = "https://user:password@example.test/source"
    if change == "path":
        patch = b"--- a/credentials/private.ts\n+++ b/credentials/private.ts\n"
        (root / "observer.patch").write_bytes(patch)
        baseline["patch_sha256"] = builder.digest(patch)
    (root / "baseline.json").write_text(json.dumps(baseline))
    with pytest.raises(ValueError):
        builder.observer_bundle(root)


def test_explicit_public_staging_does_not_overwrite(tmp_path):
    bundle = public_bundle(tmp_path / "public")
    destination = tmp_path / "staged"
    builder.stage_observer(bundle, destination)
    assert set(p.name for p in destination.iterdir()) == {
        "baseline.json",
        "LICENSE",
        "observer.patch",
    }
    assert builder.observer_bundle(destination) == builder.observer_bundle(bundle)
    with pytest.raises(ValueError, match="overwrite"):
        builder.stage_observer(bundle, destination)


@pytest.mark.parametrize("version,epoch", [("0.1.0", 1767225600), ("0.1.0rc1", 1)])
def test_explicit_version_and_epoch_required(source, tmp_path, version, epoch):
    with pytest.raises(ValueError):
        builder.build(source, tmp_path / "out", version, epoch, development=True)


def test_lock_normalization_preserves_unrelated_same_version_and_all_pins():
    text = """version = 1
[[package]]
name = "unrelated"
version = "0.1.0"
source = { registry = "https://pypi.org/simple" }
wheels = [{ url = "https://example.test/dependency.whl", hash = "sha256:immutable", size = 42 }]
[[package]]
name = "tessera-control"
version = "0.1.0"
source = { editable = "." }
dependencies = [{ name = "unrelated" }]
"""
    result = builder.normalize_self_lock(text, "tessera-control", "0.1.0rc1")
    assert result == text.replace(
        'name = "tessera-control"\nversion = "0.1.0"',
        'name = "tessera-control"\nversion = "0.1.0rc1"',
        1,
    )
    assert tomllib.loads(result)["package"][0] == tomllib.loads(text)["package"][0]


@pytest.mark.parametrize("kind", ["missing", "registry", "duplicate"])
def test_lock_normalization_fails_closed_on_ambiguous_self_package(kind):
    text = (
        'version = 1\n[[package]]\nname = "tessera-control"\n'
        'version = "0.1.0"\nsource = { editable = "." }\n'
    )
    if kind == "missing":
        text = text.replace("tessera-control", "other")
    if kind == "registry":
        text = text.replace('editable = "."', 'registry = "https://pypi.org/simple"')
    if kind == "duplicate":
        text += text[text.index("[[package]]") :]
    with pytest.raises(ValueError):
        builder.normalize_self_lock(text, "tessera-control", "0.1.0rc1")
