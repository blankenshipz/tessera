from __future__ import annotations

import hashlib

import pytest
from test_artifacts import _two_path_delivery
from test_runtime import ApiChannel

from tessera_control.store import _connect
from tessera_worker.artifacts import ArtifactTransferError, upload_output_set
from tessera_worker.outputs import CollectedOutput, collect_outputs


def _collected(tmp_path):
    root = tmp_path / "collected"
    root.mkdir()
    (root / "first.txt").write_bytes(b"first\x00\xff")
    (root / "second.txt").write_bytes(b"second")
    return root, collect_outputs(root, ["second.txt", "first.txt"], 1024)


def test_collected_bytes_and_manifest_resume_after_lost_manifest_response(tmp_path):
    state, _, lease = _two_path_delivery(tmp_path)
    root, outputs = _collected(tmp_path)

    class LostManifest(ApiChannel):
        def post(self, path, body, key):
            result = super().post(path, body, key)
            if path.endswith("/artifact-manifest"):
                raise ConnectionError("synthetic lost manifest response")
            return result

    args = {"lease_id": lease["id"], "expected_epoch": lease["epoch"], "outputs": outputs}
    with pytest.raises(ConnectionError, match="lost manifest"):
        upload_output_set(LostManifest(state), **args)
    # In-memory retained bytes are immutable despite subsequent source changes.
    (root / "first.txt").write_bytes(b"replaced")
    receipt = upload_output_set(ApiChannel(state), **args)
    assert receipt["status"] == "quarantined" and receipt["total_bytes"] == 13
    assert [entry["relative_path"] for entry in receipt["artifacts"]] == ["first.txt", "second.txt"]
    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM artifact_manifests").fetchone()[0] == 1
        rows = connection.execute("SELECT relative_path, content FROM artifacts").fetchall()
        assert {row[0]: row[1] for row in rows} == {
            item.relative_path: item.content for item in outputs
        }


def test_incomplete_output_set_cannot_publish_a_manifest(tmp_path):
    state, _, lease = _two_path_delivery(tmp_path)
    _, outputs = _collected(tmp_path)
    with pytest.raises(ConnectionError, match="409"):
        upload_output_set(
            ApiChannel(state),
            lease_id=lease["id"],
            expected_epoch=lease["epoch"],
            outputs=outputs[:1],
        )
    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM artifact_manifests").fetchone()[0] == 0
        assert connection.execute("SELECT status FROM artifacts").fetchone()[0] == "quarantined"


def test_corrupt_retained_bytes_fail_before_any_request():
    class NoRequest:
        def post(self, *args):
            pytest.fail("invalid bytes must not reach control")

    with pytest.raises(ArtifactTransferError, match="checksum"):
        upload_output_set(
            NoRequest(),
            lease_id="lease_test",
            expected_epoch=1,
            outputs=(CollectedOutput("first.txt", b"bad", hashlib.sha256(b"good").hexdigest()),),
        )


def test_manifest_receipt_checksum_is_verified(tmp_path):
    state, _, lease = _two_path_delivery(tmp_path)
    _, outputs = _collected(tmp_path)

    class BadManifest(ApiChannel):
        def post(self, path, body, key):
            result = super().post(path, body, key)
            if path.endswith("/artifact-manifest"):
                result["manifest_digest"] = "0" * 64
            return result

    with pytest.raises(ArtifactTransferError, match="checksum"):
        upload_output_set(
            BadManifest(state), lease_id=lease["id"], expected_epoch=lease["epoch"], outputs=outputs
        )


def test_boolean_entry_size_cannot_masquerade_as_one_byte(tmp_path):
    from tessera_control.store import canonical_digest

    state, _, lease = _two_path_delivery(tmp_path)
    outputs = tuple(
        CollectedOutput(path, b"x", hashlib.sha256(b"x").hexdigest())
        for path in ("first.txt", "second.txt")
    )

    class BooleanSize(ApiChannel):
        def post(self, path, body, key):
            result = super().post(path, body, key)
            if path.endswith("/artifact-manifest"):
                result["artifacts"][0]["size"] = True
                canonical = {"version": 1}
                for name in (
                    "project_id",
                    "task_id",
                    "grant_id",
                    "lease_id",
                    "worker_principal_id",
                    "epoch",
                    "operation_id",
                    "plan_digest",
                    "artifacts",
                ):
                    canonical[name] = result[name]
                result["manifest_digest"] = canonical_digest(canonical)
            return result

    with pytest.raises(ArtifactTransferError, match="checksum"):
        upload_output_set(
            BooleanSize(state), lease_id=lease["id"], expected_epoch=lease["epoch"], outputs=outputs
        )
