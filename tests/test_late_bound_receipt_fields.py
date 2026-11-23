"""Receipt fields for late-bound chained inputs (ADR 0140 amendment); fake worker, no network.

Each bound input records its upstream stage, artifact digest, bytes and the binding time (the
``campaign.stage_inputs_bound`` event's own timestamp). A refused or missing binding is explicit
on the dependent stage's receipt as ``input_not_pinned`` with the closed refusal code."""

from __future__ import annotations

import json

from test_late_bound_stage_inputs import _chain, _world, ran  # noqa: F401

from tessera_control import late_bound_inputs as late_bound
from tessera_control.late_bound_inputs import LateBindRefused
from tessera_control.store import _connect


def _receipt(world, campaign_id) -> dict:
    row = world.row("SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", campaign_id)
    return json.loads(row["receipt_json"])


def test_bound_input_records_stage_digest_bytes_and_binding_time(ran):  # noqa: F811
    world, campaign, _spec = ran
    stages = {s["stage_key"]: s for s in _receipt(world, campaign["id"])["stages"]}
    (entry,) = stages["b"]["input"][late_bound.LAUNCH_INPUTS_KEY]
    output = world.row(
        "SELECT sha256, size_bytes FROM campaign_stage_io WHERE campaign_id=? "
        "AND stage_key='a' AND direction='output'",
        campaign["id"],
    )
    (event,) = world.env.query(
        "SELECT created_at FROM events WHERE event_type=?", late_bound.BOUND_EVENT
    )
    assert entry["source_stage"] == "a"
    assert entry["digest"] == output["sha256"]
    assert entry["bytes"] == output["size_bytes"]
    assert entry["bound_at"] == event["created_at"]
    assert "bound_at" not in late_bound.launch_payload(())  # launch inputs stay unchanged
    assert late_bound.LAUNCH_INPUTS_KEY not in stages["a"]["input"]


def test_refused_binding_is_explicit_as_input_not_pinned(tmp_path, monkeypatch):
    world = _world(tmp_path)

    def refuse(*_args, **_kwargs):
        raise LateBindRefused(
            "upstream_not_settled", "test refusal /secret/path", input_name="up", source_stage="a"
        )

    monkeypatch.setattr(late_bound, "resolve_stage_inputs", refuse)
    campaign = world.campaign(_chain(world.env))
    world.tick_all(campaign["id"])
    stages = {s["stage_key"]: s for s in _receipt(world, campaign["id"])["stages"]}
    assert stages["b"]["input"][late_bound.LAUNCH_INPUTS_KEY] == [
        {
            "status": "input_not_pinned",
            "code": "upstream_not_settled",
            "input": "up",
            "source_stage": "a",
        }
    ]
    assert "secret" not in json.dumps(stages["b"]["input"])
    assert late_bound.LAUNCH_INPUTS_KEY not in stages["a"]["input"]
    (event,) = [e["payload"] for e in world.events_of(late_bound.REFUSED_EVENT)]
    assert event["source_stage"] == "a"


def test_a_refused_bind_after_an_earlier_bind_shows_both(ran):  # noqa: F811
    """A stage that bound (its local attempt) and then recorded a bind refusal (as a later
    alternate would) shows both on the receipt: the bound entry, then ``input_not_pinned``.
    Retries on the same provider reuse the pinned binding, so the refusal is recorded directly."""
    from tessera_control.campaign_receipts import _bound_section

    world, campaign, _spec = ran
    connection = _connect(world.env.state / "tessera.sqlite3")
    try:
        with connection:
            world.env.store.record_runner_event(
                connection,
                campaign,
                late_bound.REFUSED_EVENT,
                {
                    "stage_key": "b",
                    "code": "license_refused",
                    "input": "up",
                    "source_stage": "a",
                    "reason": "test refusal /secret/path",
                },
            )
        section = _bound_section(connection, campaign, "b")
    finally:
        connection.close()
    bound, refused = section[late_bound.LAUNCH_INPUTS_KEY]
    assert bound["source_stage"] == "a" and "bound_at" in bound and "status" not in bound
    assert refused == {
        "status": "input_not_pinned",
        "code": "license_refused",
        "input": "up",
        "source_stage": "a",
    }
    assert "secret" not in json.dumps(section)
