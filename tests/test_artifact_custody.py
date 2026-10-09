from __future__ import annotations

import base64
import hashlib
import sqlite3
from pathlib import Path

import pytest
from test_artifact_manifests import _body, _completed_outputs, _operation
from test_artifacts import _payload, _two_path_delivery

from tessera_control.artifact_custody import ArtifactCustodyStore
from tessera_control.artifact_dispositions import ArtifactDispositionStore
from tessera_control.artifact_manifests import ArtifactManifestStore
from tessera_control.artifacts import ArtifactStore
from tessera_control.experiments import ExperimentStore
from tessera_control.project_artifact_store import ProjectArtifactStore
from tessera_control.store import Conflict, Forbidden, Store, canonical_digest


def _released_manifest(tmp_path: Path):
    state, worker, lease, first, second = _completed_outputs(tmp_path)
    owner_token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    owner = Store(state).authenticate(owner_token)
    placement = {
        "expected_epoch": lease["epoch"],
        "operation_id": first["operation_id"],
        "artifacts": [
            {name: artifact[name] for name in ("id", "sha256", "size")}
            for artifact in (first, second)
        ],
    }
    ProjectArtifactStore(state).commit_project_artifacts(
        worker, lease["id"], "place", canonical_digest(placement), placement
    )
    body = _body(lease, first, second)
    _, manifest = ArtifactManifestStore(state).create_manifest(
        worker, lease["id"], "manifest", canonical_digest(body), body
    )
    disposition = {
        "expected_manifest_digest": manifest["manifest_digest"],
        "expected_revision": 0,
        "disposition": "retain_quarantined",
        "rationale": "retain bounded output under quarantine",
    }
    ArtifactDispositionStore(state).record_disposition(
        owner, manifest["id"], "retain", canonical_digest(disposition), disposition
    )
    release = {
        "expected_manifest_digest": manifest["manifest_digest"],
        "expected_revision": 0,
        "action": "release",
        "rationale": "researcher may inspect private retained output",
    }
    custody = ArtifactCustodyStore(state)
    custody.record_release(owner, manifest["id"], "release", canonical_digest(release), release)
    return state, owner, worker, manifest, first, second, custody


def test_released_private_output_preview_preserves_quarantine_and_bundle_snapshot(
    tmp_path: Path,
) -> None:
    _state, owner, _worker, manifest, first, second, custody = _released_manifest(tmp_path)

    released = custody.get_released_manifest(owner, manifest["id"])
    preview = custody.preview(owner, manifest["id"], first["id"], 3)
    retrieved = custody.retrieve(owner, manifest["id"], first["id"])
    request = {"max_bytes": 1024 * 1024}
    status, exported = custody.export_bundle(
        owner, manifest["id"], "export", canonical_digest(request), request
    )

    assert released["manifest"]["status"] == "quarantined"
    assert released["manifest"]["classification"] == "unclassified"
    assert preview == {
        "artifact_id": first["id"],
        "relative_path": "first.txt",
        "sha256": first["sha256"],
        "size": first["size"],
        "preview_base64": "ZGly",
        "preview_bytes": 3,
        "truncated": True,
        "status": "quarantined",
        "classification": "unclassified",
    }
    assert retrieved["content"] == b"direct"
    assert retrieved["sha256"] == first["sha256"]
    assert status == 201
    assert [item["id"] for item in exported["bundle"]["outputs"]] == [first["id"], second["id"]]
    assert any(
        item["kind"] in {"input_bytes", "input_snapshot"}
        for item in exported["bundle"]["missing_materials"]
    )
    assert custody.get_bundle(owner, exported["id"]) == exported
    assert exported["bundle"]["completeness"] == "bounded_private_reproduction_material"
    assert {
        item["kind"]: item["reason"].split(":", 1)[0]
        for item in exported["bundle"]["missing_materials"]
    } == {
        "input_bytes": "not_exported",
        "skill_material": "restricted",
        "environment_image": "missing",
    }


def test_custody_release_is_human_gated_and_revoke_fences_all_retrieval(tmp_path: Path) -> None:
    _state, owner, worker, manifest, first, _second, custody = _released_manifest(tmp_path)

    with pytest.raises(Forbidden):
        custody.preview(worker, manifest["id"], first["id"], 10)
    revoke = {
        "expected_manifest_digest": manifest["manifest_digest"],
        "expected_revision": 1,
        "action": "revoke",
        "rationale": "end researcher byte access",
    }
    custody.record_release(owner, manifest["id"], "revoke", canonical_digest(revoke), revoke)
    with pytest.raises(Forbidden):
        custody.get_released_manifest(owner, manifest["id"])
    with pytest.raises(Forbidden):
        custody.preview(owner, manifest["id"], first["id"], 10)


def test_bundle_rejects_too_small_limit_and_replays_an_immutable_snapshot(tmp_path: Path) -> None:
    _state, owner, _worker, manifest, _first, _second, custody = _released_manifest(tmp_path)
    small = {"max_bytes": 1024}
    with pytest.raises(Conflict, match="byte cap"):
        custody.export_bundle(owner, manifest["id"], "small", canonical_digest(small), small)

    request = {"max_bytes": 1024 * 1024}
    first = custody.export_bundle(
        owner, manifest["id"], "export", canonical_digest(request), request
    )
    replay = custody.export_bundle(
        owner, manifest["id"], "export", canonical_digest(request), request
    )
    assert replay == first


def test_bundle_replay_rechecks_current_custody_after_revocation(tmp_path: Path) -> None:
    _state, owner, _worker, manifest, _first, _second, custody = _released_manifest(tmp_path)
    request = {"max_bytes": 1024 * 1024}
    first = custody.export_bundle(
        owner, manifest["id"], "export", canonical_digest(request), request
    )
    revoke = {
        "expected_manifest_digest": manifest["manifest_digest"],
        "expected_revision": 1,
        "action": "revoke",
        "rationale": "revoke access before export replay",
    }
    custody.record_release(owner, manifest["id"], "revoke", canonical_digest(revoke), revoke)

    with pytest.raises(Forbidden):
        custody.export_bundle(owner, manifest["id"], "export", canonical_digest(request), request)
    assert first[0] == 201


def test_interrupted_transfer_restart_reaches_released_quarantine_bundle(
    tmp_path: Path,
) -> None:
    state, worker, lease = _two_path_delivery(tmp_path)
    owner_token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    owner = Store(state).authenticate(owner_token)
    content = b"resumed output bytes"
    direct = _payload(lease, b"first output")
    direct["relative_path"] = "first.txt"
    artifacts = ArtifactStore(state)
    _, first = artifacts.upload_artifact(
        worker, lease["id"], "first", canonical_digest(direct), direct
    )
    begin = {
        "expected_epoch": lease["epoch"],
        "operation_id": _operation(lease),
        "relative_path": "second.txt",
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }
    _, upload = artifacts.begin_upload(worker, lease["id"], "begin", canonical_digest(begin), begin)
    split = len(content) // 2
    first_chunk = {"offset": 0, "content_base64": base64.b64encode(content[:split]).decode()}
    artifacts.append_chunk(
        worker,
        lease["id"],
        upload["id"],
        "first-chunk",
        canonical_digest(first_chunk),
        first_chunk,
    )

    # A new store instance models the control process reopening its durable state.
    restarted = ArtifactStore(state)
    status = restarted.upload_status(
        worker, lease["id"], upload["id"], "status", canonical_digest({})
    )[1]
    assert {key: status[key] for key in ("id", "status", "received_bytes", "size")} == {
        "id": upload["id"],
        "status": "uploading",
        "received_bytes": split,
        "size": len(content),
    }
    second_chunk = {
        "offset": split,
        "content_base64": base64.b64encode(content[split:]).decode(),
    }
    restarted.append_chunk(
        worker,
        lease["id"],
        upload["id"],
        "second-chunk",
        canonical_digest(second_chunk),
        second_chunk,
    )
    _, second = restarted.complete_upload(
        worker, lease["id"], upload["id"], "complete", canonical_digest({})
    )
    placement = {
        "expected_epoch": lease["epoch"],
        "operation_id": _operation(lease),
        "artifacts": [
            {name: artifact[name] for name in ("id", "sha256", "size")}
            for artifact in (first, second)
        ],
    }
    ProjectArtifactStore(state).commit_project_artifacts(
        worker, lease["id"], "place", canonical_digest(placement), placement
    )
    body = _body(lease, first, second)
    _, manifest = ArtifactManifestStore(state).create_manifest(
        worker, lease["id"], "manifest", canonical_digest(body), body
    )
    disposition = {
        "expected_manifest_digest": manifest["manifest_digest"],
        "expected_revision": 0,
        "disposition": "retain_quarantined",
        "rationale": "Keep resumed fixture output quarantined for private review.",
    }
    ArtifactDispositionStore(state).record_disposition(
        owner, manifest["id"], "retain", canonical_digest(disposition), disposition
    )
    release = {
        "expected_manifest_digest": manifest["manifest_digest"],
        "expected_revision": 0,
        "action": "release",
        "rationale": "Allow fixture owner to verify resumed private output bytes.",
    }
    custody = ArtifactCustodyStore(state)
    custody.record_release(owner, manifest["id"], "release", canonical_digest(release), release)
    bundle_request = {"max_bytes": 1024 * 1024}
    _, bundle = custody.export_bundle(
        owner, manifest["id"], "bundle", canonical_digest(bundle_request), bundle_request
    )

    assert custody.retrieve(owner, manifest["id"], second["id"])["content"] == content
    assert manifest["status"] == "quarantined"
    assert manifest["classification"] == "unclassified"
    assert bundle["bundle"]["outputs"][1]["content_base64"] == base64.b64encode(content).decode()
    assert bundle["bundle"]["completeness"] == "bounded_private_reproduction_material"


@pytest.mark.parametrize(
    ("outcome", "expected_label"),
    [
        ("succeeded", "succeeded"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
        ("unknown", "inconclusive"),
    ],
)
def test_bundle_preserves_non_success_attempt_evidence(
    tmp_path: Path, outcome: str, expected_label: str
) -> None:
    state, owner, _worker, manifest, _first, _second, custody = _released_manifest(tmp_path)
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        grant = dict(
            connection.execute(
                "SELECT * FROM execution_grants WHERE id=?", (manifest["grant_id"],)
            ).fetchone()
        )
    design = {
        "goal": "Preserve every bounded operational outcome.",
        "success_criterion": "A human can inspect retained evidence without relabeling it.",
        "protocol": "One bounded synthetic baseline.",
        "variants": [
            {
                "kind": "baseline",
                "name": "baseline",
                "method_digest": "a" * 64,
                "input_digest": "b" * 64,
                "metadata": {"synthetic": True},
                "execution_task_id": grant["task_id"],
                "execution_plan_digest": grant["plan_digest"],
            },
            {
                "kind": "intervention",
                "name": "intervention",
                "method_digest": "c" * 64,
                "input_digest": "d" * 64,
                "metadata": {"synthetic": True},
                "execution_task_id": grant["task_id"],
                "execution_plan_digest": grant["plan_digest"],
            },
        ],
    }
    records = ExperimentStore(state)
    _, experiment = records.create_experiment(
        owner, grant["project_id"], f"experiment-{outcome}", canonical_digest(design), design
    )
    baseline = experiment["design"]["variants"][0]
    admission = {
        "expected_variant_revision": baseline["revision"],
        "expected_design_revision": experiment["current_design_revision"],
        "grant_id": grant["id"],
    }
    _, attempt = records.admit_attempt(
        owner, baseline["id"], f"admit-{outcome}", canonical_digest(admission), admission
    )
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            """UPDATE research_experiment_admitted_attempts
            SET state=?,outcome=?,reason=?,resolved_by_principal_id=? WHERE id=?""",
            (
                outcome,
                outcome,
                f"Recorded {expected_label} output evidence.",
                owner["id"],
                attempt["id"],
            ),
        )
    if outcome in {"failed", "cancelled"}:
        reject = {
            "expected_manifest_digest": manifest["manifest_digest"],
            "expected_revision": 1,
            "disposition": "reject",
            "rationale": f"Do not use {outcome} output as a result.",
        }
        ArtifactDispositionStore(state).record_disposition(
            owner, manifest["id"], f"reject-{outcome}", canonical_digest(reject), reject
        )
        retain = {
            "expected_manifest_digest": manifest["manifest_digest"],
            "expected_revision": 2,
            "disposition": "retain_quarantined",
            "rationale": f"Retain {outcome} output for bounded custody review only.",
        }
        ArtifactDispositionStore(state).record_disposition(
            owner, manifest["id"], f"retain-{outcome}", canonical_digest(retain), retain
        )
    request = {"max_bytes": 1024 * 1024}
    _, exported = custody.export_bundle(
        owner, manifest["id"], f"export-{outcome}", canonical_digest(request), request
    )
    evidence = exported["bundle"]["attempts"]
    assert len(evidence) == 1
    assert evidence[0]["outcome"] == evidence[0]["state"] == outcome
    assert exported["bundle"]["manifest"]["status"] == "quarantined"
    assert exported["bundle"]["manifest"]["classification"] == "unclassified"
    dispositions = exported["bundle"]["decisions"]["manifest_dispositions"]
    expected_dispositions = ["retain_quarantined"]
    if outcome in {"failed", "cancelled"}:
        expected_dispositions.extend(["reject", "retain_quarantined"])
    assert [item["disposition"] for item in dispositions] == expected_dispositions
