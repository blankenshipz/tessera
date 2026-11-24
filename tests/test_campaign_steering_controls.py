"""Pause/resume, a lower-only concurrency limit and single-stage cancel (ADR 0061).

Real control store, real runner, fake worker; no network and no Docker.
"""

from __future__ import annotations

import json

import pytest
from campaign_runner_harness import World, dump, stage_spec

from tessera_control.store import Conflict, Forbidden, NotFound, canonical_digest

ACTIONS = ["campaign:submit", "campaign:read", "campaign:cancel", "campaign:logs"]
METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]
TERMINAL = {"succeeded", "stopped", "failed", "cancelled"}


class Steer:
    def __init__(self, tmp_path, stages="chain"):
        self.world = World(tmp_path)
        self.env = self.world.env
        self.agent = self.env.agent(ACTIONS, "steerer")
        spec = self.env.spec()
        first = stage_spec(self.env, "s1", metrics=METRIC)
        if stages == "chain":
            second = stage_spec(
                self.env,
                "s2",
                metrics=METRIC,
                inputs=[
                    first["inputs"][0],
                    {"kind": "stage", "name": "up", "stage": "s1", "paths": ["data.json"]},
                ],
            )
            spec["stages"] = [first, second]
        else:  # independent stages
            spec["stages"] = [stage_spec(self.env, key, metrics=METRIC) for key in stages]
        self.campaign = self.env.approve(self.env.submit(self.agent, spec))
        self.cid = self.campaign["id"]
        self.n = 0

    def revision(self):
        return self.world.row("SELECT revision FROM campaigns WHERE id=?", self.cid)["revision"]

    def call(self, method, *args, actor=None, reason="because", **extra):
        self.n += 1
        body = {"expected_revision": self.revision(), "reason": reason, **extra}
        status, result = getattr(self.env.store, method)(
            actor or self.agent, self.cid, *args, f"steer-{self.n}", canonical_digest(body), body
        )
        assert status == 200
        return result

    def attempts(self, key):
        return [a for a in self.world.attempts(self.cid) if a["stage_key"] == key]

    def events(self, event_type):
        return [e for e in self.env.events(event_type) if e["payload"]["campaign_id"] == self.cid]


def _leased(s: Steer) -> bool:
    attempts = s.attempts("s1")
    return bool(
        attempts
        and attempts[0]["grant_id"]
        and s.world.row("SELECT 1 FROM execution_leases WHERE grant_id=?", attempts[0]["grant_id"])
    )


def test_pause_stops_new_reservations_and_leaves_the_running_attempt(tmp_path):
    s = Steer(tmp_path)
    w = s.world
    # ADR 0061: the attempt is in flight once the worker took the lease (ADR 0167).
    w.tick_until(lambda: _leased(s))
    paused = s.call("pause_campaign", reason="look first")
    assert paused["paused"] is True and s.events("campaign.paused")
    for _ in range(40):  # the running s1 attempt completes; s2 must not be reserved
        w.runner.tick()
        w.poller.run(max_cycles=1)
        if w.stages(s.cid)["s1"] == "succeeded":
            break
    assert w.stages(s.cid)["s1"] == "succeeded", dump(w, s.cid)
    for _ in range(10):
        w.runner.tick()
    assert s.attempts("s2") == [] and w.status(s.cid) == "running"
    (waiting,) = s.events("campaign.stage_waiting_paused")
    assert waiting["payload"]["stage_key"] == "s2"
    s.call("resume_campaign", reason="continue")
    assert s.events("campaign.resumed")
    w.tick_all(s.cid)
    assert w.status(s.cid) == "succeeded", dump(w, s.cid)
    receipt = json.loads(
        w.row("SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", s.cid)[0]
    )
    assert receipt["steering"] == {
        "paused_at_finish": False,
        "pause_count": 1,
        "max_concurrent_stages": None,
        "stage_cancels": [],
    }


def test_pause_before_the_worker_leases_releases_the_grant_and_resume_places_a_new_attempt(
    tmp_path,
):
    """ADR 0167 amends ADR 0061: a granted but unleased attempt is not in flight, so a pause
    gives its grant back; the attempt uses an attempt number but no retry; resume re-places it."""
    s = Steer(tmp_path)
    w = s.world
    w.tick_until(lambda: s.attempts("s1") and s.attempts("s1")[0]["grant_id"], worker=False)
    first = s.attempts("s1")[0]
    assert not w.row("SELECT 1 FROM execution_leases WHERE grant_id=?", first["grant_id"])
    s.call("pause_campaign", reason="look first")
    for _ in range(6):
        w.runner.tick()
    (released,) = s.attempts("s1")
    assert released["status"] == "failed_transient" and released["failure_class"] == "not_started"
    assert released["settled_cents"] == 0
    grant = w.row("SELECT status FROM execution_grants WHERE id=?", first["grant_id"])
    assert grant["status"] == "reconciled"
    assert [
        e
        for e in s.events("campaign.slot_freed")
        if e["payload"]["reason"] == ("paused_unleased_grant")
    ]
    assert w.status(s.cid) == "running" and not s.events("campaign.slot_reacquired")
    s.call("resume_campaign", reason="continue")
    w.tick_all(s.cid)
    assert w.status(s.cid) == "succeeded", dump(w, s.cid)
    assert [a["attempt"] for a in s.attempts("s1")] == [1, 2]
    assert s.attempts("s1")[1]["status"] == "succeeded"
    (again,) = s.events("campaign.slot_reacquired")
    assert again["payload"]["previous_attempt_id"] == released["id"]
    retries = w.row(
        "SELECT attempts_used FROM campaign_stages WHERE campaign_id=? AND stage_key='s1'", s.cid
    )
    assert retries["attempts_used"] == 2  # an attempt number, not a scientific retry


def test_a_campaign_nobody_steered_has_no_steering_receipt_section(tmp_path):
    s = Steer(tmp_path)
    s.world.tick_all(s.cid)
    receipt = json.loads(
        s.world.row("SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", s.cid)[0]
    )
    assert "steering" not in receipt


def test_pause_does_not_block_cancel_and_double_pause_or_resume_is_refused(tmp_path):
    s = Steer(tmp_path)
    s.call("pause_campaign")
    with pytest.raises(Conflict, match="already paused"):
        s.call("pause_campaign")
    s.call("resume_campaign")
    with pytest.raises(Conflict, match="not paused"):
        s.call("resume_campaign")
    s.call("pause_campaign")
    s.world.cancel(s.campaign)
    for _ in range(30):
        s.world.runner.tick()
        if s.world.status(s.cid) == "cancelled":
            break
    assert s.world.status(s.cid) == "cancelled", dump(s.world, s.cid)


def test_an_agent_cannot_lift_a_pause_a_person_set(tmp_path):
    s = Steer(tmp_path)
    s.call("pause_campaign", actor=s.env.human(), reason="operator hold")
    with pytest.raises(Forbidden, match="person"):
        s.call("resume_campaign")
    s.call("resume_campaign", actor=s.env.human())
    assert s.world.row("SELECT paused FROM campaign_steering WHERE campaign_id=?", s.cid)[0] == 0


def test_steering_needs_the_submitting_agent_the_cancel_scope_and_a_running_campaign(tmp_path):
    s = Steer(tmp_path)
    other = s.env.agent(ACTIONS, "other")
    with pytest.raises(Forbidden):
        s.call("pause_campaign", actor=other)
    reader = s.env.agent(["campaign:read"], "reader")
    with pytest.raises(Forbidden):
        s.call("pause_campaign", actor=reader)
    with pytest.raises(Conflict, match="revision"):
        s.n += 1
        body = {"expected_revision": 999, "reason": "x"}
        s.env.store.pause_campaign(s.agent, s.cid, "k-stale", canonical_digest(body), body)
    s.world.tick_all(s.cid)
    with pytest.raises(Conflict, match="running"):
        s.call("pause_campaign")
    with pytest.raises(Conflict, match="running"):
        s.call("cancel_campaign_stage", "s1")
    assert s.world.row("SELECT count(*) FROM campaign_steering")[0] == 0


def test_the_concurrency_limit_lowers_only_and_serialises_independent_stages(tmp_path):
    s = Steer(tmp_path, stages=("a", "b", "c"))
    w = s.world
    limited = s.call("limit_campaign_concurrency", max_concurrent_stages=1)
    assert limited["previous_max_concurrent_stages"] is None
    with pytest.raises(Conflict, match="lowered"):
        s.call("limit_campaign_concurrency", max_concurrent_stages=1)
    with pytest.raises(Conflict, match="lowered"):
        s.call("limit_campaign_concurrency", max_concurrent_stages=2)
    peak = 0
    for _ in range(200):
        w.runner.tick()
        open_stages = {
            a["stage_key"]
            for a in w.attempts(s.cid)
            if a["status"] in {"reserved", "granted", "admitted", "running", "exited"}
        }
        peak = max(peak, len(open_stages))
        w.poller.run(max_cycles=1)
        if w.status(s.cid) in TERMINAL:
            break
    assert w.status(s.cid) == "succeeded", dump(w, s.cid)
    assert peak == 1
    assert s.events("campaign.stage_waiting_concurrency")
    receipt = json.loads(
        w.row("SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", s.cid)[0]
    )
    assert receipt["steering"]["max_concurrent_stages"] == 1


def test_cancelling_a_pending_stage_skips_its_dependents_and_ends_stopped(tmp_path):
    s = Steer(tmp_path)
    w = s.world
    s.call("pause_campaign")  # nothing has started: both stages are still pending
    done = s.call("cancel_campaign_stage", "s1", reason="not needed")
    assert done["cancel_requested"] is True
    with pytest.raises(Conflict, match="already requested"):
        s.call("cancel_campaign_stage", "s1")
    with pytest.raises(NotFound):
        s.call("cancel_campaign_stage", "nope")
    w.tick_all(s.cid, worker=False, limit=40)
    assert w.stages(s.cid) == {"s1": "cancelled", "s2": "skipped"}
    assert w.status(s.cid) == "stopped"
    row = w.row("SELECT stop_reason FROM campaigns WHERE id=?", s.cid)
    assert row["stop_reason"] == "stage_cancelled"
    (event,) = s.events("campaign.stage_cancelled")
    assert event["payload"]["stage_key"] == "s1" and event["payload"]["reason"] == "not needed"
    receipt = json.loads(
        w.row("SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", s.cid)[0]
    )
    (cancel,) = receipt["steering"]["stage_cancels"]
    assert cancel["stage_key"] == "s1" and cancel["completed_at"]
    assert receipt["steering"]["paused_at_finish"] is True


def test_cancelling_a_running_stage_revokes_its_grant_and_settles(tmp_path):
    s = Steer(tmp_path)
    w = s.world
    w.tick_until(lambda: s.attempts("s1") and s.attempts("s1")[0]["grant_id"], worker=False)
    s.call("cancel_campaign_stage", "s1", reason="wrong input")
    grant_id = s.attempts("s1")[0]["grant_id"]
    # As for a campaign cancel: a grant no worker leased is reconciled once it expires.
    w.tick_until(
        lambda: (
            w.row("SELECT status FROM execution_grants WHERE id=?", grant_id)["status"]
            == "cancel_requested"
        ),
        worker=False,
    )
    assert w.status(s.cid) == "running" and w.stages(s.cid)["s1"] != "cancelled"
    w.env.execute(
        "UPDATE execution_grants SET expires_at='2000-01-01T00:00:00Z' WHERE id=?", grant_id
    )
    w.tick_all(s.cid, worker=False, limit=80)
    assert w.stages(s.cid) == {"s1": "cancelled", "s2": "skipped"}, dump(w, s.cid)
    (attempt,) = s.attempts("s1")
    assert attempt["status"] == "cancelled" and attempt["failure_class"] == "cancelled"
    assert w.status(s.cid) == "stopped"
    grant = w.row("SELECT status FROM execution_grants WHERE id=?", attempt["grant_id"])
    assert grant["status"] in {"revoked", "reconciled", "cancelled"}, dict(grant)
    campaign = w.row("SELECT held_cents FROM campaigns WHERE id=?", s.cid)
    assert campaign["held_cents"] == 0
    assert not w.env.query("SELECT 1 FROM snapshot_releases WHERE status='active'")


def test_a_finished_stage_cannot_be_cancelled(tmp_path):
    s = Steer(tmp_path)
    w = s.world
    w.tick_until(lambda: w.stages(s.cid)["s1"] == "succeeded")
    with pytest.raises(Conflict, match="finished"):
        s.call("cancel_campaign_stage", "s1")


def _replay(s, method, *args, **extra):
    """Send the same request twice under one idempotency key; return both responses."""
    body = {"expected_revision": s.revision(), "reason": "replay", **extra}
    digest = canonical_digest(body)
    first = getattr(s.env.store, method)(s.agent, s.cid, *args, "same-key", digest, body)
    second = getattr(s.env.store, method)(s.agent, s.cid, *args, "same-key", digest, body)
    return first, second


@pytest.mark.parametrize(
    ("method", "args", "extra", "event"),
    [
        ("pause_campaign", (), {}, "campaign.paused"),
        (
            "limit_campaign_concurrency",
            (),
            {"max_concurrent_stages": 1},
            "campaign.concurrency_limited",
        ),
        ("cancel_campaign_stage", ("s2",), {}, "campaign.stage_cancel_requested"),
    ],
)
def test_a_replayed_steering_request_returns_the_same_answer_and_acts_once(
    tmp_path, method, args, extra, event
):
    s = Steer(tmp_path)
    first, second = _replay(s, method, *args, **extra)
    assert first[0] == 200 and second == first
    assert len(s.events(event)) == 1
    assert s.revision() == first[1]["revision"]  # the replay did not bump it again


def test_a_replayed_resume_returns_the_same_answer_and_acts_once(tmp_path):
    s = Steer(tmp_path)
    s.call("pause_campaign")
    first, second = _replay(s, "resume_campaign")
    assert first[0] == 200 and second == first
    assert len(s.events("campaign.resumed")) == 1 and s.revision() == first[1]["revision"]


def test_the_same_key_with_a_different_request_is_refused(tmp_path):
    s = Steer(tmp_path)
    body = {"expected_revision": s.revision(), "reason": "a"}
    s.env.store.pause_campaign(s.agent, s.cid, "k", canonical_digest(body), body)
    other = {"expected_revision": s.revision(), "reason": "b"}
    with pytest.raises(Conflict):
        s.env.store.pause_campaign(s.agent, s.cid, "k", canonical_digest(other), other)


def test_a_non_submitter_cannot_cancel_a_stage_and_nothing_is_recorded(tmp_path):
    s = Steer(tmp_path)
    other = s.env.agent(ACTIONS, "other")
    with pytest.raises(Forbidden):
        s.call("cancel_campaign_stage", "s1", actor=other)
    no_scope = s.env.agent(["campaign:read", "campaign:submit"], "no-cancel-scope")
    with pytest.raises(Forbidden):
        s.call("cancel_campaign_stage", "s1", actor=no_scope)
    assert s.world.row("SELECT count(*) FROM campaign_stage_cancels")[0] == 0
    assert not s.events("campaign.stage_cancel_requested")
