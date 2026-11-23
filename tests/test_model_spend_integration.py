"""The supervisor's ledger client against the real control-plane routes (no network, in-process).

Proves the two halves agree on the wire: reserve, settle, release, the cap refusals read from a
200 ``allowed: false`` body, and that totals read back through the same routes the MCP tool uses.
"""

# ruff: noqa: F811
from __future__ import annotations

import pytest
from test_anthropic_runtime_integration import (  # noqa: F401
    MODEL,
    Transport,
    creds,
    make_profile,
    setup,
)
from test_model_spend_api import SHA, World, resolve

import tessera_agent.remote_reasoning_runtime as rr
from tessera_agent.model_spend_client import (
    CAP_EXCEEDED,
    NOT_CONFIGURED,
    UNAVAILABLE,
    SpendLedger,
)
from tessera_agent.remote_reasoning_runtime import RuntimeBlocked


class Control:
    """``control.call`` over the in-process app, authenticated as the enrolled agent."""

    def __init__(self, world: World) -> None:
        self.world = world

    def call(self, method, path, body=None, key=None):
        headers = {**self.world.agent(), **({"Idempotency-Key": key} if key else {})}
        response = self.world.client.request(method, path, json=body, headers=headers)
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}")
        return response.json()


QUALIFIED = "anthropic/" + MODEL


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def ledger(world, project=None):
    return SpendLedger(Control(world), project or world.project, "anthropic", "claude-test")


def test_nothing_is_allowed_before_a_human_sets_a_limit(world):
    got = ledger(world).reserve("run-1", 1, 1.0)
    assert not got.allowed and got.code == NOT_CONFIGURED


def test_reserve_settle_and_totals_agree_across_the_wire(world):
    assert world.limits(per_call=5, daily=20).status_code == 200
    spend = ledger(world)
    got = spend.reserve("run-1", 1, 2.5)
    assert got.allowed and got.call_id and got.reserved_micro_cents == 2_500_000
    done = spend.settle(got.call_id, "run-1", 1, 1.25, SHA)
    assert done.ok and done.settled_micro_cents == 1_250_000 and not done.cap_exceeded
    today = world.read().json()["today"]
    assert today["settled_cents"] == 1.25 and today["reserved_cents"] == 0
    # A replay with the same keys changes nothing.
    again = spend.reserve("run-1", 1, 2.5)
    assert again.allowed and again.call_id == got.call_id
    assert spend.settle(got.call_id, "run-1", 1, 1.25, SHA).ok
    assert world.read().json()["today"]["calls"] == 1


def test_caps_refuse_before_anything_is_reserved_and_never_downshift(world):
    world.limits(per_call=2, daily=3)
    spend = ledger(world)
    over = spend.reserve("run-1", 1, 2.5)
    assert not over.allowed and over.code == CAP_EXCEEDED
    first = spend.reserve("run-1", 2, 2.0)
    assert first.allowed
    second = spend.reserve("run-1", 3, 2.0)  # open reservation counts against the day
    assert not second.allowed and second.code == CAP_EXCEEDED
    assert spend.release(first.call_id, "run-1", 2)
    assert spend.reserve("run-1", 4, 2.0).allowed  # released budget is free again


def test_actual_above_the_cap_is_recorded_and_flagged(world):
    world.limits(per_call=2, daily=3)
    spend = ledger(world)
    got = spend.reserve("run-1", 1, 2.0)
    done = spend.settle(got.call_id, "run-1", 1, 3.5, SHA)
    assert done.ok and done.over_reservation and done.cap_exceeded


def test_a_control_failure_is_not_allowed(world):
    class Down:
        def call(self, *args, **kwargs):
            raise RuntimeError("down")

    got = SpendLedger(Down(), world.project, "anthropic", "claude-test").reserve("run-1", 1, 1.0)
    assert not got.allowed and got.code == UNAVAILABLE


# -- the supervisor's reservation path against the real routes -----------------------------


def _supervisor(world, tmp_path, creds):
    """A real ReasoningSupervisor (real journal) whose ledger talks to the in-process control."""
    profile = make_profile(model_spend={"project_id": world.project})
    store, supervisor, _, _ = setup(tmp_path, creds, Transport(), profile=profile)
    supervisor.spend = SpendLedger(Control(world), None, "anthropic", QUALIFIED)
    return store, supervisor, profile["run_id"]


def test_a_refusal_before_limits_exist_is_reevaluated_once_a_human_sets_them(
    world, tmp_path, creds
):
    store, supervisor, run = _supervisor(world, tmp_path, creds)
    with pytest.raises(RuntimeBlocked, match="model_spend_not_configured"):
        supervisor._reserve(run, 1.0)
    assert world.limits(per_call=5, daily=20).status_code == 200
    assert supervisor._reserve(run, 1.0) is True  # a new sequence, so a new answer
    assert [r["state"] for r in store.spend_rows(run)] == ["denied", "reserved"]
    assert world.read().json()["today"]["reserved_cents"] == 1.0


def test_a_daily_cap_refusal_is_reevaluated_after_the_limit_is_raised(world, tmp_path, creds):
    store, supervisor, run = _supervisor(world, tmp_path, creds)
    world.limits(per_call=5, daily=5)
    assert supervisor._reserve(run, 4.0) is True
    supervisor._release_open(run)  # not sent: the day is free again
    other = world.reserve(seq=9, cents=4.0, run="other-run")  # another run fills the day
    assert other.json()["allowed"]
    assert supervisor._reserve(run, 2.0) is False  # worst case would pass the daily cap
    world.limits(per_call=5, daily=20)
    assert supervisor._reserve(run, 2.0) is True


def test_a_different_worst_case_after_a_refusal_is_not_reported_unavailable(world, tmp_path, creds):
    store, supervisor, run = _supervisor(world, tmp_path, creds)
    world.limits(per_call=1, daily=20)
    assert supervisor._reserve(run, 2.5) is False  # over the per-call cap
    assert supervisor._reserve(run, 0.5) is True  # same run, other prompt size: no Conflict


def test_a_lost_answer_does_not_advance_and_the_retry_replays_the_stored_reservation(
    world, tmp_path, creds
):
    store, supervisor, run = _supervisor(world, tmp_path, creds)
    world.limits(per_call=5, daily=20)
    real = supervisor.spend.control
    supervisor.spend.control = type(
        "Lost",
        (),
        {"call": lambda self, *a, **k: (real.call(*a, **k), (_ for _ in ()).throw(OSError()))[0]},
    )()
    with pytest.raises(RuntimeBlocked, match="model_spend_unavailable"):
        supervisor._reserve(run, 1.0)
    assert store.spend_rows(run) == []  # nothing recorded, the sequence is unchanged
    supervisor.spend.control = real
    assert supervisor._reserve(run, 1.0) is True
    assert [r["seq"] for r in store.spend_rows(run)] == [1]
    assert len(world.read().json()["open_reservations"]) == 1


def test_keys_for_a_maximum_length_run_id_stay_distinct():
    key = SpendLedger._key
    keys = {key("r" * 128, n, op) for n in (1, 2) for op in ("reserve", "settle", "release")}
    assert len(keys) == 6 and all(len(k) <= 128 and k.isprintable() for k in keys)


def test_the_report_shows_a_persons_resolution_read_from_the_real_route(world, tmp_path, creds):
    store, supervisor, run = _supervisor(world, tmp_path, creds)
    world.limits(per_call=5, daily=20)
    assert supervisor._reserve(run, 2.0) is True  # an uncertain call would leave this open
    (open_row,) = [r for r in store.spend_rows(run) if r["state"] == "reserved"]
    before = dict(store.get(run))
    rr.refresh_spend_rows(store, supervisor.spend, run)  # still open on the control plane
    assert store.spend_rows(run)[-1]["state"] == "reserved"
    assert resolve(world, open_row["call_id"], "not_sent").status_code == 200
    rr.refresh_spend_rows(store, supervisor.spend, run)
    (row,) = [r for r in store.spend_rows(run) if r["call_id"] == open_row["call_id"]]
    assert (row["state"], row["resolution"]) == ("released", "person_released")
    assert dict(store.get(run)) == before  # the run itself is untouched


def test_a_person_settled_resolution_is_shown_as_settled(world, tmp_path, creds):
    store, supervisor, run = _supervisor(world, tmp_path, creds)
    world.limits(per_call=5, daily=20)
    assert supervisor._reserve(run, 2.0) is True
    (open_row,) = store.spend_rows(run)
    assert resolve(world, open_row["call_id"], "spent", actual_cents=1.5).status_code == 200
    rr.refresh_spend_rows(store, supervisor.spend, run)
    assert [
        (r["state"], r["resolution"], r["settled_micro_cents"]) for r in store.spend_rows(run)
    ] == [("settled", "person_settled", 1_500_000)]


def test_another_runs_call_is_not_readable_through_the_status_route(world, tmp_path, creds):
    store, supervisor, run = _supervisor(world, tmp_path, creds)
    world.limits(per_call=5, daily=20)
    assert supervisor._reserve(run, 2.0) is True
    (open_row,) = store.spend_rows(run)
    assert resolve(world, open_row["call_id"], "not_sent").status_code == 200
    ledger = supervisor.spend
    assert ledger.status(open_row["call_id"], run).status == "released"
    # Asked as another run (or without a run at all), the real route does not answer.
    assert ledger.status(open_row["call_id"], "another-run") is None
    with pytest.raises(RuntimeError):
        Control(world).call("GET", f"/model-spend/reservations/{open_row['call_id']}")


def test_an_unreachable_control_plane_leaves_the_report_rows_alone(world, tmp_path, creds):
    store, supervisor, run = _supervisor(world, tmp_path, creds)
    world.limits(per_call=5, daily=20)
    assert supervisor._reserve(run, 2.0) is True
    (open_row,) = store.spend_rows(run)
    resolve(world, open_row["call_id"], "not_sent")
    before = store.spend_rows(run)

    class Down:
        def call(self, *args, **kwargs):
            raise OSError("down")

    supervisor.spend.control = Down()
    rr.refresh_spend_rows(store, supervisor.spend, run)
    assert store.spend_rows(run) == before


def test_a_daily_cap_refusal_is_reevaluated_after_midnight_utc(world, tmp_path, creds, monkeypatch):
    from datetime import UTC, datetime, timedelta

    import tessera_control.model_spend as control_spend

    store, supervisor, run = _supervisor(world, tmp_path, creds)
    world.limits(per_call=5, daily=5)
    assert world.reserve(seq=1, cents=4.0, run="other-run").json()["allowed"]  # fills today
    assert supervisor._reserve(run, 2.0) is False  # 4 + 2 > 5 today: refused
    assert [r["state"] for r in store.spend_rows(run)] == ["denied"]
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0)
    stamp = tomorrow.isoformat().replace("+00:00", "Z")
    monkeypatch.setattr(control_spend, "utc_now", lambda: stamp)
    supervisor.clock = lambda: tomorrow.timestamp()
    assert supervisor._reserve(run, 2.0) is True  # a new sequence, judged against the new day
    assert [r["state"] for r in store.spend_rows(run)] == ["denied", "reserved"]
    (booked,) = world.rows("SELECT utc_day FROM model_spend_calls WHERE run_id=?", run)
    assert booked[0] == tomorrow.date().isoformat()  # judged and booked against the new day
