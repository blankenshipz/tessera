"""Data class guard (ADR 0039): only staged data declared public reaches third-party GPUs.

Synthetic state only; no provider, network or credential is contacted.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from contextlib import closing

import pytest
from data_class_fixtures import restore_class_guards, rewrite_class_row
from fastapi.testclient import TestClient
from test_artifact_custody import _released_manifest
from test_chunked_snapshots import _bytes_of, _chunk_body, _inventory
from test_dataset_staging import _body as _staging_body
from test_salad_input_path import (
    PUBLIC_INPUT,
    _approved_input_route,
    input_contract,
    setup_contract,
    staged_dir,
)
from test_salad_live_check import LocalSession, drive
from test_salad_live_setup import inputs, private_file, setup_script  # noqa: F401

from tessera_control.api import create_app
from tessera_control.data_class import (
    DataClassDeclaration,
    require_public_input,
    snapshot_data_class,
    target_is_third_party,
)
from tessera_control.dataset_staging import DatasetStagingStore
from tessera_control.execution_targets import get_target
from tessera_control.salad_workload_admission import check_workload_admission
from tessera_control.snapshot_chunks import ChunkedSnapshotStore
from tessera_control.snapshots import SnapshotStore
from tessera_control.store import Conflict, Forbidden, StoreError, _connect
from tessera_control.workspace import capture_workspace, inventory_digest

PUBLIC = {"data_class": "public", "license": "CC0-1.0", "source": "https://example.org/data"}
FILES = {"a.txt": b"public text", "b/c.bin": b"\x00\x01public"}


def _labelled(owner):
    """Owner-operated automation, as salad_live_setup sends it (never a bare owner)."""
    return dict(owner, automation_label="data-class-test")


def _bare(owner):
    """The root owner without an automation label (conftest labels owners by default)."""
    return {k: v for k, v in owner.items() if k != "automation_label"}


def _chunked(store, actor, project_id, files, key, *, begin=None, complete=None):
    manifest, digest = _inventory(files)
    body = {"expected_digest": digest, "inventory": manifest}
    if begin is not None:
        body["data_class"] = begin
    status, begun = store.begin_upload(actor, project_id, key + "-begin", body)
    assert status == 201
    for planned in begun["chunk_plan"]:
        store.put_chunk(
            actor,
            project_id,
            begun["upload_id"],
            planned["seq"],
            _chunk_body(_bytes_of(files, planned)),
        )
    payload = None if complete is None else {"data_class": complete}
    return store.complete_upload(actor, project_id, begun["upload_id"], key + "-done", payload)


def _selection_for(digest):
    from test_salad_http import selection
    from test_salad_workload_contract import contract as base_contract
    from test_salad_workload_contract import execution

    value = selection().model_dump(mode="json")
    value["manifests"]["data"] = digest
    work = base_contract(
        input=dict(kind="staged_snapshot", snapshot_digest=digest, max_bytes=1 << 20)
    )
    value["salad"]["execution"] = execution(work)
    return value


# --------------------------------------------------------------------------- model


def test_declaration_requires_evidence_for_public_and_never_defaults_public():
    assert DataClassDeclaration.model_validate(PUBLIC).data_class == "public"
    assert DataClassDeclaration(data_class="private_derived").license is None
    for missing in ("license", "source"):
        with pytest.raises(ValueError, match="evidence"):
            DataClassDeclaration.model_validate({k: v for k, v in PUBLIC.items() if k != missing})
    for bad in ("NOASSERTION", "none", "Unknown"):
        with pytest.raises(ValueError, match="identified license"):
            DataClassDeclaration.model_validate(dict(PUBLIC, license=bad))
    for bad_source in ("has space", "", "x" * 2049):
        with pytest.raises(ValueError):
            DataClassDeclaration.model_validate(dict(PUBLIC, source=bad_source))
    with pytest.raises(ValueError):
        DataClassDeclaration.model_validate(dict(PUBLIC, data_class="internal"))
    with pytest.raises(ValueError):
        DataClassDeclaration.model_validate(dict(PUBLIC, inferred=True))


def test_third_party_compute_is_a_target_property_and_unknown_targets_fail_closed():
    assert get_target("salad_container").third_party_compute is True
    assert get_target("salad_inference").third_party_compute is True
    assert get_target("kubernetes").third_party_compute is False
    assert target_is_third_party("salad_container") is True
    assert target_is_third_party("kubernetes") is False
    assert target_is_third_party("never-registered") is True


# ------------------------------------------------------------------------- staging


def test_chunked_staging_records_public_only_with_evidence_and_a_labelled_human(tmp_path):
    state, owner, _w, manifest, *_ = _released_manifest(tmp_path)
    project_id = manifest["project_id"]
    store = ChunkedSnapshotStore(state)
    actor = _labelled(owner)
    status, snap = _chunked(store, actor, project_id, FILES, "pub", begin=PUBLIC)
    assert status == 201
    record = snap["data_class"]
    assert record["data_class"] == "public" and record["origin"] == "human_declared"
    assert (record["license"], record["source"]) == ("CC0-1.0", "https://example.org/data")
    assert record["decision_class"] == "owner_automation_labelled"
    assert record["actor_principal_id"] == owner["id"]
    assert store.get_snapshot(owner, snap["id"])["data_class"] == record
    # No declaration: private_derived by default, never public.
    _, plain = _chunked(store, actor, project_id, {"p.txt": b"plain"}, "plain")
    assert plain["data_class"]["data_class"] == "private_derived"
    assert plain["data_class"]["origin"] == "default"
    # A public claim without evidence is refused at staging, before any upload exists.
    manifest_body, digest = _inventory({"q.txt": b"q"})
    for bad in (
        {"data_class": "public"},
        {"data_class": "public", "license": "CC0-1.0"},
        dict(PUBLIC, license="NOASSERTION"),
    ):
        with pytest.raises(StoreError):
            store.begin_upload(
                actor,
                project_id,
                "bad-" + json.dumps(bad),
                {"expected_digest": digest, "inventory": manifest_body, "data_class": bad},
            )
    # A bare owner credential cannot make the public decision.
    with pytest.raises(Forbidden):
        store.begin_upload(
            _bare(owner),
            project_id,
            "bare",
            {"expected_digest": digest, "inventory": manifest_body, "data_class": PUBLIC},
        )


def test_chunked_completion_may_declare_but_never_change_the_begin_declaration(tmp_path):
    state, owner, _w, manifest, *_ = _released_manifest(tmp_path)
    project_id = manifest["project_id"]
    store = ChunkedSnapshotStore(state)
    actor = _labelled(owner)
    _, late = _chunked(store, actor, project_id, {"l.txt": b"late"}, "late", complete=PUBLIC)
    assert late["data_class"]["data_class"] == "public"
    with pytest.raises(Conflict, match="differs"):
        _chunked(
            store,
            actor,
            project_id,
            {"m.txt": b"m"},
            "change",
            begin=PUBLIC,
            complete={"data_class": "private_derived"},
        )
    _, same = _chunked(
        store, actor, project_id, {"n.txt": b"n"}, "same", begin=PUBLIC, complete=PUBLIC
    )
    assert same["data_class"]["data_class"] == "public"


def test_http_staging_refuses_public_without_evidence(tmp_path):
    state, owner, _w, manifest, *_ = _released_manifest(tmp_path)
    client = TestClient(create_app(state))
    token = (state / "owner.header").read_text().strip().split(": ", 1)[1]
    headers = {"Authorization": token, "X-Tessera-Automation": "data-class-test"}
    manifest_body, digest = _inventory({"h.txt": b"http"})
    path = f"/projects/{manifest['project_id']}/chunked-snapshots"
    refused = client.post(
        path,
        headers=headers | {"Idempotency-Key": "no-evidence"},
        json={
            "expected_digest": digest,
            "inventory": manifest_body,
            "data_class": {"data_class": "public"},
        },
    )
    assert refused.status_code in {400, 422}
    begun = client.post(
        path,
        headers=headers | {"Idempotency-Key": "evidence"},
        json={"expected_digest": digest, "inventory": manifest_body, "data_class": PUBLIC},
    )
    assert begun.status_code == 201, begun.text
    upload = begun.json()["upload_id"]
    for planned in begun.json()["chunk_plan"]:
        data = _bytes_of({"h.txt": b"http"}, planned)
        assert client.put(
            f"{path}/{upload}/chunks/{planned['seq']}",
            headers=headers,
            json={
                "content_base64": base64.b64encode(data).decode(),
                "sha256": hashlib.sha256(data).hexdigest(),
            },
        ).status_code in {200, 201}
    done = client.post(f"{path}/{upload}/complete", headers=headers | {"Idempotency-Key": "c"})
    assert done.status_code == 201, done.text
    assert done.json()["data_class"]["data_class"] == "public"
    got = client.get(f"/snapshots/{upload}", headers=headers).json()
    assert got["data_class"]["data_class"] == "public"


def test_legacy_adoption_defaults_private_and_accepts_an_evidenced_declaration(tmp_path):
    state, owner, _w, manifest, *_ = _released_manifest(tmp_path)
    root = tmp_path / "legacy-input"
    root.mkdir()
    (root / "data.txt").write_bytes(b"legacy")
    capture = capture_workspace(str(root))
    digest = inventory_digest(capture.inventory)
    store = SnapshotStore(state)
    project_id = manifest["project_id"]
    _, private = store.adopt_snapshot(owner, project_id, "default", digest, capture)
    assert private["data_class"]["data_class"] == "private_derived"
    with pytest.raises(StoreError):
        store.adopt_snapshot(
            _labelled(owner),
            project_id,
            "no-ev",
            digest,
            capture,
            data_class={"data_class": "public"},
        )
    with pytest.raises(Forbidden):
        store.adopt_snapshot(_bare(owner), project_id, "bare", digest, capture, data_class=PUBLIC)
    _, public = store.adopt_snapshot(
        _labelled(owner), project_id, "pub", digest, capture, data_class=PUBLIC
    )
    assert public["data_class"]["data_class"] == "public"
    with closing(_connect(state / "tessera.sqlite3")) as db:
        event = db.execute(
            "SELECT payload_json FROM events WHERE event_type='workspace.snapshot_adopted' "
            "AND json_extract(payload_json,'$.snapshot_id')=?",
            (public["id"],),
        ).fetchone()[0]
    assert json.loads(event)["data_class"]["decision_class"] == "owner_automation_labelled"


def test_snapshots_from_run_outputs_are_always_private_derived(tmp_path):
    state, owner, _w, manifest, *_ = _released_manifest(tmp_path)
    store = DatasetStagingStore(state)
    body = _staging_body(state, manifest)
    # No API field can declare a derived snapshot public.
    with pytest.raises(StoreError):
        store.stage(
            _labelled(owner), manifest["project_id"], "claim", dict(body, data_class=PUBLIC)
        )
    status, snap = store.stage(_labelled(owner), manifest["project_id"], "stage", body)
    assert status == 201
    assert snap["data_class"]["data_class"] == "private_derived"
    assert snap["data_class"]["origin"] == "derived"
    assert snap["staging"]["data_class"] == "private_derived"
    assert store.get_staging(owner, snap["id"])["data_class"] == "private_derived"
    with closing(_connect(state / "tessera.sqlite3")) as db:
        assert snapshot_data_class(db, snap["id"])["data_class"] == "private_derived"
        # The recorded class is immutable: there is no reclassification path.
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(
                "UPDATE snapshot_data_classes SET data_class='public' WHERE snapshot_id=?",
                (snap["id"],),
            )


# ----------------------------------------------------------------------- admission


def test_admission_refuses_private_and_derived_inputs_and_admits_public(tmp_path, trust_policy):
    state, owner, _w, manifest, *_ = _released_manifest(tmp_path)
    project_id = manifest["project_id"]
    actor = _labelled(owner)
    chunked = ChunkedSnapshotStore(state)
    _, derived = DatasetStagingStore(state).stage(
        actor, project_id, "derived", _staging_body(state, manifest)
    )
    _, private = _chunked(chunked, actor, project_id, {"p.txt": b"private"}, "private")
    _, public = _chunked(chunked, actor, project_id, {"o.txt": b"open"}, "public", begin=PUBLIC)
    with closing(_connect(state / "tessera.sqlite3")) as db:
        for snap in (derived, private):
            with pytest.raises(ValueError, match="public"):
                check_workload_admission(db, project_id, _selection_for(snap["digest"]))
        check_workload_admission(db, project_id, _selection_for(public["digest"]))
        # ADR 0040: no target is exempt. Under the builtin default every provider receives
        # public data only; an operator file binds a worker principal to a provider.
        with pytest.raises(ValueError, match="public"):
            require_public_input(db, "kubernetes", private["id"], "worker-k8s")
        with pytest.raises(ValueError, match="public"):
            require_public_input(db, "salad_container", private["id"])
        require_public_input(db, "kubernetes", public["id"])
        trust_policy({"kubernetes": (["public", "private_derived"], ["worker-k8s"])})
        require_public_input(db, "kubernetes", private["id"], "worker-k8s")
        with pytest.raises(ValueError, match="public"):  # unbound principal: public only
            require_public_input(db, "kubernetes", private["id"], "worker-other")
        with pytest.raises(ValueError, match="public"):  # bound to another provider
            require_public_input(db, "salad_container", private["id"], "worker-k8s")
        # A snapshot without a recorded class reads as private_derived (fail closed).
        rewrite_class_row(db, public["id"])
        restore_class_guards(db)
        assert snapshot_data_class(db, public["id"])["data_class"] == "private_derived"
        with pytest.raises(ValueError, match="public"):
            check_workload_admission(db, project_id, _selection_for(public["digest"]))


def _set_class(state, snapshot_id, data_class, origin):
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        rewrite_class_row(db, snapshot_id)
        if origin is not None:
            db.execute(
                "INSERT INTO snapshot_data_classes (snapshot_id,data_class,origin,recorded_at) "
                "VALUES (?,?,?,'2026-01-01T00:00:00Z')",
                (snapshot_id, data_class, origin),
            )
        restore_class_guards(db)


def test_salad_route_refuses_non_public_input_at_admission_and_ticket_issue(
    inputs,  # noqa: F811
    capsys,
    monkeypatch,
):
    from tessera_control.remote_execution_models import RemoteProposal

    session, summary = _approved_input_route(inputs, capsys, monkeypatch)
    route = summary["records"]["route_id"]
    try:
        state = session.state(route)
        proposal = RemoteProposal.model_validate(state["proposal"])
        server = session.server
        request = {"lease_id": proposal.lease_id, "expected_epoch": proposal.epoch}
        with sqlite3.connect(inputs["state"] / "tessera.sqlite3") as db:
            (snapshot_id,) = db.execute(
                "SELECT g.snapshot_id FROM external_operations o JOIN execution_grants g "
                "ON g.id=o.grant_id WHERE o.operation_id=?",
                (proposal.operation_id,),
            ).fetchone()
        assert session.input_data_class(route) == "public"
        # Public with evidence: admitted and a ticket can be minted.
        server.store.preflight_authority(server.worker, route, proposal, "create")
        ticket = server.inputs.issue_ticket(server.worker, proposal.operation_id, request)
        assert ticket["snapshot_id"] == snapshot_id
        for data_class, origin in (
            ("private_derived", "derived"),
            ("private_derived", "default"),
            ("private_derived", "migrated"),
            (None, None),  # unrecorded
        ):
            _set_class(inputs["state"], snapshot_id, data_class, origin)
            assert session.input_data_class(route) == "private_derived"
            with pytest.raises(Conflict, match="public"):
                server.store.preflight_authority(server.worker, route, proposal, "create")
            with pytest.raises(Forbidden, match="public"):
                server.inputs.issue_ticket(server.worker, proposal.operation_id, request)
            with sqlite3.connect(inputs["state"] / "tessera.sqlite3") as db:
                with pytest.raises(ValueError, match="public"):
                    check_workload_admission(
                        db, state["project_id"], state["proposal"]["selection"]
                    )
        # Stop authority is never gated by the input's class.
        server.store.preflight_authority(server.worker, route, proposal, "stop")
    finally:
        session.close()


# ------------------------------------------------------------------------ receipts


def test_live_check_receipt_carries_input_and_output_classes():
    session = LocalSession()
    session.expects_input = True
    session.input_data_class = lambda route: "public"
    receipt = drive(session)
    assert receipt["input_data_class"] == "public"
    assert receipt["output_data_class"] == "private_derived"
    plain = drive(LocalSession())
    assert plain["input_data_class"] is None and plain["output_data_class"] == "private_derived"


def test_worker_run_receipt_carries_input_and_output_classes(tmp_path):
    from test_execution_run_receipts import _observed_fixture, _report_unresolved, _run_body

    from tessera_control.execution_run_receipts import ExecutionRunReceiptStore

    state, owner, worker, grant, lease, checkpoint = _observed_fixture(tmp_path)
    status, receipt = _report_unresolved(state, worker, grant, checkpoint, _run_body(lease))
    assert status == 201
    assert receipt["input_data_class"] == "private_derived"
    assert receipt["output_data_class"] == "private_derived"
    with closing(_connect(state / "tessera.sqlite3")) as db:
        payload = json.loads(
            db.execute(
                "SELECT payload_json FROM events "
                "WHERE event_type='worker.execution_run_receipt_recorded'"
            ).fetchone()[0]
        )
    assert payload["input_data_class"] == "private_derived"
    assert payload["output_data_class"] == "private_derived"
    _set_class(state, grant["snapshot_id"], None, None)
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute(
            """INSERT INTO snapshot_data_classes (snapshot_id, data_class, origin, license,
            source, actor_principal_id, decision_class, recorded_at) VALUES
            (?, 'public', 'human_declared', 'CC0-1.0', 'https://example.org/data', ?,
            'owner_automation_labelled', '2026-01-01T00:00:00Z')""",
            (grant["snapshot_id"], owner["id"]),
        )
    read = ExecutionRunReceiptStore(state).get_run_receipt(owner, checkpoint["operation_id"])
    assert read["input_data_class"] == "public"
    assert read["output_data_class"] == "private_derived"


# ---------------------------------------------------------------------- setup script


def _setup_argv(inputs, extra):  # noqa: F811
    _, raw = input_contract("0" * 64)
    directory = staged_dir(inputs["root"], raw)
    value, _ = setup_contract(directory, raw)
    path = private_file(inputs["root"] / "contract.json", json.dumps(value).encode())
    return inputs["argv"] + [
        "--workload-contract",
        str(path),
        "--input-dir",
        str(directory),
        *extra,
    ]


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ([], "--data-class public"),
        (["--data-class", "private_derived"], "--data-class public"),
        (["--data-class", "public", "--source", "https://example.org/d"], "--license"),
        (["--data-class", "public", "--license", "CC0-1.0"], "--source"),
        (
            ["--data-class", "public", "--license", "NOASSERTION", "--source", "x:y"],
            "identified --license",
        ),
        (["--license", "CC0-1.0", "--source", "x:y"], "--data-class public"),
    ],
)
def test_setup_refuses_to_stage_for_salad_without_a_public_declaration(
    inputs,  # noqa: F811
    capsys,
    extra,
    message,
):
    assert setup_script.main(_setup_argv(inputs, extra)) == 1
    assert message in capsys.readouterr().err
    assert not inputs["output"].exists()
    with sqlite3.connect(inputs["state"] / "tessera.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM source_snapshots").fetchone()[0] == 0


def test_setup_refuses_a_declaration_without_an_input_dir(inputs, capsys):  # noqa: F811
    argv = inputs["argv"] + PUBLIC_INPUT
    assert setup_script.main(argv) == 1
    assert "apply only to --input-dir" in capsys.readouterr().err
    assert not inputs["output"].exists()


def test_setup_records_the_declared_public_input(inputs, capsys, monkeypatch):  # noqa: F811
    session, summary = _approved_input_route(inputs, capsys, monkeypatch)
    try:
        data = next(
            r for r in summary["review_required"] if r["field"] == "selection.manifests.data"
        )
        assert "license CC0-1.0" in data["basis"] and "recorded data class public" in data["basis"]
        assert "decision_class owner_automation_labelled" in data["basis"]
        with closing(_connect(inputs["state"] / "tessera.sqlite3")) as db:
            rows = db.execute(
                "SELECT data_class, origin, decision_class FROM snapshot_data_classes d "
                "JOIN source_snapshots s ON s.id=d.snapshot_id"
            ).fetchall()
        # The marker-free generic route stages exactly one snapshot: the declared input.
        assert [tuple(r) for r in rows] == [
            ("public", "human_declared", "owner_automation_labelled")
        ]
    finally:
        session.close()


def test_public_rows_must_carry_human_evidence_at_the_table_level(tmp_path):
    state, *_ = _released_manifest(tmp_path)
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO snapshot_data_classes (snapshot_id,data_class,origin,recorded_at) "
                "VALUES ('y','public','default','t')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO snapshot_data_classes (snapshot_id,data_class,origin,license,"
                "source,recorded_at) VALUES ('z','public','human_declared','MIT','s','t')"
            )


def test_recorded_class_cannot_be_deleted_or_replaced(tmp_path):
    state, owner, _w, manifest, *_ = _released_manifest(tmp_path)
    store = ChunkedSnapshotStore(state)
    _, snap = _chunked(store, _labelled(owner), manifest["project_id"], {"d": b"d"}, "d")
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM snapshot_data_classes WHERE snapshot_id=?", (snap["id"],))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(
                """INSERT OR REPLACE INTO snapshot_data_classes (snapshot_id, data_class,
                origin, license, source, actor_principal_id, decision_class, recorded_at)
                VALUES (?, 'public', 'human_declared', 'MIT', 'x:y', ?, 'human_session', 't')""",
                (snap["id"], owner["id"]),
            )
        assert db.execute(
            "SELECT data_class, origin FROM snapshot_data_classes WHERE snapshot_id=?",
            (snap["id"],),
        ).fetchone() == ("private_derived", "default")
    # A begin-time declaration of an upload that never completes may still be discarded.
    manifest_body, digest = _inventory({"never.txt": b"never"})
    _, begun = store.begin_upload(
        _labelled(owner),
        manifest["project_id"],
        "never",
        {"expected_digest": digest, "inventory": manifest_body, "data_class": PUBLIC},
    )
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute("DELETE FROM snapshot_data_classes WHERE snapshot_id=?", (begun["upload_id"],))


def _begin_public(store, actor, project_id, inventory, digest, key):
    return store.begin_upload(
        actor,
        project_id,
        key,
        {"expected_digest": digest, "inventory": inventory, "data_class": PUBLIC},
    )


def test_public_declaration_cannot_relabel_derived_or_migrated_bytes(tmp_path):
    from dataclasses import asdict

    from test_dataset_staging import _entries

    from tessera_control.dataset_staging import staged_inventory

    state, owner, _w, manifest, *_ = _released_manifest(tmp_path)
    project_id = manifest["project_id"]
    actor = _labelled(owner)
    _, derived = DatasetStagingStore(state).stage(
        actor, project_id, "derived", _staging_body(state, manifest)
    )
    # The same bytes and inventory, re-uploaded as a "public" chunked snapshot.
    entries = _entries(dict(manifest, _state=str(state)))
    inventory = json.loads(json.dumps(asdict(staged_inventory(manifest["id"], entries))))
    store = ChunkedSnapshotStore(state)
    with pytest.raises(Conflict, match="cannot be declared public"):
        _begin_public(store, actor, project_id, inventory, derived["digest"], "launder")
    # Migrated bytes are refused the same way, at begin and at completion.
    files = {"old.txt": b"pre-v62 bytes"}
    _, old = _chunked(store, actor, project_id, files, "old")
    _set_class(state, old["id"], "private_derived", "migrated")
    old_inventory, old_digest = _inventory(files)
    with pytest.raises(Conflict, match="cannot be declared public"):
        _begin_public(store, actor, project_id, old_inventory, old_digest, "launder-old")
    with pytest.raises(Conflict, match="cannot be declared public"):
        _chunked(store, actor, project_id, files, "late-launder", complete=PUBLIC)


def test_admission_prefers_a_public_restage_of_default_private_bytes(tmp_path):
    state, owner, _w, manifest, *_ = _released_manifest(tmp_path)
    project_id = manifest["project_id"]
    actor = _labelled(owner)
    store = ChunkedSnapshotStore(state)
    files = {"same.txt": b"same bytes"}
    _, first = _chunked(store, actor, project_id, files, "first")
    assert first["data_class"]["origin"] == "default"
    with closing(_connect(state / "tessera.sqlite3")) as db:
        with pytest.raises(ValueError, match="public"):
            check_workload_admission(db, project_id, _selection_for(first["digest"]))
    # A default (undeclared) staging is not laundering: the owner may declare it now.
    _, second = _chunked(store, actor, project_id, files, "second", begin=PUBLIC)
    assert second["digest"] == first["digest"]
    assert second["data_class"]["data_class"] == "public"
    with closing(_connect(state / "tessera.sqlite3")) as db:
        check_workload_admission(db, project_id, _selection_for(first["digest"]))
