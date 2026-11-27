"""A review pre-approval of a stage that reads another stage's output (a late-bound input) binds
the plan digest the preview derives from the reviewed contract's late-bind key; the plan the
runner proposes names the composed snapshot. The plan core is compared on the preview's basis
only when the composed snapshot is provably the checked binding of that key, so an exact
pre-approval clears the plan and anything else stays a ``digest_mismatch`` for a person.

Synthetic fixtures only: the real stores and runner, test doubles for the CPU host, executor
and guardian; no provider call.
"""

from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from salad_campaign_fixture import salad_provider_reset  # noqa: F401
from test_salad_late_bound_inputs import _stage_row, _to_salad_stage, _world, host  # noqa: F401

from tessera_control import campaign_preapproval as pre
from tessera_control import campaign_salad
from tessera_control.campaign_models import normalize, parse_spec_json
from tessera_control.campaign_runner import _Ctx
from tessera_control.store import _connect, utc_now

KEY = "g1"


@pytest.fixture
def world(tmp_path, host):  # noqa: F811
    world = _world(tmp_path, host)
    _to_salad_stage(world)
    world.tick_until_phase("awaiting_lease")
    world.lease_and_package()
    world.tick_until_phase("awaiting_review")
    return world


def _campaign(world):
    return world.row("SELECT * FROM campaigns WHERE id=?", world.campaign_id)


def _preview(world) -> dict:
    campaign = dict(_campaign(world))
    spec = normalize(parse_spec_json(campaign["spec_json"]))
    return world.driver.preview_review_digests(_Ctx(campaign, spec, None, 0), KEY, "salad")


def _plan(world) -> tuple[str, dict]:
    decision_id = world.salad()["plan_decision_id"]
    row = world.row("SELECT scope_json FROM decision_requests WHERE id=?", decision_id)
    return decision_id, json.loads(row["scope_json"])


def _basis(world, scope: dict) -> dict:
    with _connect(world.env.state / "tessera.sqlite3") as connection:
        return pre.late_bound_plan_basis(connection, _campaign(world), KEY, "salad", scope)


def _preapprove(world, plan_digest: str) -> None:
    """A pre-approval of the stage on its Salad candidate, as a delegation records it."""
    digests = _preview(world)
    approval = world.row(
        "SELECT id FROM campaign_approvals WHERE campaign_id=? AND status='active'",
        world.campaign_id,
    )
    expires = (datetime.now(UTC) + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    pre_id = f"pre_{uuid.uuid4().hex}"
    with world.runner.store.runner_connection() as connection:
        connection.execute(
            """INSERT INTO campaign_review_preapprovals (id, campaign_id, approval_id,
            stage_key, source_digest, plan_digest, source_use_digest, max_attempt_cents,
            max_total_cents, expires_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                pre_id,
                world.campaign_id,
                approval["id"],
                KEY,
                digests["source_assessment_digest"],
                plan_digest,
                digests["source_use_digest"],
                1000,
                1000,
                expires,
                utc_now(),
            ),
        )
        world.runner.store.record_runner_event(
            connection,
            _campaign(world),
            pre.DELEGATED_PREAPPROVAL_EVENT,
            {
                "campaign_id": world.campaign_id,
                "stage_key": KEY,
                "provider_id": "salad",
                "preapproval_id": pre_id,
            },
        )


def _evaluate(world, decision_id: str) -> pre.Verdict:
    approval = world.row(
        "SELECT id FROM campaign_approvals WHERE campaign_id=? AND status='active'",
        world.campaign_id,
    )
    with _connect(world.env.state / "tessera.sqlite3") as connection:
        return pre.evaluate(
            connection,
            campaign_id=world.campaign_id,
            approval_id=approval["id"],
            stage_key=KEY,
            kind=pre.KIND_PLAN,
            subject_id=decision_id,
        )


def test_the_runtime_plan_differs_from_the_preview_only_in_the_late_bound_identity(world):
    preview = _preview(world)
    _, scope = _plan(world)
    composed = _stage_row(world)["input_snapshot_digest"]
    identity = scope["evaluation_protocol"]["identity"]
    assert identity["data_digest"] == composed
    # The cause of the live decline: proposed as is, the plan core is not the previewed one.
    assert pre.review_core_digest(scope) != preview["plan_digest"]
    projected = _basis(world, scope)
    assert pre.review_core_digest(projected) == preview["plan_digest"]
    # Only the two late-bound identity values moved, to the reviewed contract's key form.
    spec_stage = normalize(parse_spec_json(_campaign(world)["spec_json"])).stage(KEY)
    from tessera_control.campaign_models import stage_on_candidate

    contract = campaign_salad.salad_config(stage_on_candidate(spec_stage, "salad")).workload
    moved = projected["evaluation_protocol"]["identity"]
    assert moved["data_digest"] == contract.input.snapshot_digest != composed
    rest = copy.deepcopy(projected)
    rest["evaluation_protocol"]["identity"] = identity
    assert rest == scope


def test_an_exact_preapproval_clears_the_late_bound_plan(world):
    preview = _preview(world)
    decision_id, _ = _plan(world)
    _preapprove(world, preview["plan_digest"])
    verdict = _evaluate(world, decision_id)
    assert verdict.reason is None
    assert verdict.digest == preview["plan_digest"]


def test_another_plan_digest_still_declines(world):
    decision_id, _ = _plan(world)
    _preapprove(world, "1" * 64)
    assert _evaluate(world, decision_id).reason == pre.DIGEST_MISMATCH


@pytest.mark.parametrize(
    "field,value",
    [("data_digest", "a" * 64), ("code_digest", "b" * 64)],
)
def test_an_identity_that_is_not_the_checked_binding_is_digested_as_proposed(world, field, value):
    _, scope = _plan(world)
    scope["evaluation_protocol"]["identity"][field] = value
    assert _basis(world, scope) == scope


def test_the_key_form_itself_is_not_accepted_as_the_runtime_identity(world):
    """A proposed plan that already names the unbound key (not what the runner binds) is not
    rewritten: only the checked composed snapshot maps to the key."""
    _, scope = _plan(world)
    projected = _basis(world, scope)
    assert _basis(world, projected) == projected
    assert _basis(world, projected) is projected


def test_without_the_recorded_late_bind_check_nothing_is_projected(world, monkeypatch):
    _, scope = _plan(world)
    monkeypatch.setattr(campaign_salad, "LATE_BOUND_EVENT", "campaign.not_the_late_bind_check")
    assert _basis(world, scope) == scope
    assert pre.review_core_digest(_basis(world, scope)) != _preview(world)["plan_digest"]


def test_a_snapshot_other_than_the_stage_row_is_not_projected(world):
    _, scope = _plan(world)
    with world.runner.store.runner_connection() as connection:
        connection.execute(
            "UPDATE campaign_stages SET input_snapshot_digest=? WHERE campaign_id=? "
            "AND stage_key=?",
            ("c" * 64, world.campaign_id, KEY),
        )
    assert _basis(world, scope) == scope
