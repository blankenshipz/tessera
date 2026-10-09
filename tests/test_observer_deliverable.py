"""Actual shipped public byte pins/patch; no ignored-worktree or auth dependency."""

import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
from test_release_candidate import builder

BUNDLE = Path(__file__).parents[1] / "release/opencode-observer"
TARGET = "6cb7fc98728449d640b9711389ef036de78735de74b0da8d60ccfe2d304c9e96"


def test_actual_public_bundle_stages_27_originals_36_targets_and_both_licenses(tmp_path):
    baseline = builder.observer_bundle(BUNDLE)
    assert len(baseline["baseline_files"]) == 27 and len(baseline["target_files"]) == 36
    assert baseline["license_path"] == "LICENSE.opencode"
    assert set(baseline["licenses"]) == {"LICENSE.opencode", "harness/LICENSE"}
    assert baseline["source_date_epoch"] == 0
    assert (
        baseline["upstream_verification"] == "not_independently_verified"
        and "commit" not in baseline
    )
    assert (
        baseline["baseline_archive_sha256"]
        == "abfa57fe6c7235fe48be9f43d61902f1cc80e21bdee41d7de0464e465d249e91"
    )
    assert (
        baseline["patch_sha256"]
        == "d9efbc82580a9c1484c5db1c3dd61810e3d8166a03d75200cbc66819d6b96e69"
    )
    destination = tmp_path / "staged"
    builder.stage_observer(BUNDLE, destination)
    assert builder.observer_bundle(destination) == baseline
    assert (destination / "baseline.tar.gz").read_bytes() == (
        BUNDLE / "baseline.tar.gz"
    ).read_bytes()


def test_native_patch_reconstructs_all_36_exact_public_targets(tmp_path):
    baseline = builder.observer_bundle(BUNDLE)
    with tarfile.open(BUNDLE / "baseline.tar.gz") as tar:
        for member in tar:
            path = tmp_path / member.name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(tar.extractfile(member).read())
            path.chmod(member.mode)
    result = subprocess.run(
        ["patch", "-p1", "--batch", "-i", str(BUNDLE / "observer.patch")],
        cwd=tmp_path,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, "Native public patch application failed."
    target = {
        p.relative_to(tmp_path).as_posix(): builder.digest(p.read_bytes())
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    assert target == baseline["target_files"]
    index = "".join(f"{value}  {name}\n" for name, value in sorted(target.items())).encode()
    assert builder.digest(index) == TARGET


@pytest.mark.parametrize("change", ["omit_reproduction_test", "omit_harness_license"])
def test_actual_layout_cannot_drop_reproduction_or_licensing(tmp_path, change):
    for name in ("baseline.json", "LICENSE", "observer.patch", "baseline.tar.gz"):
        shutil.copyfile(BUNDLE / name, tmp_path / name)
    config = json.loads((tmp_path / "baseline.json").read_text())
    if change == "omit_reproduction_test":
        config["target_files"].pop("harness/tests/test_quota_check_receipt.py")
    else:
        config["licenses"].pop("harness/LICENSE")
    (tmp_path / "baseline.json").write_text(json.dumps(config))
    with pytest.raises(ValueError):
        builder.observer_bundle(tmp_path)
