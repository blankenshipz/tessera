"""Campaign retry classification (ADR 0038 section 4): only T1 and T2 are transient."""

from __future__ import annotations

import json

from campaign_runner_harness import World, dump, stage_spec

from tessera_control.campaign_runner import CampaignRunner
from tessera_control.store import canonical_digest

METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]
OK = {"data.json": json.dumps({"score": 3})}


def _world(tmp_path, **stage_extra) -> tuple[World, dict]:
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC, **stage_extra)]
    return world, world.campaign(spec)


def _classes(world, campaign_id):
    return [(a["status"], a["failure_class"]) for a in world.attempts(campaign_id)]


def _count(world, sql, *args):
    return world.row(sql, *args)["n"]


def test_t1_expired_unleased_grant_is_retried_once_with_a_fresh_grant(tmp_path):
    world, campaign = _world(tmp_path, retry={"max_attempts": 2})
    cid = campaign["id"]
    world.tick_until(
        lambda: any(a["status"] == "admitted" for a in world.attempts(cid)), worker=False
    )
    (first,) = world.attempts(cid)
    world.env.execute(
        "UPDATE execution_grants SET expires_at='2000-01-01T00:00:00Z' WHERE id=?",
        first["grant_id"],
    )
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    assert _classes(world, cid) == [
        ("failed_transient", "t1_grant_expired_unleased"),
        ("succeeded", None),
    ]
    one, two = world.attempts(cid)
    assert one["grant_id"] != two["grant_id"] and one["settled_cents"] == 0
    assert one["release_id"] == two["release_id"]  # the campaign-owned release is reused
    # One task and one experiment for the stage, however many attempts it needed.
    assert _count(world, "SELECT count(DISTINCT task_id) AS n FROM execution_grants") == 1
    assert _count(world, "SELECT count(*) AS n FROM execution_grants") == 2
    assert _count(world, "SELECT count(*) AS n FROM campaign_stages WHERE task_id IS NOT NULL") == 1
    assert world.worker_runs == [two["grant_id"]]
    assert (
        world.row("SELECT status FROM execution_grants WHERE id=?", one["grant_id"])["status"]
        == "reconciled"
    )


def test_t2_declared_exit_code_with_a_receipt_is_retried(tmp_path):
    world, campaign = _world(tmp_path, retry={"max_attempts": 2, "transient_exit_codes": [7]})
    cid = campaign["id"]
    world.scenarios = [(7, OK), (0, OK)]
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    assert _classes(world, cid) == [
        ("failed_transient", "t2_declared_exit_code"),
        ("succeeded", None),
    ]
    one, two = world.attempts(cid)
    assert one["exit_code"] == 7 and one["run_receipt_digest"] and two["exit_code"] == 0
    assert len(world.worker_runs) == 2
    receipt = json.loads(
        world.row("SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", cid)[
            "receipt_json"
        ]
    )
    assert [a["status"] for a in receipt["stages"][0]["attempts"]] == [
        "failed_transient",
        "succeeded",
    ]


def test_a_retry_attempt_carries_the_trust_evidence_and_the_receipt_shows_it(tmp_path):
    world, campaign = _world(tmp_path, retry={"max_attempts": 2, "transient_exit_codes": [7]})
    cid = campaign["id"]
    world.scenarios = [(7, OK), (0, OK)]
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    one, two = world.attempts(cid)
    for attempt in (one, two):
        assert attempt["trust_policy_digest"] and attempt["admission_basis"]
    assert two["trust_policy_digest"] == one["trust_policy_digest"]
    assert two["admission_basis"] == one["admission_basis"]
    receipt = json.loads(
        world.row("SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", cid)[
            "receipt_json"
        ]
    )
    second = receipt["stages"][0]["attempts"][1]
    assert second["trust_policy_digest"] and second["admission_basis"]
    rows = world.env.query(
        "SELECT attempt_id FROM campaign_stage_io WHERE campaign_id=? AND direction='input'", cid
    )
    assert {r["attempt_id"] for r in rows} >= {one["id"], two["id"]}
    assert second["io"] and any(i["direction"] == "input" for i in second["io"])


def test_undeclared_exit_code_is_permanent_and_issues_no_second_grant(tmp_path):
    world, campaign = _world(tmp_path, retry={"max_attempts": 3, "transient_exit_codes": [7]})
    cid = campaign["id"]
    world.scenarios = [(9, OK)]
    world.tick_all(cid)
    assert world.status(cid) == "failed", dump(world, cid)
    assert _classes(world, cid) == [("failed_permanent", "undeclared_exit_code")]
    assert len(world.worker_runs) == 1 and world.stages(cid) == {"s1": "failed"}
    assert _count(world, "SELECT count(*) AS n FROM execution_grants") == 1
    ledger = world.row("SELECT held_cents, settled_cents FROM campaigns WHERE id=?", cid)
    assert (ledger["held_cents"], ledger["settled_cents"]) == (0, 0)


def test_attempts_exhausted_fails_the_stage(tmp_path):
    world, campaign = _world(tmp_path, retry={"max_attempts": 2, "transient_exit_codes": [7]})
    cid = campaign["id"]
    world.scenarios = [(7, OK), (7, OK)]
    world.tick_all(cid)
    assert world.status(cid) == "failed", dump(world, cid)
    assert _classes(world, cid) == [
        ("failed_transient", "t2_declared_exit_code"),
        ("failed_transient", "t2_declared_exit_code"),
    ]
    assert world.stages(cid) == {"s1": "failed"} and len(world.worker_runs) == 2
    assert world.row("SELECT stop_reason FROM campaigns WHERE id=?", cid)["stop_reason"].startswith(
        "stage_failed:s1"
    )


def test_a_confirmed_exit_without_a_receipt_is_resolved_and_the_stage_retries_on_a_fresh_grant(
    tmp_path,
):
    """ADR 0053: exit confirmed, run receipt never arrived, grant lapsed. The runner resolves the
    attempt itself (it used to block for a person), releases the grant and re-queues the stage."""
    world, campaign = _world(tmp_path, retry={"max_attempts": 3, "transient_exit_codes": [7]})
    cid = campaign["id"]
    original = world.channel.post

    def lose_receipt(path, body, key):
        if path.endswith("/run-receipt"):
            raise ConnectionError("synthetic receipt loss")
        return original(path, body, key)

    world.channel.post = lose_receipt
    world.tick_until(lambda: len(world.worker_runs) == 1)
    (attempt,) = world.attempts(cid)
    # Exited and confirmed, no run receipt: wait inside the grace, resolve once the grant lapses.
    world.runner.tick()
    assert world.status(cid) == "running"
    assert (
        world.row("SELECT status FROM execution_grants WHERE id=?", attempt["grant_id"])["status"]
        == "active"
    )
    world.env.execute(
        "UPDATE execution_grants SET expires_at='2000-01-01T00:00:00Z' WHERE id=?",
        attempt["grant_id"],
    )
    world.runner.tick()
    first = world.attempts(cid)[0]
    assert (first["status"], first["failure_class"]) == (
        "failed_transient",
        "exit_confirmed_unresolved",
    ), dump(world, cid)
    assert (
        world.row("SELECT status FROM execution_grants WHERE id=?", attempt["grant_id"])["status"]
        == "reconciled"
    )
    assert world.status(cid) != "blocked" and not world.events_of("campaign.blocked")
    assert world.events_of("campaign.attempt_exit_resolved")
    world.runner.tick()
    assert (
        _count(world, "SELECT count(*) AS n FROM execution_grants") == 2
    )  # the retry has its own fresh grant
    assert world.row("SELECT held_cents FROM campaigns WHERE id=?", cid)["held_cents"] == 0


def test_a_single_attempt_stage_whose_exit_was_confirmed_fails_instead_of_hanging(tmp_path):
    world, campaign = _world(tmp_path)
    cid = campaign["id"]
    original = world.channel.post
    world.channel.post = lambda path, body, key: (
        (_ for _ in ()).throw(ConnectionError("lost"))
        if path.endswith("/run-receipt")
        else original(path, body, key)
    )
    world.tick_until(lambda: len(world.worker_runs) == 1)
    (attempt,) = world.attempts(cid)
    world.env.execute(
        "UPDATE execution_grants SET expires_at='2000-01-01T00:00:00Z' WHERE id=?",
        attempt["grant_id"],
    )
    world.tick_all(cid, worker=False)
    assert world.status(cid) == "failed", dump(world, cid)
    assert _classes(world, cid) == [("failed_transient", "exit_confirmed_unresolved")]
    assert (
        world.row("SELECT status FROM execution_grants WHERE id=?", attempt["grant_id"])["status"]
        == "reconciled"
    )


def test_a_human_reconciliation_still_unblocks_a_campaign_the_runner_left_alone(tmp_path):
    """While the worker's lease is live the runner does not resolve a confirmed exit (it may still
    be uploading). A person who reconciles the grant through the evidence route still gets the
    campaign unblocked and finished (ADR 0053)."""
    world, campaign = _world(tmp_path)
    cid = campaign["id"]
    original = world.channel.post
    world.channel.post = lambda path, body, key: (
        (_ for _ in ()).throw(ConnectionError("lost"))
        if path.endswith("/run-receipt")
        else original(path, body, key)
    )
    world.tick_until(lambda: len(world.worker_runs) == 1)
    (attempt,) = world.attempts(cid)
    world.env.execute(
        "UPDATE execution_leases SET expires_at='2999-01-01T00:00:00Z' WHERE grant_id=?",
        attempt["grant_id"],
    )
    for _ in range(3):
        world.runner.tick()
    assert world.status(cid) == "running" and not world.events_of("campaign.attempt_exit_resolved")
    grant = world.row("SELECT revision FROM execution_grants WHERE id=?", attempt["grant_id"])
    body = {
        "expected_revision": grant["revision"],
        "settlement_cents": 0,
        "provider_terminated": True,
    }
    status, _ = world.env.execution.reconcile_execution_grant(
        world.env.owner, attempt["grant_id"], "human-reconcile", canonical_digest(body), body
    )
    assert status in {200, 201}
    world.tick_all(cid, worker=False)
    assert world.status(cid) in {"failed", "succeeded"}, dump(world, cid)
    assert world.events_of("campaign.unblocked")


def test_a_lost_t1_finish_after_a_committed_reconcile_resumes_as_t1(tmp_path, monkeypatch):
    world, campaign = _world(tmp_path, retry={"max_attempts": 2})
    cid = campaign["id"]
    world.tick_until(
        lambda: any(a["status"] == "admitted" for a in world.attempts(cid)), worker=False
    )
    (first,) = world.attempts(cid)
    world.env.execute(
        "UPDATE execution_grants SET expires_at='2000-01-01T00:00:00Z' WHERE id=?",
        first["grant_id"],
    )
    original = CampaignRunner._finish_attempt
    fired = []

    def lossy(self, attempt, status, failure_class, response):
        if not fired:
            fired.append(True)
            raise RuntimeError("synthetic crash")
        return original(self, attempt, status, failure_class, response)

    monkeypatch.setattr(CampaignRunner, "_finish_attempt", lossy)
    world.tick_until(lambda: bool(fired), worker=False)
    assert (
        world.row("SELECT status FROM execution_grants WHERE id=?", first["grant_id"])["status"]
        == "reconciled"
    )
    world.restart_runner()
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    assert _classes(world, cid) == [
        ("failed_transient", "t1_grant_expired_unleased"),
        ("succeeded", None),
    ]
