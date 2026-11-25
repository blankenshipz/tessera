"""A stored campaign receipt is used only when it hashes to its recorded digest.

Every reader of ``campaign_receipts`` (the receipt read, the campaign and window reports,
artifact and receipt_output chaining, image_from resolution and measurement gates) recomputes
``canonical_digest`` over the stored ``receipt_json`` and refuses on a mismatch with the stable
``receipt_digest_mismatch`` code, returning nothing from the stored body.
"""

from __future__ import annotations

import json

import pytest
from campaign_runner_harness import World
from test_campaign_gates import approve, approve_body, decide, finished_campaign
from test_campaign_image_build_spec import consumer as image_consumer
from test_campaign_image_build_spec import mark_built, registered, submit_world
from test_campaign_io import _finished, _project, _source
from test_campaign_receipt_chaining import _consumer, _submit
from test_campaign_standing import Sched

from tessera_control import campaign_gates, program_budget
from tessera_control import campaign_provider_worker as worker
from tessera_control.campaign_io import resolve_artifact
from tessera_control.campaign_receipt_read import (
    RECEIPT_DIGEST_MISMATCH,
    ReceiptDigestMismatch,
    load_verified_receipt,
)
from tessera_control.store import Conflict, _connect, canonical_digest

SENTINEL = "tampered-sentinel-7f3a"
RESTORE_TRIGGER = """CREATE TRIGGER IF NOT EXISTS campaign_receipts_no_update
    BEFORE UPDATE ON campaign_receipts
    BEGIN SELECT RAISE(ABORT,'campaign receipts are append-only'); END"""


def _tamper(state, campaign_id: str, *, body: bool = False, digest: bool = False) -> None:
    """Rewrite the stored row behind the append-only trigger (a test-only raw connection)."""
    connection = _connect(state / "tessera.sqlite3")
    try:
        connection.execute("DROP TRIGGER campaign_receipts_no_update")
        row = connection.execute(
            "SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", (campaign_id,)
        ).fetchone()
        if body:
            receipt = json.loads(row["receipt_json"])
            receipt["status"] = SENTINEL
            receipt["note"] = SENTINEL
            connection.execute(
                "UPDATE campaign_receipts SET receipt_json=? WHERE campaign_id=?",
                (json.dumps(receipt, sort_keys=True, separators=(",", ":")), campaign_id),
            )
        if digest:
            connection.execute(
                "UPDATE campaign_receipts SET receipt_digest=? WHERE campaign_id=?",
                ("0" * 64, campaign_id),
            )
        connection.execute(RESTORE_TRIGGER)
        connection.commit()
    finally:
        connection.close()


def _assert_closed(error: pytest.ExceptionInfo) -> None:
    assert isinstance(error.value, ReceiptDigestMismatch)
    assert isinstance(error.value, Conflict)  # 409 over HTTP and MCP
    assert error.value.code == RECEIPT_DIGEST_MISMATCH
    assert RECEIPT_DIGEST_MISMATCH in str(error.value)
    assert SENTINEL not in str(error.value)
    assert SENTINEL not in repr(error.value.args)


TAMPERS = [pytest.param({"body": True}, id="body"), pytest.param({"digest": True}, id="digest")]


@pytest.fixture
def chained(tmp_path):
    world = World(tmp_path)
    first, row = _finished(world)
    world.env.trust()
    worker.register_from_config({"local_docker": {"worker_principal_id": world.env.worker["id"]}})
    try:
        yield world, first, row
    finally:
        worker.register_from_config(None)


def _campaign_count(world) -> int:
    return world.row("SELECT COUNT(*) AS n FROM campaigns")["n"]


# ---- the shared reader ----


def test_a_valid_receipt_verifies_with_the_build_digest(chained):
    world, first, _row = chained
    stored = world.row("SELECT * FROM campaign_receipts WHERE campaign_id=?", first["id"])
    connection = _connect(world.env.state / "tessera.sqlite3")
    try:
        verified = load_verified_receipt(connection, first["id"])
        assert load_verified_receipt(connection, "campaign_missing") is None
    finally:
        connection.close()
    assert verified.receipt == json.loads(stored["receipt_json"])
    assert verified.digest == stored["receipt_digest"] == canonical_digest(verified.receipt)
    assert verified.created_at == stored["created_at"]


@pytest.mark.parametrize("tamper", TAMPERS)
def test_the_shared_reader_refuses_a_tampered_row(chained, tamper):
    world, first, _row = chained
    _tamper(world.env.state, first["id"], **tamper)
    connection = _connect(world.env.state / "tessera.sqlite3")
    try:
        with pytest.raises(ReceiptDigestMismatch) as error:
            load_verified_receipt(connection, first["id"])
    finally:
        connection.close()
    _assert_closed(error)


# ---- the receipt read (HTTP GET /campaigns/{id}/receipt and the MCP tool) ----


def test_receipt_read_returns_a_valid_receipt_unchanged(chained):
    world, first, _row = chained
    env = world.env
    stored = world.row("SELECT * FROM campaign_receipts WHERE campaign_id=?", first["id"])
    read = env.store.get_campaign_receipt(env.owner, first["id"])
    assert read == {
        "campaign_id": first["id"],
        "receipt": json.loads(stored["receipt_json"]),
        "receipt_digest": stored["receipt_digest"],
        "created_at": stored["created_at"],
    }


@pytest.mark.parametrize("tamper", TAMPERS)
def test_receipt_read_refuses_a_tampered_receipt(chained, tamper):
    world, first, _row = chained
    _tamper(world.env.state, first["id"], **tamper)
    with pytest.raises(Conflict) as error:
        world.env.store.get_campaign_receipt(world.env.owner, first["id"])
    _assert_closed(error)


# ---- the campaign report and the window report ----


def _window(world):
    return world.env.store.get_campaign_window_report(
        world.env.owner, world.env.project["id"], "2000-01-01T00:00:00Z", "2999-01-01T00:00:00Z"
    )


def test_reports_use_a_valid_receipt_and_its_digest(chained):
    world, first, _row = chained
    digest = world.row(
        "SELECT receipt_digest FROM campaign_receipts WHERE campaign_id=?", first["id"]
    )["receipt_digest"]
    report = world.env.store.get_campaign_report(world.env.owner, first["id"], detail=True)
    assert digest in json.dumps(report)
    assert _window(world)["campaigns"]


@pytest.mark.parametrize("tamper", TAMPERS)
def test_reports_refuse_a_tampered_receipt(chained, tamper):
    world, first, _row = chained
    _tamper(world.env.state, first["id"], **tamper)
    with pytest.raises(Conflict) as error:
        world.env.store.get_campaign_report(world.env.owner, first["id"], detail=True)
    _assert_closed(error)
    with pytest.raises(Conflict) as error:
        _window(world)
    _assert_closed(error)


# ---- artifact inputs and receipt_output chaining ----


def test_artifact_resolution_uses_a_valid_receipt(chained):
    world, first, row = chained
    with world.runner.store.runner_connection() as connection:
        resolved = resolve_artifact(connection, _project(world), _source(first, row))
    assert resolved.sha256 == row["sha256"]


@pytest.mark.parametrize("tamper", TAMPERS)
def test_artifact_resolution_refuses_a_tampered_source_receipt(chained, tamper):
    world, first, row = chained
    _tamper(world.env.state, first["id"], **tamper)
    with pytest.raises(Conflict) as error:
        with world.runner.store.runner_connection() as connection:
            resolve_artifact(connection, _project(world), _source(first, row))
    _assert_closed(error)


@pytest.mark.parametrize("tamper", TAMPERS)
def test_receipt_output_chaining_refuses_a_tampered_source_receipt(chained, tamper):
    world, first, row = chained
    valid = _submit(world, _consumer(world, row, campaign_id=first["id"]), key="before")
    assert valid["spec"]["stages"][0]["inputs"][1]["sha256"] == row["sha256"]
    _tamper(world.env.state, first["id"], **tamper)
    before = _campaign_count(world)
    for key, ref, predecessors in (
        ("by-id", {"campaign_id": first["id"]}, None),
        ("by-name", {"predecessor": "weights"}, {"weights": first["id"]}),
    ):
        with pytest.raises(Conflict) as error:
            _submit(world, _consumer(world, row, **ref), predecessors, key=key)
        _assert_closed(error)
    assert _campaign_count(world) == before


# ---- image_from ----


@pytest.fixture
def image_chained(tmp_path):
    world = World(tmp_path)
    first, _row = _finished(world)
    world.env.trust()
    worker.register_from_config(registered(world.env))
    try:
        yield world, first
    finally:
        worker.register_from_config(None)


@pytest.mark.parametrize("tamper", TAMPERS)
def test_image_from_refuses_a_tampered_build_receipt(image_chained, tamper):
    world, first = image_chained
    digest = mark_built(world, first)
    # mark_built drops the append-only trigger; put it back so _tamper can drop it again.
    connection = _connect(world.env.state / "tessera.sqlite3")
    try:
        connection.execute(RESTORE_TRIGGER)
        connection.commit()
    finally:
        connection.close()
    valid = submit_world(world, image_consumer(world.env, campaign_id=first["id"]), key="ok")
    assert valid["spec"]["stages"][0]["image_resolved_from"]["source_receipt_sha256"] == digest
    _tamper(world.env.state, first["id"], **tamper)
    before = _campaign_count(world)
    with pytest.raises(Conflict) as error:
        submit_world(world, image_consumer(world.env, campaign_id=first["id"]), key="bad")
    _assert_closed(error)
    assert _campaign_count(world) == before


# ---- measurement gates (ADR 0051) ----


@pytest.fixture
def sched(tmp_path):
    program_budget.reset()
    campaign_gates.configure_go_authority(accept_automation_labelled=False)
    yield Sched(tmp_path)
    program_budget.reset()
    campaign_gates.configure_go_authority(accept_automation_labelled=False)


def _write_receipt(sched, campaign_id, value) -> None:
    attempt = {"attempt": 1, "status": "succeeded", "measurements": [{"name": "v", "value": value}]}
    receipt = {"stages": [{"stage_key": "s1", "attempts": [attempt]}]}
    sched.env.execute(
        "INSERT INTO campaign_receipts VALUES (?, ?, ?, 'x')",
        campaign_id,
        json.dumps(receipt, sort_keys=True, separators=(",", ":")),
        canonical_digest(receipt),
    )


def _set_value(sched, campaign_id, value) -> None:
    connection = _connect(sched.env.state / "tessera.sqlite3")
    try:
        connection.execute("DROP TRIGGER campaign_receipts_no_update")
        row = connection.execute(
            "SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", (campaign_id,)
        ).fetchone()
        receipt = json.loads(row["receipt_json"])
        receipt["stages"][0]["attempts"][0]["measurements"][0]["value"] = value
        connection.execute(
            "UPDATE campaign_receipts SET receipt_json=? WHERE campaign_id=?",
            (json.dumps(receipt, sort_keys=True, separators=(",", ":")), campaign_id),
        )
        connection.execute(RESTORE_TRIGGER)
        connection.commit()
    finally:
        connection.close()


def _gate(sched, named):
    requirement = [
        {
            "campaign_id": named,
            "accept_human_go": True,
            "measurement": {"stage": "s1", "name": "v", "op": "ge", "value": 1.0},
        }
    ]
    submitted = sched.env.submit(spec=sched.spec())
    return approve(sched, submitted, approve_body(submitted, unlock=requirement))


def test_a_valid_measurement_receipt_still_unlocks(sched):
    named = finished_campaign(sched)
    _write_receipt(sched, named, 1.0)
    decide(sched, named)
    code, result = _gate(sched, named)
    assert code == 200 and result["status"] == "running"


def test_a_measurement_raised_in_place_refuses_the_gate(sched):
    named = finished_campaign(sched)
    _write_receipt(sched, named, 0.0)  # the recorded value fails the condition
    decide(sched, named)
    with pytest.raises(Conflict, match="measurement_condition_false"):
        _gate(sched, named)
    _set_value(sched, named, 5.0)  # rewritten to pass: the digest no longer matches
    with pytest.raises(Conflict, match="measurement_receipt_digest_mismatch") as error:
        _gate(sched, named)
    assert "5.0" not in str(error.value)


def test_a_rewritten_measurement_digest_refuses_the_gate(sched):
    named = finished_campaign(sched)
    _write_receipt(sched, named, 1.0)
    decide(sched, named)
    _tamper(sched.env.state, named, digest=True)
    with pytest.raises(Conflict, match="measurement_receipt_digest_mismatch"):
        _gate(sched, named)
