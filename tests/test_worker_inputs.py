from __future__ import annotations

import base64
import copy
import hashlib
import json
import stat

import pytest

from tessera_worker.inputs import InputIntegrityError, materialize_snapshot


def bundle_for(files=None):
    files = files if files is not None else {"data/input.txt": b"public fixture", "empty": b""}
    manifest = {
        "root": "/original/local/source",
        "entries": [
            {
                "relative_path": path,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "classification": "unclassified",
                "provenance": "imported_unverified",
            }
            for path, content in files.items()
        ],
        "exclusions": [{"relative_path": "excluded", "reason": "not selected"}],
        "total_bytes": sum(map(len, files.values())),
    }
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "receipt": {
            "lease_id": "lease-fixture",
            "epoch": 1,
            "worker_principal_id": "worker-fixture",
            "snapshot_id": "snapshot-fixture",
            "snapshot_digest": digest,
            "delivery_confirmed": False,
        },
        "snapshot": {
            "id": "snapshot-fixture",
            "digest": digest,
            "manifest": manifest,
            "files": [
                {"relative_path": path, "content_base64": base64.b64encode(content).decode()}
                for path, content in files.items()
            ],
        },
    }


def stage(bundle, parent, **overrides):
    arguments = {
        "expected_snapshot_digest": bundle["snapshot"]["digest"],
        "expected_lease_id": "lease-fixture",
        "expected_epoch": 1,
        "expected_worker_id": "worker-fixture",
    }
    return materialize_snapshot(bundle, parent, **(arguments | overrides))


def test_exact_bytes_and_read_only_permissions_without_merging(tmp_path):
    bundle = bundle_for()
    existing = tmp_path / "existing.txt"
    existing.write_text("preserve")
    first = stage(bundle, tmp_path)
    second = stage(bundle, tmp_path)
    assert first.directory != second.directory
    assert existing.read_text() == "preserve"
    assert (first.directory / "data/input.txt").read_bytes() == b"public fixture"
    assert (first.directory / "empty").read_bytes() == b""
    assert first.file_count == 2
    assert first.total_bytes == len(b"public fixture")
    assert stat.S_IMODE(first.directory.stat().st_mode) == 0o555
    assert stat.S_IMODE((first.directory / "data").stat().st_mode) == 0o555
    assert stat.S_IMODE((first.directory / "data/input.txt").stat().st_mode) == 0o444


@pytest.mark.parametrize(
    "path", ["../escape", "/absolute", "a/../b", "a//b", "./b", "a\\b", "C:/x"]
)
def test_unsafe_paths_rejected_even_with_matching_manifest_digest(tmp_path, path):
    with pytest.raises(InputIntegrityError):
        stage(bundle_for({path: b"fixture"}), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_file_directory_collision_is_rejected_before_writes(tmp_path):
    with pytest.raises(InputIntegrityError, match="collides"):
        stage(bundle_for({"data": b"file", "data/nested": b"child"}), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_directory_depth_is_bounded_before_staging(tmp_path):
    with pytest.raises(InputIntegrityError, match="bounded relative"):
        stage(bundle_for({"/".join(["nested"] * 65): b"data"}), tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("paths", [("A", "a"), ("D/a", "d/b"), ("é", "e\u0301")])
def test_filesystem_aliases_are_rejected_before_staging(tmp_path, monkeypatch, paths):
    def forbid_staging(*args, **kwargs):
        pytest.fail("Input aliases must be rejected before any staging writes.")

    monkeypatch.setattr("tessera_worker.inputs.tempfile.mkdtemp", forbid_staging)
    with pytest.raises(InputIntegrityError, match="alias"):
        stage(bundle_for({path: b"fixture" for path in paths}), tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("change", ["bytes", "missing", "extra", "duplicate", "manifest", "base64"])
def test_corrupt_or_incomplete_delivery_never_leaves_partial_tree(tmp_path, change):
    bundle = bundle_for()
    snapshot = bundle["snapshot"]
    if change == "bytes":
        snapshot["files"][0]["content_base64"] = base64.b64encode(b"wrong contents").decode()
    elif change == "missing":
        snapshot["files"].pop()
    elif change == "extra":
        snapshot["files"].append({"relative_path": "extra", "content_base64": ""})
    elif change == "duplicate":
        snapshot["files"][1] = copy.deepcopy(snapshot["files"][0])
    elif change == "manifest":
        snapshot["manifest"]["root"] = "/changed"
    else:
        snapshot["files"][0]["content_base64"] = "!" * len(snapshot["files"][0]["content_base64"])
    with pytest.raises(InputIntegrityError):
        stage(bundle, tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_lease_id", "different-lease"),
        ("expected_epoch", 2),
        ("expected_epoch", True),
        ("expected_worker_id", "different-worker"),
        ("expected_snapshot_digest", "a" * 64),
    ],
)
def test_bundle_must_match_independently_supplied_authority(tmp_path, field, value):
    with pytest.raises(InputIntegrityError):
        stage(bundle_for(), tmp_path, **{field: value})
    assert list(tmp_path.iterdir()) == []


def test_failed_disk_write_cleans_only_new_tree(tmp_path, monkeypatch):
    existing = tmp_path / "keep"
    existing.write_text("unrelated")

    def fail(_):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr("tessera_worker.inputs.os.fsync", fail)
    with pytest.raises(OSError, match="disk failure"):
        stage(bundle_for(), tmp_path)
    assert list(tmp_path.iterdir()) == [existing]
    assert existing.read_text() == "unrelated"


def test_symlink_staging_parent_is_rejected(tmp_path):
    destination = tmp_path / "private"
    destination.mkdir()
    link = tmp_path / "link"
    link.symlink_to(destination, target_is_directory=True)
    with pytest.raises(InputIntegrityError, match="private 0700"):
        stage(bundle_for(), link)
    assert list(destination.iterdir()) == []


def test_broad_staging_parent_is_rejected_before_creating_a_tree(tmp_path):
    tmp_path.chmod(0o755)
    with pytest.raises(InputIntegrityError, match="private 0700"):
        stage(bundle_for(), tmp_path)
    assert list(tmp_path.iterdir()) == []
