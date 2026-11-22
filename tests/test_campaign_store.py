"""Campaign submit/approve/cancel/read store commands (ADR 0038)."""

from __future__ import annotations

import json
import sqlite3

import pytest
from campaign_fixture import Env, record_snapshot_private

from tessera_control.store import Conflict, Forbidden, NotFound, StoreError, canonical_digest


def test_submit_records_spec_stages_and_event(tmp_path):
    env = Env(tmp_path)
    campaign = env.submit()
    assert campaign["status"] == "submitted" and campaign["revision"] == 1
    stages = env.query("SELECT * FROM campaign_stages ORDER BY topo_index")
    assert [(row["stage_key"], row["status"], row["output_data_class"]) for row in stages] == [
        ("s1", "pending", "private_derived"),
        ("s2", "pending", "private_derived"),
    ]
    event = env.events("campaign.submitted")[0]
    assert event["payload"]["spec_digest"] == campaign["spec_digest"]
    assert event["payload"]["stage_keys"] == ["s1", "s2"]
    assert env.query("SELECT count(*) AS n FROM campaign_approvals")[0]["n"] == 0


def test_submit_replay_and_key_reuse(tmp_path):
    env = Env(tmp_path)
    body = {"spec": env.spec()}
    digest = canonical_digest(body)
    first = env.store.submit_campaign(env.owner, env.project["id"], "k", digest, body)
    again = env.store.submit_campaign(env.owner, env.project["id"], "k", digest, body)
    assert first == again
    assert env.query("SELECT count(*) AS n FROM campaigns")[0]["n"] == 1
    other = {"spec": env.spec(name="different")}
    with pytest.raises(Conflict):
        env.store.submit_campaign(env.owner, env.project["id"], "k", canonical_digest(other), other)


def test_salad_stage_refused_with_409_class(tmp_path):
    from salad_campaign_fixture import salad_stage

    env = Env(tmp_path)
    spec = env.spec()
    spec["stages"] = [salad_stage(env, "s1")]
    spec["budget_cap_cents"] = 50
    body = {"spec": spec}
    with pytest.raises(Conflict, match="not enabled"):
        env.store.submit_campaign(
            env.owner, env.project["id"], "salad", canonical_digest(body), body
        )
    assert env.query("SELECT count(*) AS n FROM campaigns")[0]["n"] == 0


def test_submit_refuses_unknown_worker_and_wrong_snapshot_digest(tmp_path):
    env = Env(tmp_path)
    for spec in (
        env.spec(worker_principal_id="nobody"),
        env.spec(worker_principal_id=env.owner["id"]),
    ):
        body = {"spec": spec}
        with pytest.raises(Conflict):
            env.store.submit_campaign(
                env.owner,
                env.project["id"],
                "w" + spec["worker_principal_id"],
                canonical_digest(body),
                body,
            )
    spec = env.spec()
    spec["stages"][0]["inputs"][0] = {**spec["stages"][0]["inputs"][0], "digest": "b" * 64}
    body = {"spec": spec}
    with pytest.raises(Conflict, match="pinned digest"):
        env.store.submit_campaign(
            env.owner, env.project["id"], "digest", canonical_digest(body), body
        )
    spec = env.spec()
    spec["stages"][0]["inputs"][0] = {**spec["stages"][0]["inputs"][0], "snapshot_id": "missing"}
    body = {"spec": spec}
    with pytest.raises(NotFound):
        env.store.submit_campaign(
            env.owner, env.project["id"], "missing", canonical_digest(body), body
        )


def test_invalid_spec_is_a_store_error_without_echoing_input(tmp_path):
    env = Env(tmp_path)
    body = {"spec": {**env.spec(), "budget_cap_cents": 99999}}
    with pytest.raises(StoreError) as error:
        env.store.submit_campaign(env.owner, env.project["id"], "bad", canonical_digest(body), body)
    assert "99999" not in str(error.value)


def test_approve_requires_human_digest_and_revision(tmp_path):
    env = Env(tmp_path)
    campaign = env.submit()
    body = {
        "expected_revision": campaign["revision"],
        "expected_spec_digest": campaign["spec_digest"],
    }
    digest = canonical_digest(body)
    agent = env.agent()
    with pytest.raises(Forbidden):
        env.store.approve_campaign(agent, campaign["id"], "a1", digest, body)
    stale = {**body, "expected_revision": 5}
    with pytest.raises(Conflict):
        env.store.approve_campaign(
            env.human(), campaign["id"], "a2", canonical_digest(stale), stale
        )
    wrong = {**body, "expected_spec_digest": "c" * 64}
    with pytest.raises(Conflict):
        env.store.approve_campaign(
            env.human(), campaign["id"], "a3", canonical_digest(wrong), wrong
        )
    approved = env.approve(campaign)
    assert approved["status"] == "running" and approved["revision"] == 2
    approval = approved["approval"]
    assert approval["decision_class"] == "human_session" and approval["status"] == "active"
    event = env.events("campaign.approved")[0]
    assert event["payload"]["decision_class"] == "human_session"
    # Approving again is a stale decision, never a second approval.
    with pytest.raises(Conflict):
        env.store.approve_campaign(env.human("h2"), campaign["id"], "a4", digest, body)


def test_external_agent_scopes_gate_submit_read_cancel(tmp_path):
    env = Env(tmp_path)
    body = {"spec": env.spec()}
    digest = canonical_digest(body)
    unscoped = env.agent(["context:read"], "unscoped")
    with pytest.raises(Forbidden):
        env.store.submit_campaign(unscoped, env.project["id"], "k1", digest, body)
    submitter = env.agent(["campaign:submit", "campaign:read", "campaign:cancel"], "sub")
    status, campaign = env.store.submit_campaign(submitter, env.project["id"], "k2", digest, body)
    assert status == 201 and campaign["submitted_by_principal_id"] == submitter["id"]
    assert env.store.get_campaign(submitter, campaign["id"])["id"] == campaign["id"]
    with pytest.raises(Forbidden):
        env.store.get_campaign(unscoped, campaign["id"])
    # Another agent with the cancel scope cannot cancel someone else's campaign.
    other = env.agent(["campaign:cancel"], "other")
    cancel = {"expected_revision": 1, "reason": "stop"}
    with pytest.raises(Forbidden):
        env.store.cancel_campaign(other, campaign["id"], "c1", canonical_digest(cancel), cancel)
    status, cancelled = env.store.cancel_campaign(
        submitter, campaign["id"], "c2", canonical_digest(cancel), cancel
    )
    assert status == 200 and cancelled["status"] == "cancelled"
    # An agent can never approve its own campaign.
    with pytest.raises(Forbidden):
        env.store.approve_campaign(
            submitter,
            campaign["id"],
            "ap",
            canonical_digest({"x": 1}),
            {"expected_revision": 2, "expected_spec_digest": campaign["spec_digest"]},
        )


def test_cancel_running_campaign_is_idempotent_and_reduce_only(tmp_path):
    env = Env(tmp_path)
    campaign, _ = env.approved()
    body = {"expected_revision": campaign["revision"], "reason": "operator stop"}
    digest = canonical_digest(body)
    status, result = env.store.cancel_campaign(env.human(), campaign["id"], "c", digest, body)
    assert status == 200 and result["status"] == "cancel_requested"
    again = env.store.cancel_campaign(env.owner, campaign["id"], "c-other", digest, body)
    assert again[1]["status"] == "cancel_requested"
    assert env.events("campaign.cancel_requested")[0]["payload"]["reason"] == "operator stop"
    env.execute("UPDATE campaigns SET status='succeeded' WHERE id=?", campaign["id"])
    with pytest.raises(Conflict):
        env.store.cancel_campaign(env.owner, campaign["id"], "c3", digest, body)


def test_reapproval_supersedes_and_abandons_pending_intents(tmp_path):
    env = Env(tmp_path)
    campaign, approval = env.approved()
    env.intent(campaign, approval, "create_task", {"project_id": env.project["id"]}, {"a": 1})
    env.execute("UPDATE campaigns SET status='paused_delegation_lapsed' WHERE id=?", campaign["id"])
    current = env.store.get_campaign(env.owner, campaign["id"])
    second = env.approve(current, env.human("second"))
    assert second["approval"]["id"] != approval["id"]
    rows = env.query("SELECT id, status FROM campaign_approvals ORDER BY approved_at")
    assert [row["status"] for row in rows] == ["superseded", "active"]
    assert [row["status"] for row in env.intents(campaign["id"])] == ["abandoned"]


def test_reads_are_tenant_scoped_and_shaped(tmp_path):
    env = Env(tmp_path)
    campaign, approval = env.approved()
    full = env.store.get_campaign(env.owner, campaign["id"])
    assert full["spec"]["name"] == "synthetic campaign"
    assert full["approval"]["id"] == approval["id"]
    assert [stage["stage_key"] for stage in full["stages"]] == ["s1", "s2"]
    assert full["intents"]["counts"] == {}
    listing = env.store.list_campaigns(env.owner, env.project["id"])
    assert [row["id"] for row in listing["campaigns"]] == [campaign["id"]]
    assert "spec_json" not in listing["campaigns"][0]
    with pytest.raises(NotFound):
        env.store.get_campaign(env.owner, "campaign_missing")
    with pytest.raises(NotFound):
        env.store.get_campaign_receipt(env.owner, campaign["id"])
    with pytest.raises(StoreError):
        env.store.list_campaigns(env.owner, env.project["id"], limit=0)


def test_runner_helpers(tmp_path):
    env = Env(tmp_path)
    submitted = env.submit()
    campaign, _ = env.approved()
    with env.store.runner_connection() as connection:
        runnable = env.store.runnable_campaigns(connection)
        assert [row["id"] for row in runnable] == [campaign["id"]]
        assert submitted["id"] not in {row["id"] for row in runnable}
        env.store.record_runner_event(
            connection, runnable[0], "campaign.stage_refused", {"stage_key": "s2"}
        )
    event = env.events("campaign.stage_refused")[0]
    assert event["payload"] == {"campaign_id": campaign["id"], "stage_key": "s2"}
    assert event["actor_principal_id"] == campaign["approval"]["approving_principal_id"]
    with pytest.raises(RuntimeError), env.store.runner_connection() as connection:
        connection.execute("UPDATE campaigns SET held_cents=3")
        raise RuntimeError("boom")
    assert env.query("SELECT held_cents FROM campaigns")[0][0] == 0
    env.execute("UPDATE control_recovery SET state='held_restore' WHERE singleton=1")
    with pytest.raises(Forbidden), env.store.runner_connection():
        pass


def test_other_org_gets_not_found(tmp_path):
    env = Env(tmp_path)
    campaign = env.submit()
    now = "2026-01-01T00:00:00Z"
    env.execute("INSERT INTO organizations VALUES ('org_two','Two',?)", now)
    env.execute(
        "INSERT INTO principals (id, org_id, role, token_hash, revoked_at, created_at) "
        "VALUES ('p_two','org_two','human',?,NULL,?)",
        "f" * 64,
        now,
    )
    env.execute(
        "INSERT INTO credentials (id, principal_id, kind, audience, project_id, parent_id, "
        "expires_at, revoked_at, created_at, created_by_principal_id) "
        "VALUES ('c_two','p_two','local_owner','control',NULL,NULL,NULL,NULL,?,'p_two')",
        now,
    )
    actor = {"id": "p_two", "org_id": "org_two", "kind": "local_owner", "automation_label": "t"}
    body = {"expected_revision": 1, "expected_spec_digest": campaign["spec_digest"]}
    cancel = {"expected_revision": 1, "reason": "no"}
    with pytest.raises(NotFound):
        env.store.get_campaign(actor, campaign["id"])
    with pytest.raises(NotFound):
        env.store.get_campaign_receipt(actor, campaign["id"])
    with pytest.raises(NotFound):
        env.store.list_campaigns(actor, env.project["id"])
    with pytest.raises(NotFound):
        env.store.approve_campaign(actor, campaign["id"], "a", canonical_digest(body), body)
    with pytest.raises(NotFound):
        env.store.cancel_campaign(actor, campaign["id"], "c", canonical_digest(cancel), cancel)
    with pytest.raises(NotFound):
        env.store.submit_campaign(
            actor,
            env.project["id"],
            "s",
            canonical_digest({"spec": env.spec()}),
            {"spec": env.spec()},
        )
    assert json.loads(env.query("SELECT spec_json FROM campaigns")[0][0])["name"]


@pytest.mark.parametrize("status", ["cancel_requested", "blocked"])
def test_reapproval_during_cleanup_keeps_the_status(tmp_path, status):
    env = Env(tmp_path)
    campaign, approval = env.approved()
    env.execute("UPDATE campaigns SET status=? WHERE id=?", status, campaign["id"])
    current = env.store.get_campaign(env.owner, campaign["id"])
    second = env.approve(current, env.human("second"))
    assert second["status"] == status
    assert env.store.get_campaign(env.owner, campaign["id"])["status"] == status
    assert second["approval"]["id"] != approval["id"]
    assert env.events("campaign.approved")[-1]["payload"]["resulting_status"] == status


def test_reapproval_is_refused_for_a_running_or_finished_campaign(tmp_path):
    env = Env(tmp_path)
    campaign, _ = env.approved()
    current = env.store.get_campaign(env.owner, campaign["id"])
    body = {
        "expected_revision": current["revision"],
        "expected_spec_digest": current["spec_digest"],
    }
    for status in ("running", "cancelled"):
        env.execute("UPDATE campaigns SET status=? WHERE id=?", status, campaign["id"])
        with pytest.raises(Conflict, match="not awaiting approval"):
            env.store.approve_campaign(
                env.human("second"), campaign["id"], "again-" + status, canonical_digest(body), body
            )


def _submit_refused(env: Env, match: str) -> None:
    before = env.query("SELECT count(*) AS n FROM campaigns")[0]["n"]
    body = {"spec": env.spec()}
    with pytest.raises(Conflict, match=match):
        env.store.submit_campaign(env.owner, env.project["id"], "m3", canonical_digest(body), body)
    assert env.query("SELECT count(*) AS n FROM campaigns")[0]["n"] == before


def test_a_snapshot_not_recorded_public_cannot_be_declared_public(tmp_path):
    env = Env(tmp_path)
    env.submit()  # public declaration is accepted while the record is public
    record_snapshot_private(env.state, env.snapshot_id, "default")
    _submit_refused(env, "not recorded public")
    # Declaring the class honestly is fine.
    spec = env.spec()
    for stage in spec["stages"]:
        for source in stage["inputs"]:
            if source["kind"] == "snapshot":
                source["data_class"] = "private_derived"
    assert env.submit(spec=spec)["status"] == "submitted"


def test_approve_rechecks_the_recorded_snapshot_class(tmp_path):
    env = Env(tmp_path)
    campaign = env.submit()
    record_snapshot_private(env.state, env.snapshot_id)
    body = {
        "expected_revision": campaign["revision"],
        "expected_spec_digest": campaign["spec_digest"],
    }
    with pytest.raises(Conflict, match="not recorded public"):
        env.store.approve_campaign(env.human(), campaign["id"], "a", canonical_digest(body), body)


def test_a_derived_snapshot_cannot_be_declared_public(tmp_path):
    env = Env(tmp_path)
    record_snapshot_private(env.state, env.snapshot_id, "derived")
    _submit_refused(env, "not recorded public")


def test_a_derived_input_check_passes_the_approval_time_digest(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tessera_control import provider_trust

    env = Env(tmp_path)
    seen = []

    def spy(provider_id, worker, cls, narrowed=None, approval_digest=None, connection=None):
        seen.append((cls, approval_digest))
        return True

    monkeypatch.setattr(provider_trust, "provider_admits_class", spy)
    stage = SimpleNamespace(
        key="s2",
        provider="local-docker",
        send_classes=None,
        inputs=[SimpleNamespace(kind="stage", name="up")],
    )
    provider = SimpleNamespace(
        grant_request=lambda *a: SimpleNamespace(worker_principal_id=env.worker["id"])
    )
    connection = sqlite3.connect(env.state / "tessera.sqlite3")
    try:
        env.store.check_stage_inputs(connection, stage, provider, "digest-at-approval")
    finally:
        connection.close()
    assert seen == [("private_derived", "digest-at-approval")]
