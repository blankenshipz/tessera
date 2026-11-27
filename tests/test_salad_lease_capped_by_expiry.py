"""A Salad lease that no fresh lease could make long enough is not requeued in a loop.

The pre-route window check requeues an attempt whose lease lost its route window to waiting.
When a credential, the campaign approval or the grant lifetime caps every fresh lease below what
the run needs, a requeue cannot help: the attempt is closed ``lease_capped_by_expiry`` at 0 (not
a scientific retry, not a not-started requeue), the bound is recorded on a campaign event, and the
stage waits until the bound is renewed. A route window longer than any lease is issued for is
measured against what a lease can give (declared duration plus provisioning allowance).

Real stores and runner (``SaladWorld``) on synthetic state, plus pure helpers; no provider.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from salad_campaign_fixture import SaladWorld, salad_provider_reset, salad_stage  # noqa: F401

from tessera_control.campaign_autoreconcile import NOT_STARTED
from tessera_control.campaign_salad_lease_cap import (
    CAPPED_EVENT,
    LEASE_BOUND_BY_GRANT_TTL,
    LEASE_CAPPED_BY_EXPIRY,
    WAITING_EVENT,
    admission_refusal,
    capped_attempts,
    needed_seconds,
    shortfall,
)
from tessera_control.campaign_salad_lease_lapse import (
    LEASE_WINDOW_SHORT_BEFORE_ROUTE,
    REQUEUE_EVENT,
)
from tessera_control.remote_execution_models import SALAD_PROVISIONING_ALLOWANCE_SECONDS
from tessera_control.store import Conflict

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)


def _iso(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


# -- pure helpers -----------------------------------------------------------------------------


def test_the_need_is_the_window_but_never_more_than_a_lease_is_issued_for():
    issued_for = 3000 + SALAD_PROVISIONING_ALLOWANCE_SECONDS
    assert needed_seconds(3600, 3000) == 3600
    assert needed_seconds(6600, 3000) == issued_for
    assert needed_seconds(6600, None) == 6600


class _Rows:
    def __init__(self, credentials):
        self.credentials = credentials

    def _rows(self, sql, args):
        return [{"expires_at": e} for p, e in self.credentials if p == args[0]]


@pytest.mark.parametrize(
    ("worker_left", "approver_left", "approval_left", "ttl", "binding"),
    [
        (1800, 86400, 86400, 21600, "worker_credential"),
        (86400, 1200, 86400, 21600, "approver_credential"),
        (86400, 86400, 900, 21600, "campaign_approval"),
        (86400, 86400, 86400, 1000, "grant_lifetime"),
    ],
)
def test_the_binding_bound_is_named(worker_left, approver_left, approval_left, ttl, binding):
    runner = _Rows(
        [
            ("worker", _iso(NOW + timedelta(seconds=worker_left))),
            ("person", _iso(NOW + timedelta(seconds=approver_left))),
        ]
    )
    approval = {
        "expires_at": _iso(NOW + timedelta(seconds=approval_left)),
        "approving_principal_id": "person",
    }
    record = shortfall(runner, approval, "worker", ttl, 3600, NOW)
    assert record is not None and record["binding"] == binding
    assert record["need_seconds"] == 3600 and record["available_seconds"] < 3600


def test_a_grant_lifetime_below_the_need_is_named_even_behind_an_earlier_renewable_bound():
    # The worker credential ends first, but renewing it would still leave every lease capped at
    # the 3600 s grant lifetime: the static bound is the one that decides.
    runner = _Rows([("worker", _iso(NOW + timedelta(seconds=600)))])
    approval = {"expires_at": _iso(NOW + timedelta(days=1)), "approving_principal_id": "x"}
    record = shortfall(runner, approval, "worker", 3600, 4500, NOW)
    assert record is not None and record["binding"] == "grant_lifetime"
    assert record["available_seconds"] == 3600


@pytest.mark.parametrize(
    ("ttl", "max_duration", "route_max", "refused"),
    [
        (3600, 3000, 7200, True),  # the review's repro: a lease must cover 4500 s
        (4500, 3000, 7200, False),
        (3600, 3000, 3600, False),  # the profile caps the window at 3600 s
        (3600, 2100, 7200, False),  # 2100 + 1500 = 3600
    ],
)
def test_admission_refuses_a_grant_lifetime_no_lease_could_run_under(
    ttl, max_duration, route_max, refused
):
    refusal = admission_refusal(ttl, max_duration, route_max)
    assert (refusal is not None) is refused
    if refused:
        assert LEASE_BOUND_BY_GRANT_TTL in refusal and "raise grant_ttl_seconds" in refusal


def test_no_shortfall_when_a_fresh_lease_could_cover_the_need():
    runner = _Rows([("worker", _iso(NOW + timedelta(days=1)))])
    approval = {"expires_at": _iso(NOW + timedelta(days=1)), "approving_principal_id": "x"}
    assert shortfall(runner, approval, "worker", 21600, 3600, NOW) is None
    # Exactly enough is enough.
    approval["expires_at"] = _iso(NOW + timedelta(seconds=3600))
    assert shortfall(runner, approval, "worker", 21600, 3600, NOW) is None


def test_capped_attempts_are_counted_apart_from_scientific_ones():
    attempts = [{"failure_class": LEASE_CAPPED_BY_EXPIRY}, {"failure_class": NOT_STARTED}]
    assert capped_attempts(attempts) == 1


# -- the runner -------------------------------------------------------------------------------


def _window(world: SaladWorld) -> int:
    return int(world.route_state()["proposal"]["selection"]["resources"]["max_seconds"])


def _age_lease(world: SaladWorld, *, remaining: int, waited: int = 3600) -> None:
    now = datetime.now(UTC)
    hard = (now + timedelta(seconds=remaining)).isoformat()
    world.env.execute(
        "UPDATE execution_leases SET issued_at=?, expires_at=?, hard_expires_at=? WHERE id=?",
        (now - timedelta(seconds=waited)).isoformat(),
        hard,
        hard,
        world.salad()["lease_id"],
    )


def _renew_credentials(world: SaladWorld) -> None:
    """The operator renews the expiring worker credential through its own path (simulated)."""
    later = (datetime.now(UTC) + timedelta(hours=6)).isoformat()
    world.env.execute(
        "UPDATE credentials SET expires_at=? WHERE principal_id=?", later, world.worker["id"]
    )


def _first(world: SaladWorld):
    return world.row(
        "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND stage_key='g1' "
        "AND attempt=1",
        world.campaign_id,
    )


def _closed(world: SaladWorld) -> bool:
    return _first(world)["status"] in {"failed_transient", "failed_permanent"}


def _to(world: SaladWorld, phase: str) -> None:
    world.to_awaiting_review()
    world.resolve_decisions()
    world.tick_until_phase("route_proposed")
    if phase == "route_started":
        world.tick_until_phase("route_started")


@pytest.mark.parametrize("phase", ["route_proposed", "route_started"])
def test_a_lease_no_fresh_lease_could_beat_closes_capped_and_the_stage_waits(tmp_path, phase):
    # This world's credentials last about an hour: every fresh lease is capped below the
    # 3600 s window, so a requeue could only loop.
    world = SaladWorld(tmp_path)
    _to(world, phase)
    _age_lease(world, remaining=_window(world) - 100)

    world.tick_until(lambda: _closed(world))

    first = _first(world)
    assert first["status"] == "failed_transient", world.dump()
    assert first["failure_class"] == LEASE_CAPPED_BY_EXPIRY
    assert "worker_credential leaves" in first["failure_detail"]
    assert "renewed by an operator" in first["failure_detail"]
    assert first["settled_cents"] in (0, None)
    assert first["settlement_basis"] == "unclaimed_zero"
    assert world.salad(attempt=1)["provider_id"] is None
    capped = world.events(CAPPED_EVENT)
    assert len(capped) == 1 and capped[0]["binding"].endswith("_credential")
    assert capped[0]["need_seconds"] == _window(world)
    assert [e for e in world.events(REQUEUE_EVENT) if e["reason"] == LEASE_CAPPED_BY_EXPIRY]

    # No loop: no further attempt while the bound still caps a fresh lease; one wait event.
    world.tick(8)
    assert world.attempt()["attempt"] == 1
    assert len(world.events(WAITING_EVENT)) == 1
    stage = world.row(
        "SELECT * FROM campaign_stages WHERE campaign_id=? AND stage_key='g1'", world.campaign_id
    )
    assert stage["status"] not in {"failed", "skipped"}
    assert world.status() == "running"

    # Renewed through the operator's own path: the next attempt is placed and the approved
    # review is carried over (no person).
    _renew_credentials(world)
    world.tick_until(lambda: world.attempt()["attempt"] == 2)
    world.tick_until_phase("awaiting_lease")
    world.lease_and_package()
    world.tick_until_phase("route_started")
    assert world.attempt()["attempt"] == 2


def test_a_window_longer_than_any_lease_is_measured_against_what_a_lease_gives(tmp_path):
    # Route window 6600 s (plan bound 3000 + 3600, profile ceiling 7200); a lease is issued for
    # at most 3000 + 1500 = 4500 s, so a fresh one could never cover 6600 s either. A lease that
    # still holds 4500 s (+ slack) after waiting is not short and is not requeued.
    world = SaladWorld(tmp_path, route_max_seconds=7200)
    _to(world, "route_proposed")
    window = _window(world)
    need = needed_seconds(window, 3000)
    assert need < window
    _renew_credentials(world)
    _age_lease(world, remaining=need + 600)
    world.tick(3)
    assert _first(world)["status"] not in {"failed_transient", "failed_permanent"}
    assert not world.events(REQUEUE_EVENT)


def test_a_short_lease_a_fresh_one_could_beat_still_requeues_not_started(tmp_path):
    world = SaladWorld(tmp_path)
    _to(world, "route_proposed")
    _renew_credentials(world)
    _age_lease(world, remaining=_window(world) - 100)
    world.tick_until(lambda: _closed(world))
    first = _first(world)
    assert first["failure_class"] == NOT_STARTED, world.dump()
    assert f"({LEASE_WINDOW_SHORT_BEFORE_ROUTE})" in first["failure_detail"]
    assert not world.events(CAPPED_EVENT)


# -- static and approval bounds (review repro: max_duration 3000, grant_ttl 3600, route 7200) --


def _short_ttl_stages(env):
    stage = salad_stage(env)
    stage["salad"]["grant_ttl_seconds"] = 3600
    return [stage]


def test_a_salad_stage_whose_grant_lifetime_caps_every_lease_is_refused_at_admission(tmp_path):
    world = SaladWorld(tmp_path, stages=_short_ttl_stages, route_max_seconds=7200)
    with pytest.raises(Conflict, match=LEASE_BOUND_BY_GRANT_TTL):
        world.start()


def test_a_stage_bound_by_its_own_grant_lifetime_fails_instead_of_waiting(tmp_path, monkeypatch):
    # Admitted before the admission rule existed (simulated): the stage's grant lifetime (3600 s)
    # caps every fresh lease below the 4500 s it must cover. No renewal or re-approval changes a
    # spec bound, so the stage fails with a configuration reason at 0; it does not wait forever.
    import tessera_control.campaign_provider_salad as provider_salad

    monkeypatch.setattr(provider_salad, "lease_cap_admission_refusal", lambda *args: None)
    world = SaladWorld(tmp_path, stages=_short_ttl_stages, route_max_seconds=7200)
    _to(world, "route_proposed")
    _renew_credentials(world)  # only the static bound is left short
    _age_lease(world, remaining=1200)

    world.tick_until(lambda: _closed(world))

    first = _first(world)
    assert first["status"] == "failed_permanent", world.dump()
    assert first["failure_class"] == LEASE_BOUND_BY_GRANT_TTL
    assert "raise grant_ttl_seconds" in first["failure_detail"]
    assert "re-approved" not in first["failure_detail"]
    assert "renewed" not in first["failure_detail"]
    assert first["settled_cents"] in (0, None)
    assert first["settlement_basis"] == "unclaimed_zero"
    assert world.salad(attempt=1)["provider_id"] is None
    capped = world.events(CAPPED_EVENT)
    assert len(capped) == 1 and capped[0]["binding"] == "grant_lifetime"
    assert capped[0]["need_seconds"] == 4500 and capped[0]["available_seconds"] == 3600

    world.tick(4)
    stage = world.row(
        "SELECT * FROM campaign_stages WHERE campaign_id=? AND stage_key='g1'", world.campaign_id
    )
    assert stage["status"] == "failed", world.dump()
    assert world.attempt()["attempt"] == 1
    assert not world.events(WAITING_EVENT)


def test_a_lease_capped_by_the_campaign_approval_waits_for_re_approval(tmp_path):
    world = SaladWorld(tmp_path)
    _to(world, "route_proposed")
    _renew_credentials(world)
    # The approval ends in 30 minutes: every fresh grant (and so lease) ends with it.
    world.env.execute(
        "UPDATE campaign_approvals SET expires_at=? WHERE campaign_id=? AND status='active'",
        (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
        world.campaign_id,
    )
    _age_lease(world, remaining=_window(world) - 100)

    world.tick_until(lambda: _closed(world))

    first = _first(world)
    assert first["status"] == "failed_transient", world.dump()
    assert first["failure_class"] == LEASE_CAPPED_BY_EXPIRY
    assert "waiting for the campaign to be re-approved" in first["failure_detail"]
    capped = world.events(CAPPED_EVENT)
    assert len(capped) == 1 and capped[0]["binding"] == "campaign_approval"

    world.tick(4)
    assert world.attempt()["attempt"] == 1
    assert len(world.events(WAITING_EVENT)) == 1
    assert world.status() == "running"
