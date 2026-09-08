from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_artifacts import _two_path_delivery
from test_input_delivery import _delivery
from test_runtime import ApiChannel

from tessera_control.artifact_dispositions import ArtifactDispositionStore
from tessera_control.store import _connect, canonical_digest
from tessera_worker.sealed_outputs import OutputSealError, SealedOutputJournal


def _terminated(
    channel: ApiChannel, lease: dict[str, object], **change: object
) -> dict[str, object]:
    body: dict[str, object] = {
        "lease_id": lease["id"],
        "expected_epoch": lease["epoch"],
        "observed_state": "exited",
        "observed_at": datetime.now(UTC).isoformat(),
        "provider_id": "synthetic-container",
        "termination_confirmed": True,
    }
    body.update(change)
    return channel.post(
        "/worker/observations", body, "sealed-observation-" + datetime.now(UTC).isoformat()
    )


def _journal(tmp_path: Path) -> SealedOutputJournal:
    root = tmp_path / "synthetic-runtime"
    root.mkdir(mode=0o700)
    return SealedOutputJournal(root)


def test_synthetic_seal_resumes_lost_manifest_response_after_worker_restart(tmp_path: Path) -> None:
    state, _, lease = _two_path_delivery(tmp_path)
    workspace = tmp_path / "stopped-workspace"
    workspace.mkdir()
    (workspace / "first.txt").write_bytes(b"first sealed bytes")
    (workspace / "second.txt").write_bytes(b"second sealed bytes")
    channel = ApiChannel(state)
    journal = _journal(tmp_path)
    seal_id = journal.seal(
        workspace=workspace,
        lease_id=lease["id"],
        expected_epoch=lease["epoch"],
        output_paths=["first.txt", "second.txt"],
        max_output_bytes=1024,
        termination_observation=_terminated(channel, lease),
    )

    class LostManifest(ApiChannel):
        def post(self, path, body, key):
            response = super().post(path, body, key)
            if path.endswith("/artifact-manifest"):
                raise ConnectionError("synthetic lost manifest response")
            return response

    with pytest.raises(ConnectionError, match="lost manifest"):
        journal.resume(LostManifest(state), seal_id)
    (workspace / "first.txt").write_bytes(b"changed after seal")

    # A replacement process only uses the durable immutable snapshot; it does
    # not reread the changed workspace while recovering a lost response.
    restarted = SealedOutputJournal(tmp_path / "synthetic-runtime")
    receipt = restarted.resume(ApiChannel(state), seal_id)
    assert receipt["status"] == "quarantined"
    assert receipt["classification"] == "unclassified"
    assert restarted.resume(ApiChannel(state), seal_id) == receipt
    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM artifact_manifests").fetchone()[0] == 1
        assert tuple(
            connection.execute(
                "SELECT status, classification, late FROM artifact_manifests"
            ).fetchone()
        ) == ("quarantined", "unclassified", 0)
        assert dict(
            connection.execute("SELECT relative_path, content FROM artifacts").fetchall()
        ) == {
            "first.txt": b"first sealed bytes",
            "second.txt": b"second sealed bytes",
        }
    with pytest.raises(OutputSealError, match="changed after sealing"):
        restarted.seal(
            workspace=workspace,
            lease_id=lease["id"],
            expected_epoch=lease["epoch"],
            output_paths=["first.txt", "second.txt"],
            max_output_bytes=1024,
            termination_observation=_terminated(channel, lease),
        )


@pytest.mark.parametrize(
    "change",
    [
        {"termination_confirmed": False},
        {"observed_state": "running"},
        {"provider_id": ""},
        {"expected_epoch": 999},
    ],
)
def test_seal_requires_fresh_matching_termination_before_collecting(
    tmp_path: Path, change: dict[str, object]
) -> None:
    journal = _journal(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    # A missing declared file would produce a different error if collection ran.
    observation = {
        "id": "observation_synthetic",
        "lease_id": "lease_synthetic",
        "epoch": 1,
        "observed_state": "exited",
        "observed_at": datetime.now(UTC).isoformat(),
        "provider_id": "provider_synthetic",
        "termination_confirmed": True,
        "observation_proof": "trusted_worker_assertion",
        "operational_only": True,
    }
    if "expected_epoch" in change:
        observation["epoch"] = change.pop("expected_epoch")
    observation.update(change)
    with pytest.raises(OutputSealError, match="termination"):
        journal.seal(
            workspace=workspace,
            lease_id="lease_synthetic",
            expected_epoch=1,
            output_paths=["missing.txt"],
            max_output_bytes=10,
            termination_observation=observation,
        )


def test_seal_rejects_stale_observation_missing_outputs_and_conflicting_declaration(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "result.txt").write_bytes(b"sealed")
    observation = {
        "id": "observation_synthetic",
        "lease_id": "lease_synthetic",
        "epoch": 1,
        "observed_state": "exited",
        "observed_at": (datetime.now(UTC) - timedelta(minutes=6)).isoformat(),
        "provider_id": "provider_synthetic",
        "termination_confirmed": True,
        "observation_proof": "trusted_worker_assertion",
        "operational_only": True,
    }
    with pytest.raises(OutputSealError, match="Fresh matching"):
        journal.seal(
            workspace=workspace,
            lease_id="lease_synthetic",
            expected_epoch=1,
            output_paths=["result.txt"],
            max_output_bytes=10,
            termination_observation=observation,
        )
    observation["observed_at"] = datetime.now(UTC).isoformat()
    with pytest.raises(OutputSealError, match="unavailable"):
        journal.seal(
            workspace=workspace,
            lease_id="lease_synthetic",
            expected_epoch=1,
            output_paths=["missing.txt"],
            max_output_bytes=10,
            termination_observation=observation,
        )
    seal_id = journal.seal(
        workspace=workspace,
        lease_id="lease_synthetic",
        expected_epoch=1,
        output_paths=["result.txt"],
        max_output_bytes=10,
        termination_observation=observation,
    )
    assert seal_id.startswith("output_seal_")
    with pytest.raises(OutputSealError, match="conflicts"):
        journal.seal(
            workspace=workspace,
            lease_id="lease_synthetic",
            expected_epoch=1,
            output_paths=["result.txt"],
            max_output_bytes=9,
            termination_observation=observation,
        )


def test_undeclared_or_late_sealed_outputs_remain_quarantined(tmp_path: Path) -> None:
    state, _, lease = _two_path_delivery(tmp_path)
    channel = ApiChannel(state)
    undeclared_workspace = tmp_path / "undeclared-workspace"
    undeclared_workspace.mkdir()
    (undeclared_workspace / "undeclared.txt").write_bytes(b"not in the plan")
    undeclared_journal = _journal(tmp_path)
    undeclared_seal = undeclared_journal.seal(
        workspace=undeclared_workspace,
        lease_id=lease["id"],
        expected_epoch=lease["epoch"],
        output_paths=["undeclared.txt"],
        max_output_bytes=1024,
        termination_observation=_terminated(channel, lease),
    )
    with pytest.raises(ConnectionError, match="409"):
        undeclared_journal.resume(channel, undeclared_seal)

    # A current termination observation can be followed by expiry before upload.
    # Control retains attributable late bytes in quarantine, never as a result.
    late_workspace = tmp_path / "late-workspace"
    late_workspace.mkdir()
    (late_workspace / "first.txt").write_bytes(b"late first")
    (late_workspace / "second.txt").write_bytes(b"late second")
    late_state_root = tmp_path / "late-state"
    late_state_root.mkdir()
    state, _, lease = _two_path_delivery(late_state_root)
    channel = ApiChannel(state)
    late_root = tmp_path / "late-runtime"
    late_root.mkdir(mode=0o700)
    late_journal = SealedOutputJournal(late_root)
    late_seal = late_journal.seal(
        workspace=late_workspace,
        lease_id=lease["id"],
        expected_epoch=lease["epoch"],
        output_paths=["first.txt", "second.txt"],
        max_output_bytes=1024,
        termination_observation=_terminated(channel, lease),
    )
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE execution_leases SET expires_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), lease["id"]),
        )
    receipt = late_journal.resume(channel, late_seal)
    assert receipt["status"] == "quarantined" and receipt["classification"] == "unclassified"
    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT late FROM artifact_manifests").fetchone()[0] == 1


def test_sealed_output_and_retain_disposition_never_accept_or_release_results(
    tmp_path: Path,
) -> None:
    state, _, owner, _, grant, lease = _delivery(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "result.txt").write_bytes(b"synthetic result")
    channel = ApiChannel(state)
    journal = _journal(tmp_path)
    seal_id = journal.seal(
        workspace=workspace,
        lease_id=lease["id"],
        expected_epoch=lease["epoch"],
        output_paths=["result.txt"],
        max_output_bytes=1024,
        termination_observation=_terminated(channel, lease),
    )
    manifest = journal.resume(channel, seal_id)
    disposition = {
        "expected_manifest_digest": manifest["manifest_digest"],
        "expected_revision": 0,
        "disposition": "retain_quarantined",
        "rationale": "synthetic bytes are not a result",
    }
    ArtifactDispositionStore(state).record_disposition(
        owner, manifest["id"], "retain-synthetic", canonical_digest(disposition), disposition
    )
    with _connect(state / "tessera.sqlite3") as connection:
        assert tuple(
            connection.execute(
                "SELECT status, classification FROM artifact_manifests WHERE id=?",
                (manifest["id"],),
            ).fetchone()
        ) == ("quarantined", "unclassified")
        assert (
            connection.execute(
                "SELECT status FROM execution_grants WHERE id=?", (grant["id"],)
            ).fetchone()[0]
            == "active"
        )
