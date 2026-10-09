"""Actual bounded CSV/filesystem checks with synthetic data, no provider/API proof."""

import hashlib
import json
import os
from dataclasses import asdict

import pytest
from pydantic import ValidationError

from tessera_control.dataset_snapshot_models import MAX_DATA_BYTES, DatasetDeclaration
from tessera_control.dataset_snapshots import (
    DatasetIntegrityError,
    build_dataset_capture,
    canonical_bytes,
    load_dataset_snapshot,
    manifest_bytes,
    read_dataset_snapshot,
    verify_dataset_bytes,
    verify_dataset_file,
)
from tessera_control.snapshots import _validate_capture
from tessera_control.workspace import inventory_digest

DATA = b"row_id,x,target,split\na,1,2,train\nb,2,4,validation\nc,3,6,test\n"
DECLARATION = {
    "version": "tessera-dataset-snapshot-v1",
    "profile": "bounded-tabular-regression-v1",
    "columns": [
        {"name": "row_id", "type": "string", "role": "id"},
        {"name": "x", "type": "number", "role": "feature"},
        {"name": "target", "type": "number", "role": "target"},
        {"name": "split", "type": "split", "role": "split"},
    ],
    "source": {"kind": "synthetic", "reference": "test-linear-v1", "license": "CC0-1.0"},
}


def verify(data=DATA, declaration=DECLARATION):
    return verify_dataset_bytes(
        data, declaration, expected_sha256=hashlib.sha256(data).hexdigest(), expected_size=len(data)
    )


def snapshot(tmp_path):
    manifest = verify()
    encoded = manifest_bytes(manifest)
    (tmp_path / "dataset.csv").write_bytes(DATA)
    (tmp_path / "dataset-manifest.json").write_bytes(encoded)
    return manifest, hashlib.sha256(encoded).hexdigest()


def test_actual_bytes_manifest_splits_and_existing_capture_envelope(tmp_path):
    manifest, digest = snapshot(tmp_path)
    assert manifest.row_count == 3 and tuple(s.row_count for s in manifest.splits) == (1, 1, 1)
    assert (
        manifest.splits[0].sha256
        == hashlib.sha256(
            canonical_bytes(
                {"columns": ["row_id", "x", "target", "split"], "rows": [["a", "1", "2", "train"]]}
            )
        ).hexdigest()
    )
    assert read_dataset_snapshot(tmp_path, expected_manifest_digest=digest) == manifest
    assert manifest.license_verification == "declared-not-legally-verified"
    capture = build_dataset_capture(
        DATA,
        DECLARATION,
        expected_sha256=manifest.data_sha256,
        expected_size=len(DATA),
        expected_manifest_digest=digest,
    )
    retained = _validate_capture(capture, inventory_digest(capture.inventory))
    assert retained == json.loads(json.dumps(asdict(capture.inventory)))
    assert dict(capture.files)["dataset.csv"] == DATA
    assert all(e.provenance == "imported_unverified" for e in capture.inventory.entries)


@pytest.mark.parametrize(
    "data",
    [
        DATA.replace(b"x,target", b"wrong,target"),
        DATA.replace(b"a,1,2", b"a,1"),
        DATA.replace(b"a,1,2", b"a,nan,2"),
        DATA.replace(b"a,1,2", b"a,1e309,2"),
        DATA.replace(b"validation", b"train"),
        DATA.replace(b"b,2,4", b"a,2,4"),
        DATA.replace(b"train", b"unknown"),
        DATA.replace(b"a,1,2", b'a,"1\n2",2'),
        DATA.replace(b"a,1,2", b'a,"1,2'),
        b"\xef\xbb\xbf" + DATA,
        DATA + b"\xff",
    ],
)
def test_actual_parse_rejects_bad_data_despite_self_consistent_digest(data):
    with pytest.raises(DatasetIntegrityError):
        verify(data)


@pytest.mark.parametrize("target", ["digest", "size", "bool-size", "oversize"])
def test_independent_expected_target_and_bounds(target):
    data = DATA if target != "oversize" else b"a" * (MAX_DATA_BYTES + 1)
    digest = "0" * 64 if target == "digest" else hashlib.sha256(data).hexdigest()
    size = True if target == "bool-size" else len(data) + (target == "size")
    with pytest.raises(DatasetIntegrityError):
        verify_dataset_bytes(data, DECLARATION, expected_sha256=digest, expected_size=size)


@pytest.mark.parametrize("change", ["license", "verified", "schema-echo", "duplicate-column"])
def test_closed_human_declarations_are_not_observed_evidence(change):
    declaration = json.loads(json.dumps(DECLARATION))
    if change == "license":
        declaration["source"].pop("license")
    elif change == "duplicate-column":
        declaration["columns"][1]["name"] = "row_id"
    else:
        declaration[change] = True
    with pytest.raises(ValidationError):
        DatasetDeclaration.model_validate(declaration)


def test_mutated_data_and_forged_split_observation_rejected(tmp_path):
    _, digest = snapshot(tmp_path)
    (tmp_path / "dataset.csv").write_bytes(DATA.replace(b"a,1,2", b"a,1,9"))
    with pytest.raises(DatasetIntegrityError):
        read_dataset_snapshot(tmp_path, expected_manifest_digest=digest)
    snapshot(tmp_path)
    record = json.loads((tmp_path / "dataset-manifest.json").read_bytes())
    record["splits"][0]["sha256"] = "0" * 64
    forged = canonical_bytes(record)
    (tmp_path / "dataset-manifest.json").write_bytes(forged)
    with pytest.raises(DatasetIntegrityError):
        read_dataset_snapshot(tmp_path, expected_manifest_digest=hashlib.sha256(forged).hexdigest())


@pytest.mark.parametrize(
    "path", ["../dataset.csv", "/dataset.csv", "nested/dataset.csv", "..", "x\\y"]
)
def test_traversal_and_unsafe_selected_paths_rejected(tmp_path, path):
    with pytest.raises(DatasetIntegrityError):
        verify_dataset_file(
            tmp_path,
            path,
            DECLARATION,
            expected_sha256=hashlib.sha256(DATA).hexdigest(),
            expected_size=len(DATA),
        )


def test_symlink_leaf_and_ancestor_rejected(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    (actual / "dataset.csv").write_bytes(DATA)
    (tmp_path / "dataset.csv").symlink_to(actual / "dataset.csv")
    (tmp_path / "linked").symlink_to(actual, target_is_directory=True)
    for root in (tmp_path, tmp_path / "linked"):
        with pytest.raises(DatasetIntegrityError):
            verify_dataset_file(
                root,
                "dataset.csv",
                DECLARATION,
                expected_sha256=hashlib.sha256(DATA).hexdigest(),
                expected_size=len(DATA),
            )


def test_file_mutation_during_read_denied(tmp_path, monkeypatch):
    path = tmp_path / "dataset.csv"
    path.write_bytes(DATA)
    original = os.read
    changed = False

    def read(fd, size):
        nonlocal changed
        chunk = original(fd, size)
        if chunk and not changed:
            changed = True
            path.write_bytes(DATA.replace(b"a,1,2", b"a,1,9"))
        return chunk

    monkeypatch.setattr(os, "read", read)
    with pytest.raises(DatasetIntegrityError):
        verify_dataset_file(
            tmp_path,
            "dataset.csv",
            DECLARATION,
            expected_sha256=hashlib.sha256(DATA).hexdigest(),
            expected_size=len(DATA),
        )


def test_reviewed_manifest_digest_not_recomputed_as_approval():
    with pytest.raises(DatasetIntegrityError):
        build_dataset_capture(
            DATA,
            DECLARATION,
            expected_sha256=hashlib.sha256(DATA).hexdigest(),
            expected_size=len(DATA),
            expected_manifest_digest="0" * 64,
        )


@pytest.mark.parametrize("bound", ["rows", "cell"])
def test_row_and_cell_bounds(bound):
    if bound == "rows":
        labels = ("train", "validation", "test")
        data = (
            b"row_id,x,target,split\n"
            + "".join(f"r{i},1,2,{labels[i % 3]}\n" for i in range(10_001)).encode()
        )
    else:
        data = DATA.replace(b"a,1,2", b"a," + b"1" * 1025 + b",2")
    with pytest.raises(DatasetIntegrityError):
        verify(data)


def test_nonregular_dataset_file_denied_without_waiting(tmp_path):
    os.mkfifo(tmp_path / "dataset.csv")
    with pytest.raises(DatasetIntegrityError):
        verify_dataset_file(
            tmp_path,
            "dataset.csv",
            DECLARATION,
            expected_sha256=hashlib.sha256(DATA).hexdigest(),
            expected_size=len(DATA),
        )


def test_starter_consumes_same_verified_bytes_not_a_later_path_read(tmp_path):
    expected, digest = snapshot(tmp_path)
    observed, content = load_dataset_snapshot(tmp_path, expected_manifest_digest=digest)
    (tmp_path / "dataset.csv").write_bytes(DATA.replace(b"a,1,2", b"a,1,9"))
    assert observed == expected and content == DATA
