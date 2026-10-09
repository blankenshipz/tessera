import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
from persistent_release_gate import run_gate
from release_input_parity import assert_unchanged, inventory, verify_wheel


@pytest.fixture
def public_root(tmp_path):
    root = tmp_path / "public"
    root.mkdir()
    for name in (
        "README.md",
        "DESIGN.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "pyproject.toml",
        "uv.lock",
    ):
        (root / name).write_text("public fixture\n")
    (root / "src/tessera_starter").mkdir(parents=True)
    (root / "src/tessera_starter/__init__.py").write_text("# public\n")
    return root


def test_pending_inventory_includes_untracked_examples_and_decisions(public_root):
    for name in (
        "examples/cpu/data.csv",
        "examples/cpu/recipe.json",
        "examples/cpu/Dockerfile",
        "docs/decisions/public.md",
    ):
        path = public_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("public\n")
    names = {entry["path"] for entry in inventory(public_root)["inputs"]}
    assert {
        "examples/cpu/data.csv",
        "examples/cpu/recipe.json",
        "examples/cpu/Dockerfile",
        "docs/decisions/public.md",
    } <= names


def test_inventory_change_invalidates(public_root):
    before = inventory(public_root)
    (public_root / "README.md").write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        assert_unchanged(public_root, before)


def test_missing_contract_fails(public_root):
    (public_root / "DESIGN.md").unlink()
    with pytest.raises(ValueError, match="contracts"):
        inventory(public_root)


def test_private_inputs_excluded(public_root):
    path = public_root / "examples/credentials/secret.json"
    path.parent.mkdir(parents=True)
    path.write_text("synthetic never ship")
    assert path.relative_to(public_root).as_posix() not in {
        row["path"] for row in inventory(public_root)["inputs"]
    }


@pytest.mark.parametrize("exit_code", [0, 7])
def test_complete_private_output_native_exit_preserved(public_root, tmp_path, exit_code):
    directory = tmp_path / "receipt"
    result = run_gate(
        public_root,
        directory,
        [
            sys.executable,
            "-c",
            "import sys;print('fixture-out');print('fixture-err',file=sys.stderr);"
            f"sys.exit({exit_code})",
        ],
        label="fixture",
        candidate_revision="fixture-sha256",
        timeout=10,
    )
    assert result["exit_code"] == exit_code
    assert result["status"] == ("passed" if exit_code == 0 else "incomplete")
    assert (directory / "stdout.log").read_text() == "fixture-out\n"
    assert (directory / "stderr.log").read_text() == "fixture-err\n"
    assert os.stat(directory).st_mode & 0o777 == 0o700
    assert os.stat(directory / "stdout.log").st_mode & 0o777 == 0o600
    assert (
        json.loads((directory / "result.json").read_text())["candidate_revision"]
        == "fixture-sha256"
    )


def test_existing_receipt_never_overwritten(public_root, tmp_path):
    with pytest.raises(FileExistsError):
        run_gate(
            public_root,
            tmp_path,
            [sys.executable, "-c", "pass"],
            label="fixture",
            candidate_revision="fixture",
            timeout=10,
        )


def test_timeout_retains_native_evidence(public_root, tmp_path):
    result = run_gate(
        public_root,
        tmp_path / "receipt",
        [sys.executable, "-c", "import time;print('started',flush=True);time.sleep(10)"],
        label="fixture",
        candidate_revision="fixture",
        timeout=0.2,
    )
    assert result["category"] == "timeout"
    assert result["exit_code"] != 0 and result["status"] == "incomplete"
    assert (tmp_path / "receipt/stdout.log").read_text() == "started\n"


def test_missing_command_incomplete(public_root, tmp_path):
    result = run_gate(
        public_root,
        tmp_path / "receipt",
        [str(tmp_path / "nonexistent")],
        label="fixture",
        candidate_revision="fixture",
        timeout=10,
    )
    assert result["category"] == "command_unavailable" and result["status"] == "incomplete"


def test_source_drift_fails_native_zero(public_root, tmp_path):
    result = run_gate(
        public_root,
        tmp_path / "receipt",
        [sys.executable, "-c", "from pathlib import Path;Path('README.md').write_text('drift')"],
        label="fixture",
        candidate_revision="fixture",
        timeout=10,
    )
    assert result["exit_code"] == 0
    assert result["source_unchanged"] is False and result["status"] == "incomplete"


def test_missing_wheel_resource_fails(public_root, tmp_path):
    wheel = tmp_path / "fixture.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("tessera_starter/__init__.py", "# public\n")
    with pytest.raises(KeyError):
        verify_wheel(public_root, wheel, {"README.md": "share/README.md"})


@pytest.mark.parametrize("changed", [False, True])
def test_wheel_byte_parity(public_root, tmp_path, changed):
    wheel = tmp_path / "fixture.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("tessera_starter/__init__.py", "wrong" if changed else "# public\n")
    if changed:
        with pytest.raises(ValueError, match="parity"):
            verify_wheel(public_root, wheel, {})
    else:
        assert verify_wheel(public_root, wheel, {})["verified_files"] == 1
