"""Standing approvals and scheduled campaign starts (ADR 0042).

Fakes only: an injected runner clock, the in-process control app and the fake worker.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from campaign_runner_harness import World, dump, stage_spec

from tessera_control.campaign_runner import CampaignRunner
from tessera_control.campaign_standing import MAX_STARTS_PER_TICK
from tessera_control.store import Conflict, Forbidden, StoreError, _connect, canonical_digest

METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]
TERMINAL = ("succeeded", "stopped", "failed", "cancelled")


class Sched:
    """A World whose runner reads an injected clock, plus standing-approval helpers."""

    def __init__(self, tmp_path, *, scheduler: bool = True) -> None:
        self.world = World(tmp_path)
        self.env = self.world.env
        self.clock = datetime.now(UTC).replace(microsecond=0)
        self.scheduler = scheduler
        self.world.runner = self.runner()
        self.counter = 0

    def runner(self, **kwargs) -> CampaignRunner:
        return CampaignRunner(
            self.env.state, now=lambda: self.clock, scheduler=self.scheduler, **kwargs
        )

    def spec(self, **overrides) -> dict:
        spec = self.env.spec(**overrides)
        spec["stages"] = [stage_spec(self.env, "s1", metrics=METRIC)]
        return spec

    def body(self, spec=None, **overrides) -> dict:
        spec = spec or self.spec()
        from tessera_control.campaign_models import CampaignSpec

        digest = canonical_digest(CampaignSpec.model_validate(spec).model_dump(mode="json"))
        body = {
            "spec": spec,
            "expected_spec_digest": digest,
            "per_start_budget_cap_cents": 5,
            "max_starts": 3,
            "max_total_budget_cents": 15,
            "not_after": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
        }
        body.update(overrides)
        return body

    def create(self, actor=None, status: int = 201, **kwargs) -> dict:
        body = self.body(**kwargs)
        self.counter += 1
        code, result = self.env.store.create_standing_approval(
            actor or self.env.human(),
            self.env.project["id"],
            f"standing-{self.counter}",
            canonical_digest(body),
            body,
        )
        assert code == status
        return result

    def revoke(self, standing: dict, actor=None) -> dict:
        current = self.standing(standing["id"])
        body = {"expected_revision": current["revision"], "reason": "operator revoke"}
        self.counter += 1
        code, result = self.env.store.revoke_standing_approval(
            actor or self.env.human(),
            self.env.project["id"],
            standing["id"],
            f"revoke-{self.counter}",
            canonical_digest(body),
            body,
        )
        assert code == 200
        return result

    def tick(self, count: int = 1) -> None:
        for _ in range(count):
            self.world.runner.tick()

    def standing(self, standing_id: str) -> dict:
        return dict(
            self.env.query("SELECT * FROM campaign_standing_approvals WHERE id=?", standing_id)[0]
        )

    def starts(self, standing_id: str) -> list:
        return self.env.query(
            "SELECT * FROM campaign_standing_starts WHERE standing_approval_id=? "
            "ORDER BY start_index",
            standing_id,
        )

    def finish(self, campaign_id: str, status: str, reason: str | None = None, receipt=False):
        self.env.execute(
            "UPDATE campaigns SET status=?, stop_reason=? WHERE id=?", status, reason, campaign_id
        )
        if receipt:
            self.env.execute(
                "INSERT INTO campaign_receipts VALUES (?, '{}', ?, 'x')", campaign_id, "d" * 64
            )

    def finish_all(self, standing_id: str, status: str, reason=None, receipt=False) -> None:
        for row in self.starts(standing_id):
            if self.world.status(row["campaign_id"]) not in TERMINAL:
                self.finish(row["campaign_id"], status, reason, receipt)

    def refusals(self, standing_id: str) -> list[str]:
        return [
            e["payload"]["reason"]
            for e in self.env.events("campaign.standing_start_refused")
            if e["payload"]["standing_approval_id"] == standing_id
        ]


@pytest.fixture
def sched(tmp_path):
    return Sched(tmp_path)


def test_human_creates_standing_approval_with_digest_and_provenance(sched):
    standing = sched.create()
    assert standing["id"].startswith("standing_") and standing["status"] == "active"
    assert standing["decision_class"] == "human_session"
    assert standing["trust_policy_digest"] and standing["created_by_principal_id"]
    (event,) = sched.env.events("campaign.standing_approval_created")
    assert event["payload"]["decision_class"] == "human_session"
    assert event["payload"]["standing_approval_id"] == standing["id"]
    listed = sched.env.store.list_standing_approvals(sched.env.human(), sched.env.project["id"])
    assert [s["id"] for s in listed["standing_approvals"]] == [standing["id"]]
    shown = sched.env.store.get_standing_approval(
        sched.env.human(), sched.env.project["id"], standing["id"]
    )
    assert shown["starts"] == [] and shown["triggers"] == [] and "spec" in shown


def test_agent_worker_and_external_scope_cannot_create_read_or_revoke(sched):
    standing = sched.create()
    project = sched.env.project["id"]
    store = sched.env.store
    external = sched.env.agent(["campaign:submit", "campaign:read", "campaign:cancel"], "ext")
    for actor in (sched.env.agent(name="plain"), external, sched.env.worker):
        body = sched.body()
        with pytest.raises(Forbidden):
            store.create_standing_approval(actor, project, "k", canonical_digest(body), body)
        with pytest.raises(Forbidden):
            store.list_standing_approvals(actor, project)
        with pytest.raises(Forbidden):
            store.get_standing_approval(actor, project, standing["id"])
        revoke = {"expected_revision": 1, "reason": "no"}
        with pytest.raises(Forbidden):
            store.revoke_standing_approval(
                actor, project, standing["id"], "r", canonical_digest(revoke), revoke
            )
    assert sched.standing(standing["id"])["status"] == "active"


def test_unlabelled_owner_credential_refused(sched):
    bare = {k: v for k, v in sched.env.owner.items() if k != "automation_label"}
    with pytest.raises(Forbidden):
        sched.create(actor=bare)
    labelled = {**sched.env.owner, "automation_label": "ops-script"}
    created = sched.create(actor=labelled)
    assert created["decision_class"] == "owner_automation_labelled"
    assert created["automation_label"] == "ops-script"


def test_digest_mismatch_and_invalid_spec_refused(sched):
    with pytest.raises(Conflict):
        sched.create(expected_spec_digest="c" * 64)
    broken = sched.spec()
    broken["stages"][0]["inputs"][0]["digest"] = "e" * 64
    with pytest.raises(Conflict):
        sched.create(spec=broken)


def test_per_start_cap_bounds(sched):
    spec = sched.spec()
    spec["stages"][0]["max_exposure_cents"] = 4
    with pytest.raises(StoreError, match="per_start_budget_cap_cents"):
        sched.create(spec=spec, per_start_budget_cap_cents=11)  # above the spec cap (10)
    with pytest.raises(StoreError, match="per_start_budget_cap_cents"):
        sched.create(spec=spec, per_start_budget_cap_cents=3)  # below the largest hold
    with pytest.raises(StoreError, match="max_total"):
        sched.create(spec=spec, per_start_budget_cap_cents=5, max_total_budget_cents=4)
    far = (datetime.now(UTC) + timedelta(days=31)).isoformat()
    with pytest.raises(StoreError, match="not_after"):
        sched.create(spec=spec, not_after=far)
    assert sched.create(spec=spec, per_start_budget_cap_cents=4)["status"] == "active"


def test_follow_on_must_be_same_project_follow_on_trigger_not_self(sched):
    schedule = sched.create()
    with pytest.raises(Conflict):
        sched.create(follow_on_standing_approval_id=schedule["id"])  # not a follow_on trigger
    with pytest.raises(Exception, match="not found"):
        sched.create(follow_on_standing_approval_id="standing_missing")
    target = sched.create(trigger="follow_on")
    chained = sched.create(follow_on_standing_approval_id=target["id"])
    assert chained["follow_on_standing_approval_id"] == target["id"]
    sched.revoke(target)
    with pytest.raises(Conflict):
        sched.create(follow_on_standing_approval_id=target["id"])  # no longer active
    with pytest.raises(sqlite3.IntegrityError):  # self reference is impossible in storage
        sched.env.execute(
            "UPDATE campaign_standing_approvals SET follow_on_standing_approval_id=id WHERE id=?",
            schedule["id"],
        )


def test_scheduled_start_creates_campaign_and_automation_approval(sched):
    standing = sched.create()
    sched.tick()
    (start,) = sched.starts(standing["id"])
    campaign = sched.env.query("SELECT * FROM campaigns WHERE id=?", start["campaign_id"])[0]
    assert campaign["status"] == "running" and campaign["revision"] == 2
    assert campaign["budget_cap_cents"] == 5
    assert campaign["submitted_by_principal_id"] == standing["created_by_principal_id"]
    (approval,) = sched.env.query(
        "SELECT * FROM campaign_approvals WHERE campaign_id=?", campaign["id"]
    )
    assert approval["decision_class"] == "standing_approval"
    assert approval["automation_label"] == f"standing_approval:{standing['id']}"
    assert approval["standing_approval_id"] == standing["id"]
    assert approval["approving_principal_id"] == standing["created_by_principal_id"]
    assert approval["trust_policy_digest"] == standing["trust_policy_digest"]
    expires = datetime.fromisoformat(approval["expires_at"].replace("Z", "+00:00"))
    assert expires == sched.clock + timedelta(seconds=3600)
    assert (start["start_index"], start["kind"], start["budget_cap_cents"]) == (1, "scheduled", 5)
    row = sched.standing(standing["id"])
    assert (row["starts_used"], row["committed_budget_cents"]) == (1, 5)
    approved = [
        e
        for e in sched.env.events("campaign.approved")
        if e["payload"]["campaign_id"] == campaign["id"]
    ][0]["payload"]
    assert approved["standing_approval_id"] == standing["id"] and approved["start_index"] == 1
    assert approved["standing_decision_class"] == "human_session"
    assert sched.env.events("campaign.standing_started")


def test_start_runs_through_existing_delegation_to_success(sched):
    standing = sched.create(max_starts=1, max_total_budget_cents=5)
    world = sched.world
    sched.tick()
    (start,) = sched.starts(standing["id"])
    campaign_id = start["campaign_id"]
    world.tick_all(campaign_id)
    assert world.status(campaign_id) == "succeeded", dump(world, campaign_id)
    receipt = json.loads(
        world.row("SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", campaign_id)[
            "receipt_json"
        ]
    )
    assert receipt["approval"]["decision_class"] == "standing_approval"
    events = world.events_of("campaign.step_completed")
    assert events
    delegated = [
        e
        for e in world.events_of("task.approved")
        if e["payload"].get("campaign_id") == campaign_id
    ]
    assert all(e["payload"]["approval_decision_class"] == "standing_approval" for e in delegated)


def test_max_starts_and_total_budget_exhaust(sched):
    by_count = sched.create(max_starts=2, max_total_budget_cents=100)
    for _ in range(4):
        sched.tick()
        sched.finish_all(by_count["id"], "failed")
    assert len(sched.starts(by_count["id"])) == 2
    assert sched.standing(by_count["id"])["status"] == "exhausted"
    assert len(sched.env.events("campaign.standing_approval_exhausted")) == 1
    by_money = sched.create(max_starts=10, max_total_budget_cents=7)  # one start of 5 fits
    for _ in range(4):
        sched.tick()
        sched.finish_all(by_money["id"], "failed")
    assert len(sched.starts(by_money["id"])) == 1
    assert sched.standing(by_money["id"])["status"] == "exhausted"


def test_min_interval_no_catch_up_after_clock_jump(sched):
    standing = sched.create(min_interval_seconds=600, max_starts=10, max_total_budget_cents=50)
    sched.tick()
    sched.finish_all(standing["id"], "failed")
    sched.tick()
    assert len(sched.starts(standing["id"])) == 1
    sched.clock += timedelta(hours=5)  # many missed intervals
    sched.tick()
    sched.finish_all(standing["id"], "failed")
    sched.tick()
    assert len(sched.starts(standing["id"])) == 2  # one start, never a burst
    sched.clock += timedelta(seconds=601)
    sched.tick()
    assert len(sched.starts(standing["id"])) == 3


def test_max_concurrent_blocks_overlap(sched):
    one = sched.create(max_starts=5, max_total_budget_cents=25)
    sched.tick(3)
    assert len(sched.starts(one["id"])) == 1
    two = sched.create(max_starts=5, max_total_budget_cents=25, max_concurrent=2)
    sched.tick(3)
    assert len(sched.starts(two["id"])) == 2
    sched.finish_all(one["id"], "failed")
    sched.tick()
    assert len(sched.starts(one["id"])) == 2


def test_not_before_not_after_window_and_expiry_event(sched):
    soon = (sched.clock + timedelta(hours=1)).isoformat()
    standing = sched.create(not_before=soon, max_starts=5, max_total_budget_cents=25)
    sched.tick()
    assert sched.starts(standing["id"]) == []
    assert sched.standing(standing["id"])["status"] == "active"
    sched.clock += timedelta(hours=2)
    sched.tick()
    assert len(sched.starts(standing["id"])) == 1
    sched.clock += timedelta(days=3)
    sched.tick()
    assert sched.standing(standing["id"])["status"] == "expired"
    assert len(sched.env.events("campaign.standing_approval_expired")) == 1
    assert len(sched.starts(standing["id"])) == 1  # the running campaign is not affected


def test_project_spend_headroom_refusal_creates_nothing(sched):
    sched.env.execute("UPDATE execution_project_limits SET held_cents=monetary_limit_cents-4")
    standing = sched.create()
    sched.tick(3)
    assert sched.starts(standing["id"]) == []
    assert sched.env.query("SELECT count(*) AS n FROM campaigns")[0]["n"] == 0
    assert sched.refusals(standing["id"]) == ["project_spend_headroom"]
    sched.env.execute("UPDATE execution_project_limits SET held_cents=0")
    sched.tick()
    assert len(sched.starts(standing["id"])) == 1
    assert sched.standing(standing["id"])["last_refusal_reason"] is None


def test_blocked_or_unknown_exposure_refuses_start(sched):
    standing = sched.create(max_starts=5, max_total_budget_cents=25, max_concurrent=2)
    sched.tick()
    (start,) = sched.starts(standing["id"])
    for _ in range(2):  # the fake runner un-blocks it, so re-block before each tick
        sched.env.execute("UPDATE campaigns SET status='blocked' WHERE id=?", start["campaign_id"])
        sched.tick()
    assert len(sched.starts(standing["id"])) == 1
    assert sched.refusals(standing["id"]) == ["exposure_unresolved"]
    sched.finish_all(standing["id"], "failed")
    sched.env.execute(
        """INSERT INTO campaign_stage_attempts (id, campaign_id, stage_key, attempt, status,
        hold_cents, revision, created_at, updated_at) VALUES ('a1', ?, 's1', 5, 'unknown', 0, 1,
        'x', 'x')""",
        start["campaign_id"],
    )
    sched.tick()
    assert len(sched.starts(standing["id"])) == 1


def test_trust_setting_widening_not_inherited_narrowing_refuses(sched):
    from provider_trust_fixture import DERIVED, install_trust

    worker = sched.env.worker["id"]
    # Narrow at creation (public only), then widen: the start records the creation digest.
    install_trust({"local-docker": (["public"], [worker])})
    standing = sched.create(max_starts=2, max_total_budget_cents=10, max_concurrent=2)
    install_trust({"local-docker": (DERIVED, [worker])})
    sched.tick()
    (start,) = sched.starts(standing["id"])
    approval = sched.env.query(
        "SELECT trust_policy_digest FROM campaign_approvals WHERE campaign_id=?",
        start["campaign_id"],
    )[0]
    assert approval["trust_policy_digest"] == standing["trust_policy_digest"]
    # A template that needs derived data, created under the wide setting, is refused after
    # the setting narrows.
    chained = sched.env.spec()
    chained["budget_cap_cents"] = 12
    wide = sched.create(
        spec=chained, per_start_budget_cap_cents=12, max_total_budget_cents=12, max_starts=1
    )
    install_trust({"local-docker": (["public"], [worker])})
    sched.tick(2)
    assert sched.starts(wide["id"]) == []
    assert sched.refusals(wide["id"])


def test_data_class_and_unregistered_provider_refuse_start(sched):
    from campaign_fixture import record_snapshot_private

    # A stored spec naming a provider that is not registered refuses the start.
    other = sched.create()
    spec = json.loads(sched.standing(other["id"])["spec_json"])
    spec["stages"][0]["provider"] = "nowhere-v1"
    sched.env.execute(
        "UPDATE campaign_standing_approvals SET spec_json=? WHERE id=?",
        json.dumps(spec),
        other["id"],
    )
    sched.tick(2)
    assert sched.starts(other["id"]) == []
    assert sched.refusals(other["id"])
    sched.revoke(other)
    # A snapshot re-recorded as private after creation is refused by the data-class check.
    standing = sched.create()
    record_snapshot_private(sched.env.state, sched.env.snapshot_id)
    sched.tick(2)
    assert sched.starts(standing["id"]) == []
    (reason,) = sched.refusals(standing["id"])
    assert "public" in reason.lower()


def test_creator_revoked_principal_stops_starts_and_pauses_running(sched):
    standing = sched.create(max_starts=5, max_total_budget_cents=25, max_concurrent=2)
    sched.tick()
    (start,) = sched.starts(standing["id"])
    sched.env.execute(
        "UPDATE principals SET revoked_at='2026-01-01T00:00:00Z' WHERE id=?",
        standing["created_by_principal_id"],
    )
    sched.tick(3)
    assert len(sched.starts(standing["id"])) == 1
    assert sched.refusals(standing["id"]) == ["creator_not_live"]
    assert sched.world.status(start["campaign_id"]) == "paused_delegation_lapsed"


def test_refusal_event_once_per_reason(sched):
    sched.env.execute("UPDATE execution_project_limits SET held_cents=monetary_limit_cents")
    standing = sched.create()
    sched.tick(5)
    assert sched.refusals(standing["id"]) == ["project_spend_headroom"]
    sched.env.execute("DELETE FROM execution_project_limits")
    sched.tick(3)
    assert sched.refusals(standing["id"]) == ["project_spend_headroom", "no_project_spend_limit"]


def test_revoke_stops_starts_abandons_triggers_running_continue(sched):
    target = sched.create(trigger="follow_on")
    standing = sched.create(
        follow_on_standing_approval_id=target["id"], max_starts=3, max_total_budget_cents=15
    )
    sched.tick()
    (start,) = sched.starts(standing["id"])
    sched.finish(start["campaign_id"], "succeeded", receipt=True)
    sched.tick()  # records the follow-on trigger and starts the target campaign
    revoked_target = sched.revoke(target)
    assert revoked_target["status"] == "revoked"
    # A running campaign of the revoked approval keeps going; new starts stop.
    used = len(sched.starts(standing["id"]))
    result = sched.revoke(standing)
    assert len(result["running_campaign_ids"]) == used - 1  # the finished one is not listed
    sched.tick(3)
    assert len(sched.starts(standing["id"])) == used
    with pytest.raises(Conflict):
        sched.revoke(standing)
    fresh = sched.create()
    sched.tick()
    (running,) = sched.starts(fresh["id"])
    listed = sched.revoke(fresh)
    assert listed["running_campaign_ids"] == [running["campaign_id"]]
    assert sched.world.status(running["campaign_id"]) == "running"
    assert sched.env.events("campaign.standing_approval_revoked")
    sched.tick(2)
    assert len(sched.starts(fresh["id"])) == 1


def test_retry_on_failed_counts_as_start_and_respects_retry_max(sched):
    standing = sched.create(max_starts=10, max_total_budget_cents=50, retry_max=2)
    sched.tick()
    for expected in (2, 3):
        sched.finish_all(standing["id"], "failed", "stage_failed:s1")
        sched.tick(2)
        assert len(sched.starts(standing["id"])) == expected
    last = sched.starts(standing["id"])[-1]
    assert (last["kind"], last["retry_number"]) == ("retry", 2)
    assert last["trigger_campaign_id"] == sched.starts(standing["id"])[1]["campaign_id"]
    sched.finish_all(standing["id"], "failed", "stage_failed:s1")
    sched.tick(2)
    # retry_max reached: the next start is a fresh scheduled one, not a retry
    assert sched.starts(standing["id"])[3]["kind"] == "scheduled"
    assert sched.standing(standing["id"])["starts_used"] == 4
    only_failed = sched.create(max_starts=10, max_total_budget_cents=50, retry_max=1)
    sched.tick()
    sched.finish_all(only_failed["id"], "stopped", "stop_rule:score")
    sched.tick(2)
    assert [r["kind"] for r in sched.starts(only_failed["id"])] == ["scheduled", "scheduled"]


def test_no_retry_after_cancel_budget_refused_blocked_or_held(sched):
    cases = [
        ("cancelled", "cancel: operator", 0),
        ("failed", "budget_refused:s1", 0),
        ("failed", "cancel: operator", 0),
        ("failed", "stage_failed:s1", 3),  # held_cents > 0
    ]
    for status, reason, held in cases:
        standing = sched.create(
            max_starts=5, max_total_budget_cents=25, retry_max=3, trigger="follow_on"
        )
        # a follow_on template is never scheduled; start it directly through a predecessor
        predecessor = sched.create(
            max_starts=1, max_total_budget_cents=5, follow_on_standing_approval_id=standing["id"]
        )
        sched.tick()
        (first,) = sched.starts(predecessor["id"])
        sched.finish(first["campaign_id"], "succeeded", receipt=True)
        sched.tick(2)
        (second,) = sched.starts(standing["id"])
        sched.env.execute(
            "UPDATE campaigns SET held_cents=? WHERE id=?", held, second["campaign_id"]
        )
        sched.finish(second["campaign_id"], status, reason)
        sched.tick(3)
        assert len(sched.starts(standing["id"])) == 1, (status, reason, held)
        assert not sched.env.query(
            "SELECT 1 FROM campaign_standing_triggers WHERE trigger_campaign_id=?",
            second["campaign_id"],
        )
    blocked = sched.create(max_starts=5, max_total_budget_cents=25, retry_max=3)
    sched.tick()
    (start,) = sched.starts(blocked["id"])
    sched.env.execute("UPDATE campaigns SET status='blocked' WHERE id=?", start["campaign_id"])
    sched.tick(3)
    assert len(sched.starts(blocked["id"])) == 1


def test_follow_on_starts_once_after_success_only(sched):
    target = sched.create(trigger="follow_on", max_starts=5, max_total_budget_cents=25)
    source = sched.create(
        follow_on_standing_approval_id=target["id"], max_starts=2, max_total_budget_cents=10
    )
    assert sched.starts(target["id"]) == []
    sched.tick(3)
    assert sched.starts(target["id"]) == []  # a follow_on template never starts on its own
    (first,) = sched.starts(source["id"])
    sched.finish(first["campaign_id"], "failed", "stage_failed:s1")
    sched.tick(2)
    assert sched.starts(target["id"]) == []  # failure never chains
    (second,) = sched.starts(source["id"])[1:]
    sched.finish(second["campaign_id"], "succeeded", receipt=True)
    sched.tick(3)
    (chained,) = sched.starts(target["id"])
    assert (
        chained["kind"] == "follow_on" and chained["trigger_campaign_id"] == second["campaign_id"]
    )
    (trigger,) = sched.env.query(
        "SELECT * FROM campaign_standing_triggers WHERE trigger_campaign_id=?",
        second["campaign_id"],
    )
    assert trigger["status"] == "started" and trigger["start_index"] == 1
    sched.tick(3)
    assert len(sched.starts(target["id"])) == 1
    with pytest.raises(sqlite3.IntegrityError):
        sched.env.execute(
            "INSERT INTO campaign_standing_starts VALUES "
            "(?, 9, 'x', 'y', 'follow_on', ?, 0, 5, 'x')",
            target["id"],
            second["campaign_id"],
        )


def test_follow_on_abandoned_when_target_revoked_or_exhausted(sched):
    revoked = sched.create(trigger="follow_on")
    exhausted = sched.create(trigger="follow_on", max_starts=1, max_total_budget_cents=5)
    for target in (revoked, exhausted):
        source = sched.create(
            follow_on_standing_approval_id=target["id"], max_starts=1, max_total_budget_cents=5
        )
        sched.tick()
        (start,) = sched.starts(source["id"])
        if target is exhausted:
            sched.env.execute(
                "UPDATE campaign_standing_approvals SET starts_used=1, "
                "committed_budget_cents=5 WHERE id=?",
                target["id"],
            )
        else:
            sched.revoke(target)
        sched.finish(start["campaign_id"], "succeeded", receipt=True)
        sched.tick(2)
        (trigger,) = sched.env.query(
            "SELECT * FROM campaign_standing_triggers WHERE trigger_campaign_id=?",
            start["campaign_id"],
        )
        assert trigger["status"] == "abandoned", trigger["reason"]
        assert sched.starts(target["id"]) == []
    assert len(sched.env.events("campaign.standing_trigger_abandoned")) == 2


def test_crash_after_commit_no_double_start(sched):
    standing = sched.create(max_starts=5, max_total_budget_cents=25, max_concurrent=4)
    sched.tick()
    (start,) = sched.starts(standing["id"])
    # A replayed start with the same index is refused by the primary key.
    with pytest.raises(sqlite3.IntegrityError):
        sched.env.execute(
            "INSERT INTO campaign_standing_starts VALUES (?, 1, 'c2', 'a2', 'scheduled', NULL, "
            "0, 5, 'x')",
            standing["id"],
        )
    # Two runners: the lease keeps the second from starting anything in the same instant.
    other = sched.runner(holder_id="runner_other")
    other.tick()
    sched.world.runner.tick()
    assert len(sched.starts(standing["id"])) == 1 + (
        1 if sched.standing(standing["id"])["max_concurrent"] > 1 else 0
    )
    indexes = [r["start_index"] for r in sched.starts(standing["id"])]
    assert indexes == list(range(1, len(indexes) + 1))
    assert sched.starts(standing["id"])[0]["campaign_id"] == start["campaign_id"]
    with pytest.raises(sqlite3.IntegrityError):
        sched.env.execute("DELETE FROM campaign_standing_starts")


def test_restart_resumes_schedule_from_sqlite(sched):
    standing = sched.create(min_interval_seconds=600, max_starts=5, max_total_budget_cents=25)
    sched.tick()
    sched.finish_all(standing["id"], "failed")
    sched.world.restart_runner()
    sched.world.runner = sched.runner()
    sched.tick(2)
    assert len(sched.starts(standing["id"])) == 1  # the interval counts from the committed start
    sched.clock += timedelta(seconds=601)
    sched.world.restart_runner()
    sched.world.runner = sched.runner()
    sched.tick()
    assert len(sched.starts(standing["id"])) == 2


def test_scheduler_disabled_flag(tmp_path):
    off = Sched(tmp_path, scheduler=False)
    standing = off.create()
    off.tick(3)
    assert off.starts(standing["id"]) == []
    assert off.standing(standing["id"])["status"] == "active"
    off.scheduler = True
    off.world.restart_runner()
    off.world.runner = off.runner()
    off.tick()
    assert len(off.starts(standing["id"])) == 1
    from tessera_control import cli

    parsed = cli.parser().parse_args(["serve", "--state-dir", "/x", "--port", "1"])
    assert parsed.no_campaign_scheduler is False
    parsed = cli.parser().parse_args(
        ["serve", "--state-dir", "/x", "--port", "1", "--no-campaign-scheduler"]
    )
    assert parsed.no_campaign_scheduler is True


def test_human_reapproval_of_paused_automated_campaign_keeps_first_bound(sched):
    standing = sched.create()
    sched.tick()
    (start,) = sched.starts(standing["id"])
    campaign_id = start["campaign_id"]
    first = sched.env.query("SELECT * FROM campaign_approvals WHERE campaign_id=?", campaign_id)[0]
    sched.clock += timedelta(seconds=3601)
    sched.tick()
    assert sched.world.status(campaign_id) == "paused_delegation_lapsed"
    current = sched.world.row("SELECT * FROM campaigns WHERE id=?", campaign_id)
    body = {
        "expected_revision": current["revision"],
        "expected_spec_digest": current["spec_digest"],
    }
    sched.env.store.approve_campaign(
        sched.env.human(), campaign_id, "re1", canonical_digest(body), body
    )
    (active,) = sched.env.query(
        "SELECT * FROM campaign_approvals WHERE campaign_id=? AND status='active'", campaign_id
    )
    assert active["decision_class"] == "human_session"
    # The window still ends deadline_seconds after the first (standing) approval.
    bound = datetime.fromisoformat(first["approved_at"].replace("Z", "+00:00")) + timedelta(
        seconds=3600
    )
    assert datetime.fromisoformat(active["expires_at"].replace("Z", "+00:00")) <= bound
    assert first["decision_class"] == "standing_approval"


def test_max_starts_per_tick_is_bounded(sched):
    ids = [
        sched.create(max_starts=1, max_total_budget_cents=5) for _ in range(MAX_STARTS_PER_TICK + 2)
    ]
    sched.tick()
    started = sum(1 for s in ids if sched.starts(s["id"]))
    assert started == MAX_STARTS_PER_TICK
    sched.tick()
    assert sum(1 for s in ids if sched.starts(s["id"])) == len(ids)


def test_connection_helper_used(sched):
    # the storage guards are real: starts rows are append-only
    standing = sched.create()
    sched.tick()
    connection = _connect(sched.env.state / "tessera.sqlite3")
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("UPDATE campaign_standing_starts SET kind='retry'")
    finally:
        connection.close()
    assert sched.starts(standing["id"])


def test_creator_liveness_is_bound_to_the_creating_credential(sched):
    standing = sched.create(max_starts=5, max_total_budget_cents=25)
    creator_credential = standing["created_credential_id"]
    assert creator_credential is not None
    other = sched.env.query(
        "SELECT id FROM credentials WHERE id<>? AND revoked_at IS NULL LIMIT 1", creator_credential
    )[0]["id"]
    # The decision was made under one credential; a different credential for the same
    # principal does not carry it forward.
    sched.env.execute(
        "UPDATE campaign_standing_approvals SET created_credential_id=? WHERE id=?",
        other,
        standing["id"],
    )
    sched.tick(2)
    assert sched.starts(standing["id"]) == []
    assert sched.refusals(standing["id"]) == ["creator_credential_changed"]
    # Expiring the creating credential also stops starts.
    sched.env.execute(
        "UPDATE campaign_standing_approvals SET created_credential_id=? WHERE id=?",
        creator_credential,
        standing["id"],
    )
    sched.env.execute(
        "UPDATE credentials SET expires_at='2000-01-01T00:00:00Z' WHERE id=?", creator_credential
    )
    sched.tick(2)
    assert sched.starts(standing["id"]) == []
    assert sched.refusals(standing["id"])[-1] == "creator_not_live"
