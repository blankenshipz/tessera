"""Approved campaign submissions (ADR 0093): human approval, agent consume, fail closed.

Synthetic fixtures only (no experiment data); the in-process control store and app.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from campaign_fixture import Env
from fastapi.testclient import TestClient

from tessera_control import campaign_submissions, program_budget
from tessera_control.api import create_app
from tessera_control.approval_trust import AUTOMATION_KEY
from tessera_control.campaign_models import CampaignSpec
from tessera_control.campaign_receipts import build_receipt
from tessera_control.campaign_submissions import (
    AgentSubmissionConfig,
    CampaignSubmissionStore,
    SubmissionRefused,
    kill_switch_engaged,
)
from tessera_control.store import Forbidden, NotFound, StoreError, _connect, canonical_digest

SUBMIT = "campaign:submit_cleared"


class Sub:
    def __init__(self, tmp_path) -> None:
        self.env = Env(tmp_path)
        self.store = CampaignSubmissionStore(self.env.state)
        self.counter = 0
        marker_dir = tmp_path / "switch"
        marker_dir.mkdir()
        self.marker = marker_dir / "stop"
        self.on = AgentSubmissionConfig(enabled=True, kill_switch_path=str(self.marker))

    def key(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}-{self.counter}"

    def body(self, **overrides) -> dict:
        spec = self.env.spec()
        digest = canonical_digest(CampaignSpec.model_validate(spec).model_dump(mode="json"))
        body = {
            "kind": "submit",
            "spec": spec,
            "expected_spec_digest": digest,
            "per_run_budget_cap_cents": 8,
            "not_after": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
        }
        body.update(overrides)
        return body

    def create(self, actor=None, **overrides) -> dict:
        body = self.body(**overrides)
        status, result = self.store.create_submission_approval(
            actor or self.env.human(),
            self.env.project["id"],
            self.key("create"),
            canonical_digest(body),
            body,
        )
        assert status == 201
        return result

    def submit(self, record, actor=None, config=None, key=None, revision=None):
        body = {"expected_revision": revision or record["revision"]}
        return self.store.submit_approved(
            actor or self.agent(),
            record["id"],
            key or self.key("submit"),
            canonical_digest(body),
            body,
            config=self.on if config is None else config,
        )

    def refusal(self, record, **kwargs) -> SubmissionRefused:
        with pytest.raises(SubmissionRefused) as caught:
            self.submit(record, **kwargs)
        return caught.value

    def cancel(self, record, actor, revision=None):
        body = {"expected_revision": revision or record["revision"], "reason": "not wanted"}
        return self.store.cancel_submission_approval(
            actor, record["id"], self.key("cancel"), canonical_digest(body), body
        )

    def agent(self, actions=None, name="agent") -> dict:
        return self.env.agent(actions or ["campaign:read", SUBMIT], name=name)

    def row(self, ident: str) -> dict:
        return dict(
            self.env.query("SELECT * FROM campaign_submission_approvals WHERE id=?", ident)[0]
        )

    def decisions(self, ident: str) -> list[dict]:
        return [
            dict(r)
            for r in self.env.query(
                "SELECT * FROM campaign_submission_decisions WHERE approval_id=? "
                "ORDER BY created_at, rowid",
                ident,
            )
        ]

    def campaigns(self) -> int:
        return self.env.query("SELECT count(*) AS n FROM campaigns")[0]["n"]


@pytest.fixture
def sub(tmp_path):
    program_budget.reset()
    yield Sub(tmp_path)
    program_budget.reset()


# -- creation: human only ------------------------------------------------------------------


def test_human_creates_pending_approval_with_provenance(sub):
    record = sub.create()
    assert record["id"].startswith("submission_")
    assert record["status"] == "pending" and record["revision"] == 1
    assert record["campaign_id"] is None and record["kind"] == "submit"
    assert record["decision_class"] == "human_session"
    assert record["per_run_budget_cap_cents"] == 8 and len(record["spec_digest"]) == 64
    (event,) = sub.env.events("campaign.submission_approval_created")
    assert event["payload"]["submission_approval_id"] == record["id"]


@pytest.mark.parametrize("who", ["scoped_agent", "plain_agent", "worker", "bare_owner"])
def test_only_a_human_session_creates(sub, who):
    actor = {
        "scoped_agent": lambda: sub.agent(["campaign:read", SUBMIT, "campaign:submit"]),
        "plain_agent": lambda: sub.env.agent(None, name="plain"),
        "worker": lambda: sub.env.worker,
        # The root owner credential without an automation label is not a decision actor.
        "bare_owner": lambda: {k: v for k, v in sub.env.owner.items() if k != AUTOMATION_KEY},
    }[who]()
    body = sub.body()
    with pytest.raises(Forbidden):
        sub.store.create_submission_approval(
            actor, sub.env.project["id"], "k", canonical_digest(body), body
        )
    assert sub.env.query("SELECT count(*) AS n FROM campaign_submission_approvals")[0]["n"] == 0


def test_create_validates_digest_cap_and_window(sub):
    with pytest.raises(StoreError, match="digest"):
        sub.create(expected_spec_digest="0" * 64)
    with pytest.raises(StoreError, match="per_run_budget_cap_cents"):
        sub.create(per_run_budget_cap_cents=11)  # above the spec cap of 10
    with pytest.raises(StoreError, match="per_run_budget_cap_cents"):
        sub.create(per_run_budget_cap_cents=5)  # below the largest stage exposure
    with pytest.raises(StoreError, match="not_after"):
        sub.create(not_after=(datetime.now(UTC) + timedelta(days=40)).isoformat())
    with pytest.raises(StoreError):
        sub.create(kind="rerun")  # a re-run needs a source campaign and no spec


# -- operator switch and kill switch -------------------------------------------------------


def test_off_by_default_refuses_and_records_once(sub):
    record = sub.create()
    for _ in range(2):
        refused = sub.refusal(record, config=AgentSubmissionConfig())
        assert refused.reason == "feature_disabled" and not refused.permanent
    (decision,) = sub.decisions(record["id"])
    assert decision["decision"] == "refused" and decision["reason"] == "feature_disabled"
    assert decision["actor_principal_id"] == sub.agent()["id"]
    assert sub.row(record["id"])["status"] == "pending"
    assert sub.campaigns() == 0
    assert len(sub.env.events("campaign.submission_refused")) == 1
    # A refusal is not cached under its idempotency key: the same key works once enabled.
    refused = sub.refusal(record, config=AgentSubmissionConfig(), key="fixed")
    status, result = sub.submit(record, key="fixed")
    assert status == 201 and result["campaign_id"]


def test_kill_switch_fails_closed(sub, tmp_path):
    record = sub.create()
    for config in (
        AgentSubmissionConfig(enabled=True),  # no marker path configured
        AgentSubmissionConfig(enabled=True, kill_switch_path=str(tmp_path / "nope" / "stop")),
    ):
        assert kill_switch_engaged(config)
        assert sub.refusal(record, config=config).reason == "kill_switch_engaged"
    sub.marker.write_text("")
    assert sub.refusal(record).reason == "kill_switch_engaged"
    assert sub.campaigns() == 0
    sub.marker.unlink()
    assert not kill_switch_engaged(sub.on)
    assert sub.submit(record)[0] == 201


# -- consume -------------------------------------------------------------------------------


def test_agent_consumes_and_the_human_stays_the_approver(sub):
    record = sub.create()
    agent = sub.agent()
    status, result = sub.submit(record, actor=agent)
    assert status == 201 and result["status"] == "submitted" and result["revision"] == 2
    campaign = dict(sub.env.query("SELECT * FROM campaigns WHERE id=?", result["campaign_id"])[0])
    assert campaign["status"] == "running" and campaign["budget_cap_cents"] == 8
    assert campaign["spec_digest"] == record["spec_digest"]
    human = sub.env.human()
    assert campaign["submitted_by_principal_id"] == human["id"]
    (approval,) = sub.env.query(
        "SELECT * FROM campaign_approvals WHERE campaign_id=?", campaign["id"]
    )
    assert approval["approving_principal_id"] == human["id"] != agent["id"]
    assert approval["decision_class"] == "approved_submission"
    assert approval["automation_label"] == f"approved_submission:{record['id']}"
    assert approval["standing_approval_id"] is None
    assert not sub.env.query("SELECT 1 FROM campaign_review_preapprovals")
    assert not sub.env.query("SELECT 1 FROM campaign_standing_approvals")
    (decision,) = sub.decisions(record["id"])
    assert decision["decision"] == "submitted" and decision["actor_principal_id"] == agent["id"]


def test_agent_without_the_scope_is_refused(sub):
    record = sub.create()
    for actions in (["campaign:read", "campaign:submit"], ["campaign:read", "campaign:cancel"]):
        with pytest.raises(Forbidden):
            sub.submit(record, actor=sub.env.agent(actions, name="-".join(actions)))
    with pytest.raises(Forbidden):
        sub.submit(record, actor=sub.env.agent(None, name="unscoped"))
    with pytest.raises(Forbidden):
        sub.submit(record, actor=sub.env.worker)
    assert sub.decisions(record["id"]) == [] and sub.campaigns() == 0


def test_human_may_consume(sub):
    record = sub.create()
    status, result = sub.submit(record, actor=sub.env.human())
    assert status == 201 and result["campaign_id"]


def test_replays_return_the_same_campaign(sub):
    record = sub.create()
    first = sub.submit(record, key="same")
    again = sub.submit(record, key="same")
    other = sub.submit(record, key="other")
    assert again == first
    assert other[0] == 200 and other[1]["replayed"] is True
    assert other[1]["campaign_id"] == first[1]["campaign_id"]
    assert sub.campaigns() == 1
    assert [d["decision"] for d in sub.decisions(record["id"])] == ["submitted"]


def test_concurrent_consumers_create_one_campaign(sub):
    record = sub.create()
    agent = sub.agent()
    results: list[Any] = []

    def run(index: int) -> None:
        try:
            results.append(sub.submit(record, actor=agent, key=f"race-{index}"))
        except Exception as error:  # noqa: BLE001 - collected for the assertion
            results.append(error)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    ids = {r[1]["campaign_id"] for r in results if isinstance(r, tuple)}
    assert len(ids) == 1 and sub.campaigns() == 1
    assert sorted(r[0] for r in results if isinstance(r, tuple)).count(201) == 1


# -- refusals ------------------------------------------------------------------------------


def test_headroom_refusal_creates_nothing_and_stays_pending(sub):
    record = sub.create()
    sub.env.execute("UPDATE execution_project_limits SET held_cents=monetary_limit_cents-4")
    refused = sub.refusal(record)
    assert refused.reason == "project_spend_headroom" and not refused.permanent
    assert sub.campaigns() == 0 and sub.row(record["id"])["status"] == "pending"
    sub.env.execute("DELETE FROM execution_project_limits")
    assert sub.refusal(record).reason == "no_project_spend_limit"
    reasons = [d["reason"] for d in sub.decisions(record["id"])]
    assert reasons == ["project_spend_headroom", "no_project_spend_limit"]


def test_program_budget_refusal(sub):
    record = sub.create()
    program_budget.mark_broken()
    assert sub.refusal(record).reason == "program_budget_unavailable"
    assert sub.campaigns() == 0
    program_budget.reset()
    assert sub.submit(record)[0] == 201


def test_stale_revision_is_refused(sub):
    record = sub.create()
    assert sub.refusal(record, revision=5).reason == "stale_revision"
    assert sub.row(record["id"])["status"] == "pending"


def test_expired_approval_ends(sub, monkeypatch):
    record = sub.create()
    later = (datetime.now(UTC) + timedelta(days=3)).isoformat().replace("+00:00", "Z")
    monkeypatch.setattr(campaign_submissions, "utc_now", lambda: later)
    refused = sub.refusal(record)
    assert refused.reason == "expired" and refused.permanent
    row = sub.row(record["id"])
    assert row["status"] == "expired" and row["revision"] == 2
    assert [d["decision"] for d in sub.decisions(record["id"])] == ["expired"]
    monkeypatch.undo()
    assert sub.refusal(record, revision=2).reason == "not_pending"
    assert sub.campaigns() == 0


def test_creator_credential_revoked_ends_the_approval(sub):
    record = sub.create()
    sub.env.execute(
        "UPDATE credentials SET revoked_at='2026-01-01T00:00:00Z' WHERE id=?",
        sub.env.human()["credential_id"],
    )
    refused = sub.refusal(record)
    assert refused.permanent and refused.reason.startswith("creator_")
    assert sub.row(record["id"])["status"] == "refused_permanent"
    assert sub.campaigns() == 0


# -- immutability --------------------------------------------------------------------------


def test_terms_and_decisions_cannot_be_rewritten(sub):
    record = sub.create()
    sub.refusal(record, config=AgentSubmissionConfig())
    db = sqlite3.connect(sub.env.state / "tessera.sqlite3")
    try:
        for statement in (
            "UPDATE campaign_submission_approvals SET per_run_budget_cap_cents=10",
            "UPDATE campaign_submission_approvals SET spec_digest='" + "a" * 64 + "'",
            "UPDATE campaign_submission_approvals SET spec_json='{}'",
            "UPDATE campaign_submission_approvals SET not_after='2999-01-01T00:00:00Z'",
            "DELETE FROM campaign_submission_approvals",
            "UPDATE campaign_submission_decisions SET reason='x'",
            "DELETE FROM campaign_submission_decisions",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(statement)
    finally:
        db.close()
    status, result = sub.submit(record)
    db = sqlite3.connect(sub.env.state / "tessera.sqlite3")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE campaign_submission_approvals SET campaign_id=NULL")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE campaign_submission_approvals SET status='pending'")
    finally:
        db.close()


def test_create_payload_cannot_be_reached_by_submit(sub):
    record = sub.create()
    body = {"expected_revision": 1, "per_run_budget_cap_cents": 10}
    with pytest.raises(StoreError):
        sub.store.submit_approved(
            sub.agent(), record["id"], "k", canonical_digest(body), body, config=sub.on
        )


# -- cancel --------------------------------------------------------------------------------


def test_cancel_is_reduce_only_and_scoped(sub):
    record = sub.create()
    with pytest.raises(Forbidden):
        sub.cancel(record, sub.agent())  # no campaign:cancel
    status, result = sub.cancel(record, sub.env.agent(["campaign:cancel"], name="canceller"))
    assert status == 200 and result["status"] == "cancelled" and result["revision"] == 2
    assert [d["decision"] for d in sub.decisions(record["id"])] == ["cancelled"]
    assert sub.refusal(record, revision=2).reason == "not_pending"
    with pytest.raises(StoreError, match="already"):
        sub.cancel(record, sub.env.human(), revision=2)
    assert sub.campaigns() == 0


# -- reads and isolation -------------------------------------------------------------------


def test_reads_are_scoped_to_the_project(sub):
    record = sub.create()
    reader = sub.env.agent(["campaign:read"], name="reader")
    listed = sub.store.list_submission_approvals(reader, sub.env.project["id"], "pending")
    assert [r["id"] for r in listed["submission_approvals"]] == [record["id"]]
    detail = sub.store.get_submission_approval(reader, record["id"])
    assert detail["spec"]["name"] and detail["decisions"] == []
    with pytest.raises(Forbidden):
        sub.store.get_submission_approval(
            sub.env.agent(["campaign:cancel"], name="noread"), record["id"]
        )
    _, other = sub.env.plain.create_project(
        sub.env.owner, "other-project", canonical_digest({"name": "Other"}), {"name": "Other"}
    )
    body = {
        "kind": "agent",
        "audience": "control",
        "project_id": other["id"],
        "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        "external_agent": {"actions": ["campaign:read", SUBMIT], "destination": "planner.local"},
    }
    token = "synthetic-outsider-token-" + "x" * 30
    sub.env.plain.issue_credential(sub.env.owner, "cred-out", canonical_digest(body), body, token)
    outsider = {**sub.env.plain.authenticate(token), "agent_destination": "planner.local"}
    with pytest.raises(NotFound):
        sub.store.get_submission_approval(outsider, record["id"])
    with pytest.raises(NotFound):
        sub.store.list_submission_approvals(outsider, sub.env.project["id"])
    with pytest.raises(NotFound):
        sub.submit(record, actor=outsider)
    assert sub.decisions(record["id"]) == []


# -- receipt and re-run --------------------------------------------------------------------


def test_receipt_shows_the_submission_only_when_there_is_one(sub):
    record = sub.create()
    sub.refusal(record, config=AgentSubmissionConfig())
    _, result = sub.submit(record)
    plain, _ = sub.env.approved()
    connection = _connect(sub.env.state / "tessera.sqlite3")
    try:
        campaign = connection.execute(
            "SELECT * FROM campaigns WHERE id=?", (result["campaign_id"],)
        ).fetchone()
        receipt, _ = build_receipt(connection, campaign, "succeeded")
        other = connection.execute("SELECT * FROM campaigns WHERE id=?", (plain["id"],)).fetchone()
        plain_receipt, _ = build_receipt(connection, other, "succeeded")
    finally:
        connection.close()
    section = receipt["submission"]
    assert section["submission_approval_id"] == record["id"]
    assert section["approved_by_principal_id"] == sub.env.human()["id"]
    assert [d["decision"] for d in section["decisions"]] == ["refused", "submitted"]
    assert receipt["approval"]["decision_class"] == "approved_submission"
    assert "submission" not in plain_receipt


def test_rerun_approval_records_lineage(sub):
    source, _ = sub.env.approved()
    sub.env.execute("UPDATE campaigns SET status='failed' WHERE id=?", source["id"])
    body = {
        "kind": "rerun",
        "source_campaign_id": source["id"],
        "mode": "identical",
        "per_run_budget_cap_cents": 8,
        "not_after": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }
    status, record = sub.store.create_submission_approval(
        sub.env.human(), sub.env.project["id"], "rerun-1", canonical_digest(body), body
    )
    assert status == 201 and record["lineage"]["rerun_of"] == source["id"]
    status, result = sub.submit(record)
    assert status == 201
    (event,) = sub.env.events("campaign.rerun_submitted")
    assert event["payload"]["campaign_id"] == result["campaign_id"]
    assert event["payload"]["rerun_of"] == source["id"]


def _rerun_approval(sub, source, key, cap=8):
    body = {
        "kind": "rerun",
        "source_campaign_id": source["id"],
        "mode": "identical",
        "per_run_budget_cap_cents": cap,
        "not_after": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }
    status, record = sub.store.create_submission_approval(
        sub.env.human(), sub.env.project["id"], key, canonical_digest(body), body
    )
    assert status == 201
    return record


def test_sibling_reruns_cannot_exceed_the_lineage_cap(sub):
    source, _ = sub.env.approved()  # root campaign cap: 10 cents
    sub.env.execute("UPDATE campaigns SET status='failed' WHERE id=?", source["id"])
    first = _rerun_approval(sub, source, "lineage-1")
    second = _rerun_approval(sub, source, "lineage-2")
    status, _result = sub.submit(first)
    assert status == 201
    campaigns = sub.campaigns()
    refused = sub.refusal(second)
    assert refused.reason == "rerun_budget_exceeds_lineage" and not refused.permanent
    assert sub.campaigns() == campaigns
    assert sub.row(second["id"])["status"] == "pending"
    assert [d["reason"] for d in sub.decisions(second["id"])] == ["rerun_budget_exceeds_lineage"]
    assert sub.row(first["id"])["status"] == "submitted"


def test_a_refused_record_does_not_block_a_sibling_record(sub):
    big = sub.create(per_run_budget_cap_cents=10)
    small = sub.create(per_run_budget_cap_cents=8)
    sub.env.execute("UPDATE execution_project_limits SET held_cents=monetary_limit_cents-9")
    assert sub.refusal(big).reason == "project_spend_headroom"
    assert sub.row(big["id"])["status"] == "pending" and sub.campaigns() == 0
    status, result = sub.submit(small)
    assert status == 201 and result["campaign_id"]
    assert sub.row(small["id"])["status"] == "submitted"
    assert sub.row(big["id"])["status"] == "pending"
    assert [d["decision"] for d in sub.decisions(small["id"])] == ["submitted"]


def test_rerun_of_an_unfinished_campaign_is_refused_at_create(sub):
    source, _ = sub.env.approved()
    body = {
        "kind": "rerun",
        "source_campaign_id": source["id"],
        "mode": "identical",
        "per_run_budget_cap_cents": 8,
        "not_after": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }
    with pytest.raises(StoreError, match="re-run"):
        sub.store.create_submission_approval(
            sub.env.human(), sub.env.project["id"], "rerun-2", canonical_digest(body), body
        )


# -- HTTP ----------------------------------------------------------------------------------


def test_routes_default_off_and_return_a_stable_reason(sub):
    sub.agent()  # issues the synthetic agent token
    client = TestClient(create_app(sub.env.state))
    human = {"Authorization": "Bearer synthetic-human-token-" + "x" * 30}
    sub.env.human()
    body = sub.body()
    response = client.post(
        f"/projects/{sub.env.project['id']}/submission-approvals",
        json=body,
        headers={**human, "Idempotency-Key": "c1"},
    )
    assert response.status_code == 201, response.text
    record = response.json()
    agent = {
        "Authorization": "Bearer synthetic-agent-token-" + "x" * 30,
        "X-Agent-Destination": "planner.local",
    }
    denied = client.post(
        f"/projects/{sub.env.project['id']}/submission-approvals",
        json=body,
        headers={**agent, "Idempotency-Key": "c2"},
    )
    assert denied.status_code == 403
    refused = client.post(
        f"/submission-approvals/{record['id']}/submit",
        json={"expected_revision": 1},
        headers={**agent, "Idempotency-Key": "s1"},
    )
    assert refused.status_code == 409
    assert refused.json()["reason"] == "feature_disabled"
    listed = client.get(
        f"/projects/{sub.env.project['id']}/submission-approvals?status=pending", headers=agent
    )
    assert listed.status_code == 200 and len(listed.json()["submission_approvals"]) == 1
    detail = client.get(f"/submission-approvals/{record['id']}", headers=agent)
    assert [d["reason"] for d in detail.json()["decisions"]] == ["feature_disabled"]
