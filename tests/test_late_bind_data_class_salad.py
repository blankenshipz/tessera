"""Late-bind routing on Salad for a chained, derived stage input (ADR 0140 decision 3); no network.

Owner invariant: non-commercial or restricted rows never reach Salad, including derived data
that arrives through a chained stage. The upstream stage runs on a trusted CPU host; the
dependent stage is placed on Salad (the ``SaladWorld`` doubles, no Salad call). Labels are
generic (``non-commercial``); the licence label is inherited from the upstream stage's input
snapshot. Complements test_salad_late_bound_inputs.py, which patches the resolver's labels.
"""

from __future__ import annotations

import copy

import pytest
from salad_campaign_fixture import salad_provider_reset  # noqa: F401
from test_salad_late_bound_inputs import (
    HOST,
    UP,
    _Host,
    _stage_row,
    _to_salad_stage,
    _world,
    host,  # noqa: F401
)

from tessera_control import late_bound_inputs, license_labels, provider_trust
from tessera_control.campaign_salad import LATE_BOUND_EVENT
from tessera_control.store import _connect, utc_now

RESTRICTED = "non-commercial"


def _label_upstream_snapshot(world, label=RESTRICTED):
    connection = _connect(world.env.state / "tessera.sqlite3")
    try:
        with connection:
            license_labels.add_snapshot_labels(
                connection,
                {"id": world.env.owner["id"], "org_id": world.env.owner["org_id"]},
                world.env.snapshot_id,
                [label],
                utc_now(),
            )
    finally:
        connection.close()


def _retrust(world, **salad):
    """Replace the Salad trust entry (installed and recorded, as an operator reload does)."""
    connection = _connect(world.env.state / "tessera.sqlite3")
    try:
        approved = provider_trust.current_policy(connection)
        if provider_trust._stored_document(connection, approved.digest) is None:
            # The setting the campaign was approved under, kept so the narrowing intersects it
            # (an approval digest with no stored document grants only public).
            with connection:
                provider_trust.record_policy(connection, approved, "operator", utc_now())
        document = copy.deepcopy(approved.document)
        document["providers"]["salad"].update(salad)
        policy = provider_trust.policy_from_document(document, "file")
        provider_trust.install_policy(policy)
        with connection:
            provider_trust.record_policy(connection, policy, "operator", utc_now())
    finally:
        connection.close()


def _bind_refusals(world):
    return world.events(late_bound_inputs.REFUSED_EVENT)


def _reserved(world):
    """Run until the dependent stage holds a reservation (it passed the reservation-time check)
    but has not started, i.e. before its inputs are bound."""
    world.tick_until(
        lambda: (
            world.row(
                "SELECT 1 FROM campaign_stage_attempts WHERE campaign_id=? AND stage_key='g1'",
                world.campaign_id,
            )
            is not None
        )
    )
    assert world.events(LATE_BOUND_EVENT) == []


def _failed(world):
    world.tick_until(lambda: _stage_row(world)["status"] == "failed")
    stop = world.row("SELECT stop_reason FROM campaigns WHERE id=?", world.campaign_id)
    seen = {
        e["event_type"]: e["payload_json"]
        for e in world.env.query(
            "SELECT event_type, payload_json FROM events WHERE event_type LIKE '%refus%'"
        )
    }
    assert world.events(LATE_BOUND_EVENT) == []
    assert world.row("SELECT 1 FROM campaign_salad_attempts") is None  # nothing released
    return stop["stop_reason"], seen


def _assert_bind_refused(world, code):
    reason, seen = _failed(world)
    assert reason == f"stage_failed:g1:late_bind_refused:{code}", (reason, seen)
    (event,) = _bind_refusals(world)
    assert event["code"] == code and event["provider"] == "salad", event
    return event


def test_a_restricted_label_is_refused_at_reservation_before_any_bind(tmp_path, host):  # noqa: F811
    world = _world(tmp_path, host, salad_labels=["unlabelled"])
    _label_upstream_snapshot(world)
    _to_salad_stage(world)
    reason, seen = _failed(world)
    assert reason == "stage_failed:g1:input_not_admitted"
    assert (
        "campaign.stage_license_refused" in seen
        and RESTRICTED in seen["campaign.stage_license_refused"]
    )


def test_an_upstream_restricted_snapshot_label_salad_does_not_trust_is_license_refused(
    tmp_path,
    host,  # noqa: F811
):
    world = _world(tmp_path, host, salad_labels=["unlabelled"])
    _to_salad_stage(world)
    _reserved(world)
    _label_upstream_snapshot(world)  # the lineage-inherited label the bind must see
    event = _assert_bind_refused(world, "license_refused")
    assert RESTRICTED in event["reason"]


def test_an_upstream_output_class_salad_is_not_trusted_for_is_data_class_refused(tmp_path, host):  # noqa: F811
    world = _world(tmp_path, host)
    _to_salad_stage(world)
    _reserved(world)
    _retrust(world, data_classes=["public"])
    _assert_bind_refused(world, "data_class_refused")


def test_trust_narrowed_after_approval_is_refused_at_bind(tmp_path, host):  # noqa: F811
    world = _world(tmp_path, host, salad_labels=[RESTRICTED, "unlabelled"])
    _label_upstream_snapshot(world)
    _to_salad_stage(world)
    _reserved(world)
    # Approved and reserved while the label was allowed; narrowed before the stage starts.
    _retrust(world, allowed_license_labels=["unlabelled"])
    _assert_bind_refused(world, "license_refused")


def test_an_allowed_label_and_class_binds_the_upstream_output_digest(tmp_path, host):  # noqa: F811
    world = _world(tmp_path, host, salad_labels=[RESTRICTED, "unlabelled"])
    _label_upstream_snapshot(world)
    _to_salad_stage(world)
    world.tick_until_phase("awaiting_lease")
    (bound,) = world.events(LATE_BOUND_EVENT)
    (item,) = bound["inputs"]
    upstream = world.row(
        "SELECT sha256 FROM campaign_stage_io WHERE campaign_id=? AND stage_key=? "
        "AND direction='output'",
        world.campaign_id,
        UP,
    )
    assert item["digest"] == upstream["sha256"]
    assert item["license_labels"] == [RESTRICTED]
    assert _bind_refusals(world) == []


def test_the_same_chain_on_a_local_provider_trusted_with_the_label_binds(tmp_path, host):  # noqa: F811
    # The rule is provider trust, not a blanket refusal: the host stage (the dependent's
    # sibling on a trusted local provider) receives the same labelled derived output.
    world = _world(tmp_path, host, salad_labels=["unlabelled"])
    _label_upstream_snapshot(world)
    connection = _connect(world.env.state / "tessera.sqlite3")
    try:
        campaign = dict(
            connection.execute(
                "SELECT * FROM campaigns WHERE id=?", (world.campaign_id,)
            ).fetchone()
        )
        from tessera_control.campaign_models import normalize, parse_spec_json

        spec = normalize(parse_spec_json(campaign["spec_json"]))
        stage = next(s for s in spec.stages if s.key == "g1")
        host_worker = next(
            p
            for p, entry in provider_trust.current_policy(connection).document["providers"].items()
            if p == HOST
        )
        host_principal = provider_trust.current_policy(connection).document["providers"][
            host_worker
        ]["worker_principal_ids"][0]
        resolved = late_bound_inputs.resolve_stage_inputs(
            connection,
            campaign["id"],
            stage,
            org_id=campaign["org_id"],
            project_id=campaign["project_id"],
        )
        (item,) = resolved
        assert item.license_labels == (RESTRICTED,)
        import dataclasses

        local = dataclasses.replace(stage, provider=HOST)
        assert late_bound_inputs.check_resolved_routing(
            connection, local, resolved, worker=host_principal
        ) == {RESTRICTED}
        with pytest.raises(late_bound_inputs.LateBindRefused) as caught:
            late_bound_inputs.check_resolved_routing(
                connection,
                dataclasses.replace(stage, provider="salad"),
                resolved,
                worker=world.worker["id"],
            )
        assert caught.value.code == "license_refused"
        assert caught.value.input_name == item.name
        assert caught.value.source_stage == item.source_stage == UP
    finally:
        connection.close()
    assert _Host  # the host provider stays registered for the whole test
