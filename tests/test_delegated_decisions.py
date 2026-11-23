"""Decisions under an owner-signed delegation (ADR 0122): approve, gate, fail closed, receipts."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from test_approval_delegations import Del, later
from test_delegation_rules import _finished, _rule  # noqa: F401 (the gate fixtures)

from tessera_control.approval_trust import CLASS_OWNER_DELEGATED
from tessera_control.campaign_gates import unlock_receipt_section
from tessera_control.delegated_decisions import (
    DECIDE_SCOPE,
    DelegatedDecisionStore,
    DelegationRefused,
    delegation_receipt_section,
)
from tessera_control.store import Conflict, Forbidden, _connect, canonical_digest


class World(Del):
    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.store = DelegatedDecisionStore(self.env.state)

    def agent(self, scopes=(DECIDE_SCOPE, "campaign:read"), name="decider"):
        return self.env.agent(list(scopes), name=name)

    def campaign(self, cap=10, name="synthetic campaign"):
        return self.env.submit(spec=self.env.spec(budget_cap_cents=cap, name=name))

    def approve(self, campaign, delegation, actor=None, **over):
        body = {
            "delegation_id": delegation["id"],
            "expected_revision": campaign["revision"],
            "expected_spec_digest": campaign["spec_digest"],
            **over,
        }
        return self.store.delegated_approve(
            actor or self.agent(),
            campaign["id"],
            self.key("delegated"),
            canonical_digest(body),
            body,
        )

    def decisions(self, delegation_id):
        return [
            dict(r)
            for r in self.env.query(
                "SELECT * FROM delegated_decisions WHERE delegation_id=? ORDER BY rowid",
                delegation_id,
            )
        ]


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def test_a_met_rule_approves_and_the_receipt_cites_the_delegation(w):
    delegation = w.create()
    campaign = w.campaign()
    status, result = w.approve(campaign, delegation)
    assert status == 200 and result["status"] == "running"
    approval = result["approval"]
    assert approval["decision_class"] == CLASS_OWNER_DELEGATED
    assert approval["delegation_id"] == delegation["id"]
    # The runner acts as the person who signed it, and only while that person is live.
    creator = w.row(delegation["id"])["created_by_principal_id"]
    assert approval["approving_principal_id"] == creator
    from tessera_control.campaign_delegation import approver_is_live

    with _connect(w.env.state / "tessera.sqlite3") as connection:
        assert approver_is_live(connection, approval)
        section = delegation_receipt_section(connection, campaign["id"])
    (decision,) = section["delegation"]["decisions"]
    assert decision["outcome"] == "approved" and decision["delegation_id"] == delegation["id"]
    assert decision["terms_digest"] == w.row(delegation["id"])["terms_digest"]
    payload = w.env.events("campaign.approved")[-1]["payload"]
    assert payload["decision_class"] == CLASS_OWNER_DELEGATED
    assert payload["delegation_id"] == delegation["id"]
    assert w.env.events("campaign.delegated_approval")


def test_usage_counts_toward_the_ceiling_and_the_next_campaign_is_refused(w):
    delegation = w.create(terms=_terms(ceiling=15, per_run=10))
    first = w.campaign(cap=10)
    assert w.approve(first, delegation)[0] == 200
    second = w.campaign(cap=10, name="second")
    with pytest.raises(DelegationRefused) as refused:
        w.approve(second, delegation)
    assert "ceiling_exceeded" in refused.value.reasons
    status = w.env.query("SELECT status FROM campaigns WHERE id=?", second["id"])[0][0]
    assert status == "submitted"  # nothing was approved
    assert (
        w.env.query("SELECT COUNT(*) FROM campaign_approvals WHERE campaign_id=?", second["id"])[0][
            0
        ]
        == 0
    )


def _finish(w, campaign, status, settled=0, held=0):
    with sqlite3.connect(w.env.state / "tessera.sqlite3") as db:
        db.execute(
            "UPDATE campaigns SET status=?, settled_cents=?, held_cents=? WHERE id=?",
            (status, settled, held, campaign["id"]),
        )


def test_a_cancelled_campaign_that_spent_still_counts_against_the_ceiling(w):
    """Approve, spend, cancel, repeat must not release the spend: ceiling 10, per-run 10."""
    delegation = w.create(terms=_terms(ceiling=10, per_run=10))
    first = w.campaign(cap=10)
    assert w.approve(first, delegation)[0] == 200
    _finish(w, first, "cancelled", settled=10)
    second = w.campaign(cap=10, name="second")
    with pytest.raises(DelegationRefused) as refused:
        w.approve(second, delegation)
    assert "ceiling_exceeded" in refused.value.reasons
    assert w.env.query("SELECT status FROM campaigns WHERE id=?", second["id"])[0][0] == "submitted"


def test_a_cancelled_campaign_counts_what_it_held_and_settled_not_its_cap(w):
    from tessera_control.delegation_rules import approval_count, committed_cents

    delegation = w.create(terms=_terms(ceiling=30, per_run=10))
    first = w.campaign(cap=10)
    assert w.approve(first, delegation)[0] == 200
    connection = _connect(w.env.state / "tessera.sqlite3")
    try:
        assert committed_cents(connection, delegation["id"]) == 10  # running: its cap
    finally:
        connection.close()
    _finish(w, first, "cancelled", settled=3)
    connection = _connect(w.env.state / "tessera.sqlite3")
    try:
        assert committed_cents(connection, delegation["id"]) == 3  # finished: what it spent
        assert approval_count(connection, delegation["id"]) == 1  # cancelled still counted
    finally:
        connection.close()
    second = w.campaign(cap=10, name="second")
    assert w.approve(second, delegation)[0] == 200  # 3 + 10 fits under 30


def test_the_summary_and_the_enforced_limit_use_one_count_and_one_total(w):
    import tessera_control.approval_delegations as shown
    import tessera_control.delegation_rules as enforced

    assert shown.committed_cents is enforced.committed_cents
    assert shown.approval_count is enforced.approval_count


def test_a_refusal_is_recorded_once_for_the_owner_and_changes_nothing(w):
    delegation = w.create(terms=_terms(per_run=5))
    campaign = w.campaign(cap=10)
    for _ in range(2):  # asking twice records the same request once
        with pytest.raises(DelegationRefused) as refused:
            w.approve(campaign, delegation)
        assert "per_run_cap_exceeded" in refused.value.reasons
        assert "owner has been asked" in str(refused.value)
    rows = w.decisions(delegation["id"])
    assert [r["outcome"] for r in rows] == ["escalated"]
    assert "per_run_cap_exceeded" in rows[0]["reason"]
    asked = w.store.list_delegation_escalations(w.env.human(), w.project)["escalations"]
    assert [e["campaign_id"] for e in asked] == [campaign["id"]]
    assert w.env.events("campaign.delegation_escalated")
    assert w.env.query("SELECT status FROM campaigns WHERE id=?", campaign["id"])[0][0] == (
        "submitted"
    )


@pytest.mark.parametrize("state", ["revoked", "expired"])
def test_a_revoked_or_expired_delegation_decides_nothing(w, state):
    delegation = w.create()
    campaign = w.campaign()
    if state == "revoked":
        w.revoke(delegation)
        expected = "delegation_revoked"
    else:
        _expire(w, delegation)
        expected = "delegation_expired"
    with pytest.raises(DelegationRefused) as refused:
        w.approve(campaign, delegation)
    assert expected in refused.value.reasons


def test_stale_input_is_a_conflict_not_an_escalation(w):
    delegation = w.create()
    campaign = w.campaign()
    with pytest.raises(Conflict):
        w.approve(campaign, delegation, expected_spec_digest="0" * 64)
    with pytest.raises(Conflict):
        w.approve(campaign, delegation, expected_revision=campaign["revision"] + 1)
    assert w.decisions(delegation["id"]) == []


def test_only_the_enrolled_scope_a_human_or_nothing(w):
    delegation = w.create()
    campaign = w.campaign()
    with pytest.raises(Forbidden):
        w.approve(campaign, delegation, actor=w.agent(("campaign:read",), name="reader"))
    with pytest.raises(Forbidden):
        w.approve(campaign, delegation, actor=w.agent(("campaign:submit",), name="submitter"))
    assert w.env.query("SELECT status FROM campaigns WHERE id=?", campaign["id"])[0][0] == (
        "submitted"
    )
    status, _ = w.approve(campaign, delegation, actor=w.agent())
    assert status == 200


def test_a_campaign_is_approved_once_and_replays_return_the_same_answer(w):
    delegation = w.create()
    campaign = w.campaign()
    key = "same-key"
    body = {
        "delegation_id": delegation["id"],
        "expected_revision": campaign["revision"],
        "expected_spec_digest": campaign["spec_digest"],
    }
    actor = w.agent()
    first = w.store.delegated_approve(actor, campaign["id"], key, canonical_digest(body), body)
    again = w.store.delegated_approve(actor, campaign["id"], key, canonical_digest(body), body)
    assert first == again
    assert len(w.decisions(delegation["id"])) == 1
    with pytest.raises(DelegationRefused) as refused:  # a new key cannot approve it twice
        w.approve({**campaign, "revision": first[1]["revision"]}, delegation)
    assert "campaign_not_submitted" in refused.value.reasons


def test_a_delegated_approval_cannot_carry_what_a_person_must_supply(w):
    # The request model has no unlock and no pre-approval field at all.
    from tessera_control.delegated_decisions import DelegatedApprove

    assert set(DelegatedApprove.model_fields) == {
        "delegation_id",
        "expected_revision",
        "expected_spec_digest",
    }
    delegation = w.create()
    campaign = w.campaign()
    with pytest.raises(Exception):  # noqa: B017 - extra fields are refused by the strict model
        w.approve(campaign, delegation, unlock=[{"campaign_id": "x"}])


def test_the_receipt_section_is_absent_without_a_delegation(w):
    campaign = w.campaign()
    with _connect(w.env.state / "tessera.sqlite3") as connection:
        assert delegation_receipt_section(connection, campaign["id"]) == {}


# -- gates -------------------------------------------------------------------------------------


def _terms(ceiling=100, per_run=20):
    from test_approval_delegations import terms

    return terms(budget_ceiling_cents=ceiling, per_run_cap_cents=per_run)


def _expire(w, delegation):
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    with sqlite3.connect(w.env.state / "tessera.sqlite3") as db:
        db.execute("DROP TRIGGER IF EXISTS approval_delegations_immutable")
        for (name,) in db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='approval_delegations'"
        ).fetchall():
            db.execute(f"DROP TRIGGER {name}")
        db.execute(
            "UPDATE approval_delegations SET expires_at=? WHERE id=?", (past, delegation["id"])
        )


class GateHarness:
    """A finished campaign with a computable bound outcome, and a delegation with a gate rule."""

    def __init__(self, tmp_path):
        from gate_evidence_fixture import GateWorld  # noqa: I001
        from test_delegation_rules import _ensure_column

        self.world = GateWorld(tmp_path)
        with self.world.db() as connection:
            _ensure_column(connection, "campaign_gate_decisions")
        self.lock, self.campaign_id = _finished(self.world)
        self.store = DelegatedDecisionStore(self.world.state)
        from tessera_control.store import Store

        owner = self.world.owner
        body = {
            "kind": "agent",
            "audience": "control",
            "project_id": self.world.project["id"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            "external_agent": {
                "actions": [DECIDE_SCOPE, "campaign:read"],
                "destination": "d.local",
            },
        }
        token = "synthetic-gate-agent-token-" + "x" * 30
        Store(self.world.state).issue_credential(
            owner, "cred-gate", canonical_digest(body), body, token
        )
        self.agent = {**Store(self.world.state).authenticate(token), "agent_destination": "d.local"}

    def delegation(self, rule):
        from tessera_control.delegation_rules import terms_digest
        from tessera_control.delegation_terms import DelegationTerms

        terms = DelegationTerms(
            budget_ceiling_cents=100,
            per_run_cap_cents=20,
            allowed_providers=["local-docker"],
            gate_rules=[rule],
        )
        owner = self.world.owner
        ident = "deleg_" + uuid.uuid4().hex
        with self.world.db() as connection:
            connection.execute(
                """INSERT INTO approval_delegations (id, org_id, project_id,
                created_by_principal_id, created_credential_id, decision_class, automation_label,
                terms_json, terms_digest, expires_at, status, revision, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,'active',1,?)""",
                (
                    ident,
                    owner["org_id"],
                    self.world.project["id"],
                    owner["id"],
                    owner.get("credential_id"),
                    "human_session",
                    None,
                    json.dumps(terms.model_dump(mode="json")),
                    terms_digest(terms),
                    later(),
                    later(0),
                ),
            )
            connection.commit()
        return ident

    def decide(self, delegation_id, rule="improves"):
        with self.world.db() as connection:
            revision = connection.execute(
                "SELECT revision FROM campaigns WHERE id=?", (self.campaign_id,)
            ).fetchone()[0]
        body = {"delegation_id": delegation_id, "rule": rule, "expected_revision": revision}
        return self.store.delegated_gate_decision(
            self.agent, self.campaign_id, "gate-" + uuid.uuid4().hex, canonical_digest(body), body
        )


@pytest.fixture
def gate(tmp_path):
    return GateHarness(tmp_path)


def test_a_rule_that_computes_to_go_records_a_delegated_go_that_unlocks(gate):
    delegation = gate.delegation(_rule(gate.world, gate.lock))
    status, result = gate.decide(delegation)
    assert status == 201 and result["decision"] == "go"
    assert result["decision_class"] == CLASS_OWNER_DELEGATED
    with gate.world.db() as connection:
        row = connection.execute(
            "SELECT * FROM campaign_gate_decisions WHERE id=?", (result["gate_decision_id"],)
        ).fetchone()
        assert (row["decision_class"], row["delegation_id"]) == (CLASS_OWNER_DELEGATED, delegation)
        section = delegation_receipt_section(connection, gate.campaign_id)
        assert section["delegation"]["decisions"][0]["rule"] == "improves"
        # The next campaign's unlock accepts this go, and its receipt names the delegation.
        from tessera_control.campaign_gate_models import UnlockRequirement
        from tessera_control.campaign_gates import _evaluate_basis

        basis, code = _evaluate_basis(
            connection,
            gate.world.owner["org_id"],
            gate.world.project["id"],
            UnlockRequirement(campaign_id=gate.campaign_id, accept_human_go=True),
        )
        assert code is None and basis["gate_decision_id"] == result["gate_decision_id"]
    assert unlock_receipt_section(
        sqlite3.connect(gate.world.state / "tessera.sqlite3"), "none"
    ) == {"requirements": []}


def test_a_failing_rule_records_a_delegated_no_go_that_vetoes(gate):
    delegation = gate.delegation(_rule(gate.world, gate.lock, threshold=10.0))
    status, result = gate.decide(delegation)
    assert status == 201 and result["decision"] == "no_go"


def test_an_unknown_rule_or_an_unevaluable_outcome_escalates_and_records_no_decision(gate):
    delegation = gate.delegation(_rule(gate.world, gate.lock, on_pass=None))
    with pytest.raises(DelegationRefused) as refused:
        gate.decide(delegation)
    assert refused.value.reasons == ["outcome_pass_unmapped"]
    with pytest.raises(DelegationRefused) as unknown:
        gate.decide(delegation, rule="not-a-rule")
    assert "rule_unknown" in unknown.value.reasons
    with gate.world.db() as connection:
        assert connection.execute("SELECT COUNT(*) FROM campaign_gate_decisions").fetchone()[0] == 0
        kinds = [
            r["outcome"] for r in connection.execute("SELECT outcome FROM delegated_decisions")
        ]
    assert kinds == ["escalated", "escalated"]
