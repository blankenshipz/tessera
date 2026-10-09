"""Guard unit tests only; no candidate service or installed acceptance run."""

import base64
import csv
import hashlib
import io
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
from protocol_installation import (
    check_origins,
    check_paths,
    check_wheel_files,
    check_wheel_metadata,
)  # noqa: E402


@pytest.fixture
def candidate_paths(tmp_path):
    prefix = tmp_path / "candidate"
    bin_dir = prefix / "bin"
    bin_dir.mkdir(parents=True)
    (prefix / "pyvenv.cfg").write_text("fixture, not executable acceptance")
    python = bin_dir / "python"
    python.write_text("fixture")
    python.chmod(0o700)
    for name in ("tessera-mcp", "tessera-a2a-peer"):
        path = bin_dir / name
        path.write_text("#!" + str(python) + "\n")
        path.chmod(0o700)
    wheel = tmp_path / "fixture.whl"
    wheel.write_bytes(b"synthetic path guard input, not a wheel")
    return python, bin_dir, wheel, hashlib.sha256(wheel.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "case",
    [
        "wrong_sha",
        "missing_wheel",
        "wrong_bin",
        "missing_entry",
        "wrong_shebang",
        "editable",
        "tessera_pth",
    ],
)
def test_candidate_path_guard_rejects_before_launch(candidate_paths, case):
    python, bin_dir, wheel, sha = candidate_paths
    if case == "wrong_sha":
        sha = "0" * 64
    elif case == "missing_wheel":
        wheel.unlink()
    elif case == "wrong_bin":
        bin_dir = bin_dir.parent
    elif case == "missing_entry":
        (bin_dir / "tessera-mcp").unlink()
    elif case == "wrong_shebang":
        (bin_dir / "tessera-mcp").write_text("#!/source/python\n")
    else:
        site = bin_dir.parent / "lib/python3.11/site-packages"
        site.mkdir(parents=True)
        (site / "loader.pth").write_text(
            "import __editable__loader" if case == "editable" else "/source/tessera/src"
        )
    with pytest.raises(ValueError):
        check_paths(python, bin_dir, wheel, sha)


def test_explicit_third_party_pth_does_not_waive_product_origin_check(candidate_paths, tmp_path):
    python, bin_dir, wheel, sha = candidate_paths
    site = bin_dir.parent / "lib/python3.11/site-packages"
    site.mkdir(parents=True)
    # Relative .pth entries are resolved against site-packages. Avoid inheriting
    # "tessera" from the test-state ancestor and tripping the strict path guard
    # before this test can exercise the independent product-origin check.
    (site / "thirdparty-only").mkdir()
    (site / "thirdparty.pth").write_text("thirdparty-only\n")
    check_paths(python, bin_dir, wheel, sha)
    with pytest.raises(ValueError, match="escaped"):
        check_origins(
            {"tessera_control": str(tmp_path / "source/tessera_control/__init__.py")}, [site]
        )


def test_origin_guard_rejects_source_symlink_and_missing_origins(tmp_path):
    site = tmp_path / "site-packages"
    source = tmp_path / "source"
    site.mkdir()
    source.mkdir()
    (site / "tessera_control").symlink_to(source, target_is_directory=True)
    with pytest.raises(ValueError):
        check_origins({"tessera_control": str(site / "tessera_control/__init__.py")}, [site])
    with pytest.raises(ValueError):
        check_origins({}, [site])


def test_installed_launcher_rejects_bad_binding_before_fixture_or_service_imports(
    candidate_paths, tmp_path
):
    python, bin_dir, wheel, _ = candidate_paths
    harness = Path(__file__).resolve().parents[1] / "dev/check_protocol_compatibility.py"
    result = subprocess.run(
        [
            sys.executable,
            str(harness),
            "--installed-python",
            str(python),
            "--installed-bin-dir",
            str(bin_dir),
            "--wheel",
            str(wheel),
            "--wheel-sha256",
            "0" * 64,
            "--receipt",
            str(tmp_path / "not-created.json"),
        ],
        env={"PATH": str(Path(sys.executable).parent), "PYTHONNOUSERSITE": "1"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0 and "Wheel SHA256 mismatch" in result.stderr
    assert "test_a2a_research_generated_backend" not in result.stderr
    assert not (tmp_path / "not-created.json").exists()


def test_uv_hashless_and_pip_hashed_metadata_keep_independent_proof(candidate_paths):
    _, _, wheel, sha = candidate_paths
    uv = check_wheel_metadata(
        {"url": wheel.resolve().as_uri(), "archive_info": {}}, "uv\n", wheel, sha
    )
    assert uv["installer_archive_sha256"] is None
    assert uv["binding_basis"].startswith("uv_exact_local_url")
    pip = check_wheel_metadata(
        {"url": wheel.resolve().as_uri(), "archive_info": {"hashes": {"sha256": sha}}},
        "pip",
        wheel,
        sha,
    )
    assert pip["installer_archive_sha256"] == sha


@pytest.mark.parametrize(
    "case",
    [
        "bad_hash",
        "conflicting_hash",
        "other_installer",
        "editable",
        "directory",
        "extra",
        "remote",
        "remote_file",
        "url_mismatch",
        "escape",
        "query",
        "missing_sha",
        "wrong_independent_sha",
        "unknown_archive",
        "empty_hashes",
    ],
)
def test_uv_metadata_negative_cases(candidate_paths, case):
    _, _, wheel, sha = candidate_paths
    direct = {"url": wheel.resolve().as_uri(), "archive_info": {}}
    installer = "uv"
    if case == "bad_hash":
        direct["archive_info"] = {"hashes": {"sha256": "0" * 64}}
    elif case == "conflicting_hash":
        direct["archive_info"] = {"hashes": {"sha256": sha}, "hash": "sha256=" + "0" * 64}
    elif case == "other_installer":
        installer = "pip"
    elif case in {"editable", "directory"}:
        direct["dir_info"] = {"editable": case == "editable"}
    elif case == "extra":
        direct["extra"] = True
    elif case == "remote":
        direct["url"] = "https://example.test/fixture.whl"
    elif case == "remote_file":
        direct["url"] = "file://example.test" + str(wheel)
    elif case == "url_mismatch":
        direct["url"] = wheel.parent.as_uri()
    elif case == "escape":
        direct["url"] = wheel.parent.as_uri() + "/../" + wheel.parent.name + "/" + wheel.name
    elif case == "query":
        direct["url"] += "?extra=1"
    elif case == "missing_sha":
        sha = None
    elif case == "wrong_independent_sha":
        sha = "0" * 64
    elif case == "unknown_archive":
        direct["archive_info"] = {"extra": True}
    elif case == "empty_hashes":
        direct["archive_info"] = {"hashes": {}}
    with pytest.raises(ValueError):
        check_wheel_metadata(direct, installer, wheel, sha)


@pytest.fixture
def miniature_wheel(tmp_path):
    site = tmp_path / "site-packages"
    site.mkdir()
    name = "tessera_control/__init__.py"
    body = b"# synthetic guard fixture\n"
    row = [
        name,
        "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode().rstrip("="),
        str(len(body)),
    ]
    stream = io.StringIO()
    csv.writer(stream).writerow(row)
    metadata = {
        "METADATA": "Name: tessera-control\nVersion: 0.1.0rc1\n",
        "WHEEL": "Wheel-Version: 1.0\n",
        "entry_points.txt": "[console_scripts]\n",
        "RECORD": stream.getvalue(),
    }
    wheel = tmp_path / "synthetic.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(name, body)
        for key, value in metadata.items():
            archive.writestr("tessera_control-0.1.0rc1.dist-info/" + key, value)
    target = site / name
    target.parent.mkdir()
    target.write_bytes(body)

    class Dist:
        version = "0.1.0rc1"

        def read_text(self, key):
            return metadata[key]

    return wheel, site, target, metadata, Dist()


@pytest.mark.parametrize(
    "case", ["valid", "bytes", "record", "metadata", "version", "extra", "symlink"]
)
def test_installed_bytes_record_metadata_and_extras(miniature_wheel, tmp_path, case):
    wheel, site, target, metadata, dist = miniature_wheel
    if case == "bytes":
        target.write_bytes(b"changed")
    elif case == "record":
        metadata["RECORD"] = ""
    elif case == "metadata":
        metadata["METADATA"] += "Extra: changed\n"
    elif case == "version":
        dist.version = "different"
    elif case == "extra":
        (target.parent / "extra.py").write_text("not in wheel")
    elif case == "symlink":
        external = tmp_path / "external.py"
        external.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(external)
    with zipfile.ZipFile(wheel) as archive:
        if case == "valid":
            assert check_wheel_files(archive, dist, [site]) == 1
        else:
            with pytest.raises(ValueError):
                check_wheel_files(archive, dist, [site])
