"""A stuck campaign's leased grants are driven through the evidence paths only (ADR 0153).

The fixture project has ``max_concurrency=1`` and one one-slot local worker, so a holder's
unresolved grant keeps every other campaign waiting at ``reserved``. Each holder's grant was
leased by the worker; the cases differ in what control recorded afterwards.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from campaign_runner_harness import (
    ScriptedAdapter,
    World,
    admit,
    cut_reports,
    dump,
    lapse,
    refuse_launches,
    stage_spec,
    started_but_unreported,
)

from tessera_control import campaign_exited_resolve as cer

METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]
PAST = "2000-01-01T00:00:00+00:00"
OVERRUN = "budget_overrun: test"
STUCK = ["blocked", "paused_delegation_lapsed"]


def _spec(world: World, **extra) -> dict:
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC, **extra)]
    return spec


def _stick(world: World, cid: str, status: str) -> None:
    """Blocked as a late overrun leaves it (only a person moves it on), or paused."""
    reason = OVERRUN if status == "blocked" else None
    world.env.execute(
        "UPDATE campaigns SET status=?, stop_reason=? WHERE id=?", status, reason, cid
    )


def _expire_release(world: World, attempt) -> None:
    world.env.execute(
        "UPDATE snapshot_releases SET expires_at=? WHERE id=?", PAST, attempt["release_id"]
    )


def _events(world: World, event_type: str, cid: str) -> list[dict]:
    return [e for e in world.events_of(event_type) if e["payload"]["campaign_id"] == cid]


def _freed(world: World, cid: str) -> list[dict]:
    return [e["payload"] for e in _events(world, "campaign.slot_freed", cid)]


def _held(world: World, cid: str) -> list[dict]:
    return [e["payload"] for e in _events(world, "campaign.slot_held", cid)]


def _grant(world: World, grant_id: str):
    return world.row("SELECT * FROM execution_grants WHERE id=?", grant_id)


def _granted(world: World, cid: str) -> bool:
    return any(a["grant_id"] for a in world.attempts(cid))


def _later(world: World, minutes: int):
    """Move the runner's clock; returns the original to put back."""
    original = world.runner.now
    world.runner.now = lambda: datetime.now(UTC) + timedelta(minutes=minutes)
    return original


def _exited_holder(world: World, status: str, **extra) -> tuple[str, dict]:
    """Leased, ran and exited (confirmed); the run receipt and manifest were lost; the
    grant and lease then expired and the campaign got stuck."""
    holder = world.campaign(_spec(world, **extra))
    cid = holder["id"]
    admit(world, cid)
    original = world.channel.post

    def cut(path, body, key):
        if any(n in path for n in ("run-receipt", "manifest", "artifact")):
            raise ConnectionError("synthetic report loss")
        return original(path, body, key)

    world.channel.post = cut
    world.poller.run(max_cycles=1)
    world.channel.post = original
    (attempt,) = world.attempts(cid)
    assert attempt["status"] == "admitted"
    lapse(world, attempt["grant_id"])
    _expire_release(world, attempt)
    _stick(world, cid, status)
    return cid, attempt


def _not_started_holder(world: World, status: str, monkeypatch) -> tuple[str, dict]:
    """Leased; the worker reported a refused launch (ADR 0046 evidence); then lapsed."""
    holder = world.campaign(_spec(world))
    cid = holder["id"]
    admit(world, cid)
    refuse_launches(monkeypatch)
    world.poller.run(max_cycles=1)
    monkeypatch.undo()
    (attempt,) = world.attempts(cid)
    assert attempt["status"] == "admitted"
    lapse(world, attempt["grant_id"])
    _expire_release(world, attempt)
    _stick(world, cid, status)
    return cid, attempt


def _unreported_holder(world: World, status: str) -> tuple[str, dict]:
    """Leased and started, every report lost, then lapsed: control cannot tell if it ran."""
    holder = world.campaign(_spec(world))
    cid = holder["id"]
    attempt = started_but_unreported(world, cid, [])
    _expire_release(world, attempt)
    _stick(world, cid, status)
    return cid, attempt


# -- evidence frees the slot ----------------------------------------------------------------


@pytest.mark.parametrize("status", STUCK)
def test_a_leased_grant_with_a_confirmed_exit_frees_the_slot(tmp_path, status):
    world = World(tmp_path)
    holder, attempt = _exited_holder(world, status)
    clock = _later(world, 5)  # past the output grace (ADR 0064)
    world.runner.tick()
    world.runner.now = clock
    (closed,) = world.attempts(holder)
    assert (closed["status"], closed["failure_class"]) == (
        "failed_transient",
        cer.EXIT_CONFIRMED_UNRESOLVED,
    )
    assert (closed["settled_cents"], closed["settlement_basis"]) == (0, "local_zero")
    grant = _grant(world, attempt["grant_id"])
    assert grant["status"] == "reconciled" and grant["settlement_cents"] == 0
    assert world.row("SELECT held_cents FROM campaigns WHERE id=?", holder)["held_cents"] == 0
    (event,) = _freed(world, holder)
    assert event["reason"] == "confirmed_exit" and event["grant_id"] == attempt["grant_id"]
    assert event["campaign_status"] == status and event["trigger"] == "stuck_campaign_reconcile"
    assert not _held(world, holder)
    assert world.status(holder) == status
    fresh = world.campaign(_spec(world))
    admit(world, fresh["id"])
    assert world.attempts(fresh["id"])[0]["grant_id"]
    for _ in range(3):
        world.runner.tick()
    assert len(_freed(world, holder)) == 1


@pytest.mark.parametrize("status", STUCK)
def test_a_leased_grant_that_never_started_frees_the_slot(tmp_path, status, monkeypatch):
    world = World(tmp_path)
    holder, attempt = _not_started_holder(world, status, monkeypatch)
    world.runner.tick()
    (closed,) = world.attempts(holder)
    assert (closed["status"], closed["failure_class"]) == ("failed_transient", "not_started")
    assert (closed["settled_cents"], closed["settlement_basis"]) == (0, "unclaimed_zero")
    grant = _grant(world, attempt["grant_id"])
    assert grant["status"] == "reconciled" and grant["settlement_cents"] == 0
    (event,) = _freed(world, holder)
    assert event["reason"] == "not_started_evidence" and event["grant_id"] == attempt["grant_id"]
    assert not _held(world, holder)
    assert world.status(holder) == status
    fresh = world.campaign(_spec(world))
    admit(world, fresh["id"])
    assert world.attempts(fresh["id"])[0]["grant_id"]


# -- uncertainty keeps the slot and says so --------------------------------------------------


@pytest.mark.parametrize("status", STUCK)
def test_a_leased_grant_without_evidence_stays_counted_with_one_held_event(tmp_path, status):
    world = World(tmp_path)
    holder, attempt = _unreported_holder(world, status)
    fresh = world.campaign(_spec(world))
    for _ in range(12):
        world.runner.tick()
    assert not _granted(world, fresh["id"])
    assert _events(world, "campaign.stage_waiting_limits", fresh["id"])
    (kept,) = world.attempts(holder)
    # Classified as the running flow's observation would; nothing settled or reconciled.
    assert (kept["status"], kept["failure_class"]) == ("unknown", "observed_unknown")
    assert kept["settled_cents"] is None
    assert _grant(world, attempt["grant_id"])["status"] == "active"
    assert not _freed(world, holder)
    assert world.status(holder) == status
    (event,) = _held(world, holder)
    assert event["reason"] == "leased_grant_unconfirmed"
    assert event["grant_id"] == attempt["grant_id"] and event["attempt_id"] == attempt["id"]
    assert event["campaign_status"] == status and event["attempt_status"] == "unknown"
    assert event["evidence"]["lease_count"] == 1 and event["evidence"]["live_lease_count"] == 0
    assert len(event["evidence"]["operation_ids"]) == 1
    assert event["evidence"]["run_receipt"] is False
    assert attempt["grant_id"] in event["action"]
    (raw,) = _events(world, "campaign.slot_held", holder)
    campaign = world.row("SELECT org_id, project_id FROM campaigns WHERE id=?", holder)
    assert (raw["org_id"], raw["project_id"]) == (campaign["org_id"], campaign["project_id"])


def test_a_new_spell_reports_the_held_grant_again(tmp_path, monkeypatch):
    world = World(tmp_path)
    starts = _count_starts(monkeypatch)
    holder, attempt = _unreported_holder(world, "paused_delegation_lapsed")
    for _ in range(3):
        world.runner.tick()
    assert len(_held(world, holder)) == 1
    # Re-approved: running again, the unknown attempt blocks it (a new spell out of running).
    world.env.approve(dict(world.row("SELECT * FROM campaigns WHERE id=?", holder)))
    for _ in range(4):
        world.runner.tick()
    assert world.status(holder) == "blocked", dump(world, holder)
    first, second = _held(world, holder)
    assert first["campaign_status"] == "paused_delegation_lapsed"
    assert second["campaign_status"] == "blocked" and second["grant_id"] == attempt["grant_id"]
    assert _started_grants(world, starts) == [attempt["grant_id"]]


@pytest.mark.parametrize("status", STUCK)
def test_a_live_leased_grant_is_untouched(tmp_path, status):
    world = World(tmp_path)
    holder = world.campaign(_spec(world))
    cid = holder["id"]
    admit(world, cid)
    cut_reports(world)
    world.poller.run(max_cycles=1)
    (attempt,) = world.attempts(cid)
    assert world.row(
        "SELECT count(*) AS n FROM execution_leases WHERE grant_id=?", attempt["grant_id"]
    )["n"]
    _expire_release(world, attempt)
    _stick(world, cid, status)
    fresh = world.campaign(_spec(world))
    for _ in range(8):
        world.runner.tick()
    (kept,) = world.attempts(cid)
    assert kept["status"] == "admitted" and kept["revision"] == attempt["revision"]
    assert _grant(world, attempt["grant_id"])["status"] == "active"
    assert not _held(world, cid) and not _freed(world, cid)
    assert not _granted(world, fresh["id"])


# -- resume never double-runs ---------------------------------------------------------------


def _count_starts(monkeypatch) -> list[str]:
    """Operation ids of every container the fake adapter starts."""
    starts: list[str] = []
    original = ScriptedAdapter.start

    def counted(self, operation):
        starts.append(operation.operation_id)
        return original(self, operation)

    monkeypatch.setattr(ScriptedAdapter, "start", counted)
    return starts


def _started_grants(world: World, starts: list[str]) -> list[str]:
    return [
        world.row("SELECT grant_id FROM external_operations WHERE operation_id=?", op)["grant_id"]
        for op in starts
    ]


def test_resume_after_a_confirmed_exit_retries_without_double_running(tmp_path, monkeypatch):
    world = World(tmp_path)
    starts = _count_starts(monkeypatch)
    holder, attempt = _exited_holder(world, "paused_delegation_lapsed", retry={"max_attempts": 2})
    clock = _later(world, 5)
    world.runner.tick()
    world.runner.now = clock
    assert _grant(world, attempt["grant_id"])["status"] == "reconciled"
    fresh = world.campaign(_spec(world))
    world.tick_all(fresh["id"])
    assert world.status(fresh["id"]) == "succeeded"
    world.env.approve(dict(world.row("SELECT * FROM campaigns WHERE id=?", holder)))
    assert world.status(holder) == "running"
    world.tick_all(holder)
    assert world.status(holder) == "succeeded", dump(world, holder)
    first, second = world.attempts(holder)
    assert first["id"] == attempt["id"] and first["status"] == "failed_transient"
    assert second["status"] == "succeeded" and second["grant_id"] != attempt["grant_id"]
    # Each grant's container started once: the exited grant was never re-run.
    grants = _started_grants(world, starts)
    assert len(starts) == len(set(starts)) == len(grants) == len(set(grants)) == 3
    assert grants[0] == attempt["grant_id"]
    assert len(_freed(world, holder)) == 1


def test_resume_with_an_unconfirmed_grant_issues_no_new_grant(tmp_path, monkeypatch):
    world = World(tmp_path)
    starts = _count_starts(monkeypatch)
    holder, attempt = _unreported_holder(world, "paused_delegation_lapsed")
    world.runner.tick()
    world.env.approve(dict(world.row("SELECT * FROM campaigns WHERE id=?", holder)))
    for _ in range(6):
        world.runner.tick()
        world.poller.run(max_cycles=1)
    # Still unknown: the campaign blocks on it and never runs the stage beside it.
    assert world.status(holder) == "blocked"
    (only,) = world.attempts(holder)
    assert only["id"] == attempt["id"] and only["status"] == "unknown"
    assert _started_grants(world, starts) == [attempt["grant_id"]]
    assert {a["grant_id"] for a in world.attempts(holder)} == {attempt["grant_id"]}
