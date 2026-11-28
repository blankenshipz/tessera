"""An unreadable guardian accounting is its own retryable cause; Salad stages default to two
infrastructure retries at submit (ADR 0215).

Fakes only: the guardian service double from ``test_salad_safety_service`` and the Salad
campaign world (no network, no provider)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import test_salad_safety_concurrent_frames as frames
from salad_campaign_fixture import SaladWorld, salad_provider_reset, salad_stage  # noqa: F401
from test_salad_runtime_settle_classify import CLEAN_STOPPED, STATUS, _attempts, _first

from tessera_control.campaign_infra_retry import (
    INFRA_SALAD_SAFETY_STOP,
    SAFETY_STOP_CAUSES,
    is_infra_class,
)
from tessera_control.campaign_models import EXPERIMENT_CEILING_CENTS, parse_spec, spec_digest
from tessera_control.campaign_salad_executor import project_guardian_final
from tessera_control.campaign_salad_retry_default import (
    SALAD_DEFAULT_INFRA_RETRIES,
    may_run_on_salad,
    with_salad_infra_retry_default,
)
from tessera_control.salad_guardian import ACCOUNTING_UNAVAILABLE_CAUSE, SERVICE_STOP_CAUSES

# -- guardian: an accounting read error is not a cap hit ---------------------------------------


def _kinds(service, kind):
    rows = service.db.execute("SELECT reservation FROM events WHERE kind=?", (kind,)).fetchall()
    return {name for (name,) in rows}


def test_an_unreadable_total_fences_every_frame_as_accounting_unavailable(tmp_path):
    service, clock, _provider, a, b = frames._two_frames_one_running(tmp_path, 40)
    try:

        def broken(**_kwargs):
            raise OSError("disk")

        service._committed = broken
        clock[0] += 1
        service.loop()
        for body in (a, b):
            row = service._row(body["reservation"])
            assert row["budget_fenced"] == 1 and row["recovery"] == 1  # still fail-closed
            assert row["stop_cause"] == ACCOUNTING_UNAVAILABLE_CAUSE
        assert _kinds(service, ACCOUNTING_UNAVAILABLE_CAUSE) == {a["reservation"], b["reservation"]}
        assert _kinds(service, "total_cap_breached") == set()
        # The detail is the exception type only, never its text.
        details = {
            detail
            for (detail,) in service.db.execute(
                "SELECT detail FROM events WHERE kind=?", (ACCOUNTING_UNAVAILABLE_CAUSE,)
            )
        }
        assert details == {"OSError"}
        # The bound frame's guardian reports the service's first cause.
        assert service.guardian.status("a" * 32)["stop_cause"] == ACCOUNTING_UNAVAILABLE_CAUSE
    finally:
        service.close()


def test_a_real_overshoot_stays_total_cap(tmp_path):
    service, clock, _provider, a, b = frames._two_frames_one_running(tmp_path, 20)
    try:
        with service.db:
            service.db.execute(
                "UPDATE reservations SET accrued_cents='15' WHERE reservation=?",
                (a["reservation"],),
            )
        clock[0] += 1
        service.loop()
        assert service._row(b["reservation"])["stop_cause"] == "total_cap"
        assert _kinds(service, ACCOUNTING_UNAVAILABLE_CAUSE) == set()
    finally:
        service.close()


def test_the_cause_tables_agree():
    assert ACCOUNTING_UNAVAILABLE_CAUSE in SERVICE_STOP_CAUSES
    assert ACCOUNTING_UNAVAILABLE_CAUSE in SAFETY_STOP_CAUSES
    assert "total_cap" in SERVICE_STOP_CAUSES and "total_cap" not in SAFETY_STOP_CAUSES


def test_the_projection_carries_accounting_unavailable_but_never_total_cap():
    carried = project_guardian_final(dict(STATUS, stop_cause=ACCOUNTING_UNAVAILABLE_CAUSE))
    assert carried["safety_stop_cause"] == ACCOUNTING_UNAVAILABLE_CAUSE
    assert "safety_stop_cause" not in project_guardian_final(dict(STATUS, stop_cause="total_cap"))


# -- control: retried as infrastructure; a cap hit is not ------------------------------------


def _unset_stage(env, **over):
    """A Salad stage whose submitted retry leaves ``infra_retries`` unset."""
    stage = salad_stage(env, **over)
    stage["retry"] = {"max_attempts": 1}
    return stage


def _stored_retry(world):
    spec = json.loads(
        world.row("SELECT spec_json FROM campaigns WHERE id=?", world.campaign_id)["spec_json"]
    )
    return spec["stages"][0]["retry"]


def test_an_accounting_read_error_is_retried_under_the_salad_default(tmp_path):
    world = SaladWorld(tmp_path, stages=lambda env: [_unset_stage(env)])
    world.to_route_started()
    assert _stored_retry(world)["infra_retries"] == SALAD_DEFAULT_INFRA_RETRIES
    world.run_to_outcome(
        passed=False,
        outputs=False,
        guardian_final=dict(CLEAN_STOPPED, safety_stop_cause=ACCOUNTING_UNAVAILABLE_CAUSE),
    )
    world.tick_until(lambda: len(_attempts(world)) >= 2)
    first = _first(world)
    assert (first["status"], first["failure_class"]) == (
        "failed_transient",
        INFRA_SALAD_SAFETY_STOP,
    )
    assert ACCOUNTING_UNAVAILABLE_CAUSE in first["failure_detail"]
    assert first["settled_cents"] == first["hold_cents"]  # settlement unchanged


def test_a_total_cap_stop_is_not_retried_even_with_infra_retries_left(tmp_path):
    world = SaladWorld(tmp_path, stages=lambda env: [_unset_stage(env)])
    world.to_route_started()
    world.run_to_outcome(
        passed=False,
        outputs=False,
        guardian_final=CLEAN_STOPPED,  # a total_cap cause never reaches the recorded final
        receipt_extra=dict(guardian_stop_cause="total_cap"),
    )
    world.tick_until(lambda: world.status() == "failed")
    attempt = world.attempt()
    assert attempt["failure_class"] == "salad_not_passed"
    assert not is_infra_class(attempt["failure_class"])
    assert len(_attempts(world)) == 1


# -- the submit-time default ------------------------------------------------------------------


def test_the_default_is_written_into_the_stored_spec_and_its_digest(tmp_path):
    world = SaladWorld(tmp_path, stages=lambda env: [_unset_stage(env)])
    world.start()
    row = world.row("SELECT spec_json, spec_digest FROM campaigns WHERE id=?", world.campaign_id)
    assert json.loads(row["spec_json"])["stages"][0]["retry"]["infra_retries"] == 2
    stored = parse_spec(json.loads(row["spec_json"]))
    assert spec_digest(stored) == row["spec_digest"]  # re-parsing never re-defaults
    assert stored.stages[0].retry.worst_case_attempts == 3  # counted in the approved exposure


def test_a_stage_without_any_retry_block_gets_the_default(tmp_path):
    def stage(env):
        value = salad_stage(env)
        value.pop("retry")
        return value

    world = SaladWorld(tmp_path, stages=lambda env: [stage(env)])
    world.start()
    assert _stored_retry(world)["infra_retries"] == 2


@pytest.mark.parametrize("explicit", [0, 1, 2])
def test_an_explicit_value_is_respected(tmp_path, explicit):
    world = SaladWorld(
        tmp_path,
        stages=lambda env: [salad_stage(env, retry={"max_attempts": 1, "infra_retries": explicit})],
    )
    world.start()
    stored = _stored_retry(world)
    assert stored.get("infra_retries", 0) == explicit


def test_explicit_zero_keeps_the_digest_a_spec_had_before(tmp_path):
    world = SaladWorld(
        tmp_path,
        stages=lambda env: [salad_stage(env, retry={"max_attempts": 1, "infra_retries": 0})],
    )
    world.start()
    row = world.row("SELECT spec_json, spec_digest FROM campaigns WHERE id=?", world.campaign_id)
    assert "infra_retries" not in json.loads(row["spec_json"])["stages"][0]["retry"]


def test_validate_reports_the_digest_submit_stores(tmp_path):
    world = SaladWorld(tmp_path, stages=lambda env: [_unset_stage(env)])
    env = world.env
    checked = env.store.validate_campaign_spec(env.owner, env.project["id"], {"spec": world.spec})
    world.start()
    stored = world.row("SELECT spec_digest FROM campaigns WHERE id=?", world.campaign_id)
    assert checked["spec_digest"] == stored["spec_digest"]


def test_the_default_is_not_applied_where_it_would_pass_the_experiment_ceiling(tmp_path):
    hold = EXPERIMENT_CEILING_CENTS // 2  # one attempt fits the ceiling, 1 + 2 do not
    world = SaladWorld(tmp_path, stages=lambda env: [_unset_stage(env, hold=hold)])
    data = dict(world.spec, budget_cap_cents=hold)
    spec = parse_spec(data)
    unknown = SimpleNamespace(get=lambda _name: None)  # no profile ceiling in the way
    assert with_salad_infra_retry_default(spec, unknown) is spec
    assert spec.stages[0].retry.infra_retries == 0


def test_the_default_is_not_applied_where_the_profile_task_ceiling_refuses_it(tmp_path):
    # hold 20 x (1 + 2 reallocation) = 60 fits a 60-cent task ceiling; with 2 more it does not.
    world = SaladWorld(
        tmp_path, stages=lambda env: [_unset_stage(env)], max_task_cents=60, max_run_cents=60
    )
    world.start()  # submitted and approved as before the default existed
    assert _stored_retry(world).get("infra_retries", 0) == 0


def test_a_spec_stored_without_the_key_still_means_zero(tmp_path):
    world = SaladWorld(tmp_path, stages=lambda env: [_unset_stage(env)])
    world.start()
    row = world.row("SELECT spec_json FROM campaigns WHERE id=?", world.campaign_id)
    data = json.loads(row["spec_json"])
    data["stages"][0]["retry"].pop("infra_retries")
    # A spec stored before the default: parsing never applies it (only fresh submits do).
    assert parse_spec(data).stages[0].retry.infra_retries == 0


def test_only_a_stage_that_may_run_on_salad_is_defaulted():
    local = SimpleNamespace(backend="docker-local-cpu-v1")
    v2_local = SimpleNamespace(provider="local-docker", alternate_providers=None)
    v2_alt = SimpleNamespace(
        provider="local-docker", alternate_providers=[SimpleNamespace(provider="salad")]
    )
    assert not may_run_on_salad(local) and not may_run_on_salad(v2_local)
    assert may_run_on_salad(v2_alt)
    assert may_run_on_salad(SimpleNamespace(backend="salad-gpu-v1"))


def test_the_default_is_not_applied_where_an_operator_fallback_task_ceiling_refuses_it(tmp_path):
    # The stage's own profile takes the default; the operator CPU fallback on its chain does not
    # (hold 20 x 3 attempts fits 60, x 5 does not), so the default would silently remove a
    # fallback the stage could have run on at 0: the stage keeps 0 (ADR 0215 F3).
    world = SaladWorld(tmp_path, stages=lambda env: [_unset_stage(env)])
    spec = parse_spec(world.spec)
    own = spec.stages[0].salad.profile
    ceilings = {
        own: SimpleNamespace(max_task_cents=10_000),
        "cpu-fallback": SimpleNamespace(max_task_cents=60),
    }
    chains = {own: ("cpu-fallback",)}
    with_chain = SimpleNamespace(get=ceilings.get, fallback_chain=lambda n: chains.get(n, ()))
    assert with_salad_infra_retry_default(spec, with_chain) is spec
    without_chain = SimpleNamespace(get=ceilings.get, fallback_chain=lambda _name: ())
    defaulted = with_salad_infra_retry_default(spec, without_chain)
    assert defaulted.stages[0].retry.infra_retries == SALAD_DEFAULT_INFRA_RETRIES
