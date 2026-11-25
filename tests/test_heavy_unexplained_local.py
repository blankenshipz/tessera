# ruff: noqa: F811
"""A heavy stage lands on a local provider only with an explicit fallback reason (ADR 0107
amendment 2026-11-23b). Fakes only: no provider call, no network, no money.

Explained (placed, recorded as before): Salad unavailable (no profile, credential, capacity,
infrastructure), a cap, a trust or licence refusal, an operator's own placement decision, or the
ADR 0141 record saying why no Salad candidate was added. Unexplained (``not_listed`` with no
record, ``remote_not_tried``): refused with ``heavy_local_unexplained`` and the stage fails,
unless the operator set ``heavy_unexplained_local: warn``.
"""

from __future__ import annotations

import json

import pytest
from salad_campaign_fixture import salad_provider_reset, salad_spec_v2  # noqa: F401
from test_campaign_placement_default import _cpu_world  # noqa: F401
from test_campaign_placement_salad import NEAR, _placed, near  # noqa: F401
from test_execution import _worker
from test_salad_stage_adapter import _Capacity, cpu_world, spec_of

from tessera_control import campaign_heavy_guard as guard
from tessera_control import campaign_heavy_guard_readout as readout
from tessera_control import campaign_placement as placement
from tessera_control import campaign_placement_preference as preference
from tessera_control import campaign_provider_worker as worker
from tessera_control import program_budget
from tessera_control.campaign_provider_salad import SaladRouteProvider
from tessera_control.campaign_providers import register_provider, unregister_provider
from tessera_control.store import Conflict

GIB = 1024**3
FAR = "far-cpu"
HEAVY = {"placement_tags": ["heavy"]}
CODE = "heavy_local_unexplained"


@pytest.fixture(autouse=True)
def _reset():
    preference.configure(None, ())
    program_budget.reset()
    yield
    preference.configure(None, ())
    program_budget.reset()


@pytest.fixture
def far():
    def make(world) -> None:
        env = world.env
        principal = _worker(env.state, env.owner, env.project["id"], "far-token")
        register_provider(_Capacity(worker._descriptor(FAR, principal["id"], None, 1)))

    before = _Capacity.slots
    _Capacity.slots = 1  # far-cpu has a free slot (this class serves only far-cpu here)
    yield make
    _Capacity.slots = before
    unregister_provider(FAR)


def _single(world, **over):
    """The world's stage with no alternates (one local provider: the single-provider path)."""
    stage = {k: v for k, v in world.spec["stages"][0].items() if k != "alternate_providers"}
    world.spec = salad_spec_v2(world.env, [{**stage, **over}])


def _stop_reason(world):
    return world.row("SELECT stop_reason FROM campaigns WHERE id=?", world.campaign_id)[
        "stop_reason"
    ]


def _receipt_entry(world):
    from tessera_control.campaign_receipts import build_receipt

    with world.env.store.runner_connection() as connection:
        campaign = connection.execute(
            "SELECT * FROM campaigns WHERE id=?", (world.campaign_id,)
        ).fetchone()
        receipt, _ = build_receipt(connection, campaign, "running")
    return receipt["stages"][0]["attempts"][0]


def _status(world):
    with world.env.store.runner_connection() as connection:
        return readout.status_view(connection, world.env.project["id"], world.campaign_id)


def _salad_not_enrolled(monkeypatch):
    def refuse(self, connection, project_id, stage):
        raise Conflict("Salad stage names an unknown operator profile.")

    monkeypatch.setattr(SaladRouteProvider, "check_stage", refuse)


# -- classification ----------------------------------------------------------------------------


def test_explained_reason_codes():
    assert guard.EXPLAINED_REASONS == {
        "no_remote_profile",
        "credential_unavailable",
        "no_fitting_profile",
        "remote_failed_infra",
        "remote_skipped",
        "operator_override",
        # ADR 0169: the operator's allocation-instability rule moved the remote class behind.
        "allocation_unstable",
        "remote_candidate_not_added",
    }
    assert guard.explained(["remote_skipped"])
    assert guard.explained(["allocation_unstable"])
    assert guard.explained(["not_listed", "operator_override"])
    assert not guard.explained(["not_listed"])
    assert not guard.explained(["remote_not_tried"])
    assert not guard.explained([])


def test_not_listed_is_explained_only_by_a_not_added_record():
    record = {"added": False, "reason": "over_operator_cap", "provider": "salad", "signals": []}
    assert guard.explained(["not_listed"], record)
    for reason in ("candidate_refused", "egress_bound", "scratch_requested", "no_fitting_profile"):
        assert guard.explained(["not_listed"], {**record, "reason": reason})
    assert not guard.explained(["not_listed"], {**record, "reason": "something_new"})
    added = {**record, "added": True, "reason": "operator_heavy_remote_candidate"}
    assert not guard.explained(["not_listed"], added)
    # The recorded guard block or its detail list carry the record's reason too.
    entry = {"provider_id": "salad", "reason": "over_stage_cap", "source": guard.RECORD_SOURCE}
    assert guard.explained(["not_listed"], {"reasons": ["not_listed"], "detail": [entry]})
    assert guard.explained(["not_listed"], [entry])
    # A placement skip entry (no record source) never explains not_listed.
    assert not guard.explained(["not_listed"], [{"provider_id": "salad", "reason": "untrusted"}])
    assert not guard.explained(["remote_not_tried"], record)


def test_reasons_name_the_record_for_a_stage_with_no_remote(local_providers_two):
    stage, kinds = local_providers_two
    record = {"added": False, "reason": "program_budget", "provider": "salad", "signals": []}
    view = guard.Guard(("tag:heavy",), "warn", kinds, remote_record=record)
    assert guard.reasons(view, stage, NEAR, (), None, None) == (
        ["remote_candidate_not_added"],
        [{"provider_id": "salad", "reason": "program_budget", "source": "heavy_remote_candidate"}],
    )
    unknown = guard.Guard(("tag:heavy",), "warn", kinds, remote_record={**record, "reason": "x"})
    codes, detail = guard.reasons(unknown, stage, NEAR, (), None, None)
    assert codes == ["not_listed"] and detail[0]["reason"] == "x"
    assert not guard.explained(codes, detail)


@pytest.fixture
def local_providers_two():
    from test_campaign_placement_default import _normal

    for provider_id in (NEAR, FAR):
        register_provider(
            worker.WorkerGrantProvider(worker._descriptor(provider_id, "p1", None, 1))
        )
    stage = _normal(order=(FAR,), placement_tags=["heavy"])
    kinds = {pid: preference.candidate_kind(stage, pid) for pid in stage.candidate_ids}
    assert kinds == {NEAR: "local", FAR: "local"}
    yield stage, kinds
    for provider_id in (NEAR, FAR):
        unregister_provider(provider_id)


def test_the_operator_setting_defaults_to_refuse_and_is_validated():
    assert preference.heavy_unexplained_local() == "refuse"
    preference.configure({"heavy_unexplained_local": "warn"}, ())
    assert preference.heavy_unexplained_local() == "warn"
    for bad in ("block", True, None, 1):
        with pytest.raises(ValueError):
            preference.configure({"heavy_unexplained_local": bad}, ())
        assert preference.active() is None


# -- refused: nothing explains the local placement ----------------------------------------------


def test_a_one_provider_heavy_stage_with_no_record_is_refused_and_fails(tmp_path, near):
    world = _cpu_world(tmp_path, near, HEAVY)
    _single(world)
    world.start()
    world.tick(5)
    assert world.attempt() is None and not _placed(world)
    assert not world.events(guard.EVENT)  # no fallback was used
    (event,) = world.events(guard.REFUSED_EVENT)
    assert event == {
        "campaign_id": world.campaign_id,
        "stage_key": "g1",
        "attempt": 1,
        "provider_id": NEAR,
        "mode": "warn",
        "signals": ["tag:heavy"],
        "reasons": ["not_listed"],
        "detail": [],
        "code": CODE,
        "unexplained_local": "refuse",
    }
    assert _stop_reason(world) == f"stage_failed:g1:{CODE}"
    stage = world.row("SELECT status FROM campaign_stages WHERE campaign_id=?", world.campaign_id)
    assert stage["status"] == "failed"
    held = world.row("SELECT held_cents FROM campaigns WHERE id=?", world.campaign_id)
    assert held["held_cents"] == 0
    (entry,) = _status(world)
    assert entry["mode"] == "refuse" and entry["code"] == CODE and entry["waiting"] is False
    assert entry["reasons"] == ["not_listed"] and entry["provider_id"] == NEAR


def test_two_local_candidates_and_no_record_are_refused_on_the_placement_path(tmp_path, near, far):
    world = _cpu_world(tmp_path, near, HEAVY)
    far(world)
    stage = world.spec["stages"][0]
    world.spec = salad_spec_v2(
        world.env,
        [{**stage, "alternate_providers": [{"provider": FAR, "runtime": "docker-cpu-v1"}]}],
    )
    world.start()
    world.tick(5)
    assert world.attempt() is None and not _placed(world)
    (final,) = world.events(placement.WAITING_EVENT)
    assert [(s["provider_id"], s["reason"], s["detail"]) for s in final["skipped"]] == [
        (NEAR, CODE, "not_listed"),
        (FAR, CODE, "not_listed"),
    ]
    assert final["final"] is True
    (event,) = world.events(guard.REFUSED_EVENT)
    assert event["code"] == CODE and event["reasons"] == ["not_listed"]
    assert event["unexplained_local"] == "refuse"
    assert _stop_reason(world) == f"stage_failed:g1:{CODE}"
    assert not world.events(guard.EVENT)


def test_refuse_mode_does_not_wait_for_a_remote_that_cannot_appear(tmp_path, near):
    world = _cpu_world(tmp_path, near, HEAVY)
    _single(world)
    preference.configure({"heavy_local_fallback": "refuse"}, [NEAR, "salad"])
    world.start()
    world.tick(3)
    assert world.attempt() is None
    (event,) = world.events(guard.REFUSED_EVENT)
    assert event["mode"] == "refuse" and event["code"] == CODE
    assert _stop_reason(world) == f"stage_failed:g1:{CODE}"


def test_an_untried_remote_is_tried_before_an_unexplained_local(tmp_path, near):
    # A gpu-tagged stage keeps the spec's order [local, salad-cpu] (ADR 0088); the local
    # candidate would land with only ``remote_not_tried``, so Salad is tried instead.
    world = _cpu_world(tmp_path, near, {"placement_tags": ["gpu"]})
    world.start()
    world.tick_until(lambda: bool(_placed(world)))
    (placed,) = _placed(world)
    assert placed["candidates"] == [NEAR, "salad"] and placed["provider_id"] == "salad"
    assert placed["skipped"] == [
        {"provider_id": NEAR, "reason": CODE, "detail": "remote_not_tried"}
    ]
    assert "heavy_guard" not in placed
    assert not world.events(guard.EVENT) and not world.events(guard.REFUSED_EVENT)


# -- explained: placed locally, the reason on the placement and the receipt ---------------------


def test_an_untried_remote_that_is_then_skipped_explains_the_local(tmp_path, near, monkeypatch):
    world = _cpu_world(tmp_path, near, {"placement_tags": ["gpu"]})
    world.start()
    _salad_not_enrolled(monkeypatch)
    world.tick_until(lambda: world.attempt() is not None)
    (placed,) = _placed(world)
    assert placed["provider_id"] == NEAR and placed["candidates"] == [NEAR, "salad"]
    assert [(s["provider_id"], s["reason"]) for s in placed["skipped"]] == [
        ("salad", "not_enrolled")
    ]
    assert placed["not_evaluated"] == []
    block = placed["heavy_guard"]
    assert block["reasons"] == ["no_fitting_profile"] and "unexplained_local" not in block
    (event,) = world.events(guard.EVENT)
    assert event["reasons"] == ["no_fitting_profile"]
    assert world.attempt()["provider_id"] == NEAR


def test_a_record_over_a_cap_explains_the_local_placement(tmp_path, near):
    preference.configure(
        {"heavy_remote_candidate": {"provider": "salad", "max_exposure_cents": 1}},
        [NEAR, "salad"],
    )
    world = cpu_world(tmp_path, near, slots=1)
    world.spec = spec_of(world, alternate_providers=None, memory_bytes=2 * GIB)
    world.start()
    stored = json.loads(world.env.query("SELECT spec_json FROM campaigns")[0]["spec_json"])
    assert stored["stages"][0]["heavy_remote_candidate"]["reason"] == "over_operator_cap"
    world.tick_until(lambda: world.attempt() is not None)
    assert world.attempt()["provider_id"] == NEAR
    (event,) = world.events(guard.EVENT)
    detail = [
        {"provider_id": "salad", "reason": "over_operator_cap", "source": guard.RECORD_SOURCE}
    ]
    assert event["reasons"] == ["remote_candidate_not_added"] and event["detail"] == detail
    assert guard.explained(event["reasons"], event["detail"])
    assert not world.events(guard.REFUSED_EVENT)
    block = _receipt_entry(world)["placement"]["heavy_guard"]
    assert block["reasons"] == ["remote_candidate_not_added"] and block["detail"] == detail
    assert block["warning"] is True and "unexplained_local" not in block


def test_salad_skipped_for_capacity_explains_the_local_placement(tmp_path, near, monkeypatch):
    real = placement._check

    def full(rt, ctx, base, stage, exposure_open):
        if stage.provider == "salad":
            return placement.Skip("salad", "no_free_slot", "0 free, 0 placed awaiting a grant")
        return real(rt, ctx, base, stage, exposure_open)

    monkeypatch.setattr(placement, "_check", full)
    world = _cpu_world(tmp_path, near, HEAVY)
    world.start()
    world.tick_until(lambda: world.attempt() is not None)
    (placed,) = _placed(world)
    assert placed["candidates"] == ["salad", NEAR] and placed["provider_id"] == NEAR
    expected = {
        "warning": True,
        "signals": ["tag:heavy"],
        "reasons": ["remote_skipped"],
        "detail": [{"provider_id": "salad", "reason": "no_free_slot"}],
        "mode": "warn",
    }
    assert placed["heavy_guard"] == expected
    assert _receipt_entry(world)["placement"]["heavy_guard"] == expected
    assert not world.events(guard.REFUSED_EVENT)


# -- the operator's opt-out, and light stages ---------------------------------------------------


def test_the_operator_may_opt_back_into_a_recorded_warning(tmp_path, near):
    world = _cpu_world(tmp_path, near, HEAVY)
    _single(world)
    preference.configure({"heavy_unexplained_local": "warn"}, [NEAR, "salad"])
    world.start()
    world.tick_until(lambda: world.attempt() is not None)
    assert world.attempt()["provider_id"] == NEAR
    assert not world.events(guard.REFUSED_EVENT)
    (event,) = world.events(guard.EVENT)
    assert event["reasons"] == ["not_listed"] and event["unexplained_local"] == "warn"
    block = _receipt_entry(world)["placement"]["heavy_guard"]
    assert block["unexplained_local"] == "warn" and block["reasons"] == ["not_listed"]
    assert block["warning"] is True


def test_a_light_one_provider_stage_is_unchanged(tmp_path, near):
    world = _cpu_world(tmp_path, near, {"max_duration_seconds": 60})
    _single(world)
    world.start()
    world.tick_until(lambda: world.attempt() is not None)
    assert world.attempt()["provider_id"] == NEAR
    assert not world.events(guard.EVENT) and not world.events(guard.REFUSED_EVENT)
    assert _stop_reason(world) is None
