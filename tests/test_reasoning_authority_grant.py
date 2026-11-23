"""Reasoning-only agent authority: model calls without an execution grant, fail closed.

A reasoning grant (``bounds.kind == "reasoning"``) pays for model-runtime calls to one provider
and model, inside the project's model-spend limits and its own budget. It carries no execution
binding and can never be consumed for an execution action, delegated, or parent a runtime
binding. Synthetic only: the in-process control plane and stub transports; no network, no keys.
"""

# ruff: noqa: F811
from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from reasoning_grant_world import DESTINATION, ReasoningGrant
from test_agent_authority import admit_payload, enroll, issue, post, prepared  # noqa: F401
from test_anthropic_runtime_integration import (  # noqa: F401
    MODEL,
    Transport,
    creds,
    ok,
)
from test_execution import _owner
from test_first_call_real_grant import run
from test_remote_reasoning_runtime import Harness
from test_runtime_bindings import runtime  # noqa: F401

from tessera_agent.model_spend_client import GRANT_CODES, GRANT_SCOPE_MISMATCH
from tessera_agent.remote_reasoning_runtime import ReasoningSupervisor
from tessera_agent.remote_reasoning_store import ReasoningStore
from tessera_control.agent_authority_models import IssueAgentAuthority, authority_kind
from tessera_control.api import create_app
from tessera_control.store import Store, initialize_state

MICRO = 1_000_000
QUALIFIED = "anthropic/" + MODEL
SPEND_ACTIONS = ["authority:consume", "campaign:read", "model_spend:record", "provenance:append"]


def reasoning_bounds(subject, **over):
    bounds = {
        "kind": "reasoning",
        "subject_credential_id": subject,
        "destination": DESTINATION,
        "model": MODEL,
        "purpose": "plan critique",
        "provider_id": "anthropic",
        "actions": ["model:call"],
        "budget_cents": 100,
        "max_duration_seconds": 3600,
        "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
    }
    return bounds | over


class Bare:
    """A project with no task, execution grant or experiment: nothing is running."""

    def __init__(self, tmp_path, actions=SPEND_ACTIONS):
        self.state = tmp_path / "state"
        initialize_state(self.state)
        human = _owner(self.state)
        header = (self.state / "owner.header").read_text().strip().split(": ", 1)[1]
        self.owner = {"Authorization": header}
        self.project = Store(self.state).create_project(
            human, "project", "project", {"name": "bounded"}
        )[1]["id"]
        self.client = TestClient(create_app(self.state))
        self.token = "synthetic-reasoning-agent-" + secrets.token_hex(16)
        self.credential = post(
            self.client,
            self.owner,
            "/credentials",
            {
                "token": self.token,
                "kind": "agent",
                "audience": "control",
                "project_id": self.project,
                "expires_at": (datetime.now(UTC) + timedelta(hours=3)).isoformat(),
                "external_agent": {"destination": DESTINATION, "actions": actions},
            },
        )["credential_id"]
        self.agent = {"Authorization": "Bearer " + self.token, "X-Agent-Destination": DESTINATION}
        self.n = 0

    def key(self):
        self.n += 1
        return {"Idempotency-Key": f"bare-{self.n}"}

    def limits(self, provider="*", per_call=5, daily=20):
        body = {
            "project_id": self.project,
            "provider_id": provider,
            "per_call_cents": per_call,
            "daily_cents": daily,
        }
        response = self.client.put(
            "/model-spend/limits", json=body, headers={**self.owner, **self.key()}
        )
        assert response.status_code == 200, response.text

    def issue(self, bounds=None, headers=None, **over):
        bounds = bounds or reasoning_bounds(self.credential, **over)
        return self.client.post(
            f"/projects/{self.project}/agent-authority-grants",
            json={"bounds": bounds},
            headers={**(headers or self.owner), **self.key()},
        )

    def reserve(self, grant_id, worst=1, provider="anthropic", model=QUALIFIED, seq=1):
        body = {
            "project_id": self.project,
            "run_id": "direct",
            "seq": seq,
            "provider_id": provider,
            "model": model,
            "worst_case_cents": worst,
            "grant_id": grant_id,
        }
        return self.client.post(
            "/model-spend/reservations", json=body, headers={**self.agent, **self.key()}
        )

    def read(self, grant_id):
        response = self.client.get(f"/agent-authority-grants/{grant_id}", headers=self.agent)
        assert response.status_code == 200, response.text
        return response.json()

    def rows(self, sql, *args):
        with sqlite3.connect(self.state / "tessera.sqlite3") as db:
            db.row_factory = sqlite3.Row
            return db.execute(sql, args).fetchall()

    def events(self, kind):
        return [
            json.loads(r["payload_json"])
            for r in self.rows("SELECT payload_json FROM events WHERE event_type=?", kind)
        ]


@pytest.fixture
def bare(tmp_path):
    return Bare(tmp_path)


# -- the closed shape --------------------------------------------------------------------------


def test_missing_kind_is_execution_and_never_serialized():
    from test_approval_trust import DIGEST, FUTURE

    binding = {
        "variant_id": "v",
        "expected_variant_revision": 1,
        "expected_design_revision": 1,
        "execution_grant_id": "g",
        "expected_execution_grant_revision": 1,
        "expected_execution_epoch": 1,
        "task_id": "t",
        "expected_task_revision": 1,
        "plan_digest": DIGEST,
        "target_digest": DIGEST,
    }
    execution = {
        "subject_credential_id": "c",
        "destination": "d",
        "model": "m",
        "purpose": "p",
        "actions": ["attempt:admit"],
        "bindings": [binding],
        "budget_cents": 0,
        "max_duration_seconds": 60,
        "expires_at": FUTURE,
    }
    for body in (execution, execution | {"kind": "execution"}):
        dumped = IssueAgentAuthority.model_validate({"bounds": body}).model_dump(mode="json")
        assert "kind" not in dumped["bounds"]  # stored rows and digests are unchanged
        assert authority_kind(dumped["bounds"]) == "execution"
    # An execution body cannot be relabelled reasoning, nor carry a reasoning-only field.
    for bad in (execution | {"kind": "reasoning"}, execution | {"provider_id": "anthropic"}):
        with pytest.raises(ValueError):
            IssueAgentAuthority.model_validate({"bounds": bad})


@pytest.mark.parametrize(
    "over",
    [
        {"budget_cents": 0},
        {"budget_cents": -1},
        {"budget_cents": 1.5},
        {"actions": ["model:call", "attempt:admit"]},
        {"actions": ["attempt:admit"]},
        {"actions": ["authority:delegate"]},
        {"actions": []},
        {"delegation_depth": 1},
        {"provider_id": "*"},
        {"kind": "operator"},
        {"expires_at": "2030-01-01T00:00:00"},
        {"bindings": [{"execution_grant_id": "g"}]},
    ],
)
def test_reasoning_bounds_are_closed(over):
    with pytest.raises(ValueError):
        IssueAgentAuthority.model_validate({"bounds": reasoning_bounds("c", **over)})


# -- issuance ----------------------------------------------------------------------------------


def test_issued_without_any_execution_grant_when_limits_exist(bare):
    bare.limits(provider="anthropic")
    response = bare.issue()
    assert response.status_code == 201, response.text
    grant = response.json()
    assert grant["bounds"]["kind"] == "reasoning"
    assert grant["bounds"]["bindings"] == [] and grant["bounds"]["actions"] == ["model:call"]
    assert grant["bounds"]["delegation_depth"] == 0
    assert grant["remaining_budget_cents"] == 100
    assert bare.rows("SELECT * FROM execution_grants") == []
    (event,) = bare.events("agent_authority.issued")
    assert event["kind"] == "reasoning" and event["grant_id"] == grant["id"]
    (record,) = [
        json.loads(r["envelope_json"])
        for r in bare.rows(
            "SELECT envelope_json FROM agent_provenance WHERE record_id=?", grant["id"]
        )
    ]
    assert record["references"]["authority_kind"] == "reasoning"
    read = bare.read(grant["id"])
    assert (read["status"], read["effective_status"]) == ("active", "active")


def test_the_default_provider_limit_also_configures_it(bare):
    bare.limits(provider="*")
    assert bare.issue().status_code == 201


@pytest.mark.parametrize(
    "limits", [None, {"provider": "openai"}, {"per_call": 0}, {"per_call": 0, "daily": 0}]
)
def test_refused_without_usable_spend_limits(bare, limits):
    if limits is not None:
        bare.limits(**limits)
    response = bare.issue()
    assert response.status_code == 409
    assert "model_spend_not_configured" in response.text
    assert bare.rows("SELECT * FROM agent_authority_grants") == []


def test_refused_when_subject_cannot_record_model_spend(tmp_path):
    world = Bare(tmp_path, actions=["authority:consume", "campaign:read"])
    world.limits()
    assert world.issue().status_code == 403
    assert world.rows("SELECT * FROM agent_authority_grants") == []


@pytest.mark.parametrize(
    "expires",
    [
        lambda: datetime.now(UTC) - timedelta(minutes=1),
        lambda: datetime.now(UTC) + timedelta(hours=25),
        lambda: datetime.now(UTC) + timedelta(hours=4),  # past the credential's expiry
    ],
)
def test_refused_outside_the_expiry_window(bare, expires):
    bare.limits()
    response = bare.issue(expires_at=expires().isoformat())
    assert response.status_code in {403, 409}, response.text
    assert bare.rows("SELECT * FROM agent_authority_grants") == []


def test_only_a_person_or_labelled_automation_issues(bare):
    bare.limits()
    response = bare.issue(headers=bare.agent)
    assert response.status_code == 403
    assert bare.rows("SELECT * FROM agent_authority_grants") == []


@pytest.mark.real_owner_trust
def test_bare_owner_is_refused_and_labelled_automation_recorded(bare):
    labelled = {**bare.owner, "X-Tessera-Automation": "test-script"}
    body = {"project_id": bare.project, "provider_id": "*", "per_call_cents": 5, "daily_cents": 20}
    assert (
        bare.client.put(
            "/model-spend/limits", json=body, headers={**labelled, **bare.key()}
        ).status_code
        == 200
    )
    assert bare.issue().status_code == 403
    assert bare.issue(headers=labelled).status_code == 201


def test_idempotent_replay_returns_the_same_grant(bare):
    bare.limits()
    body = {"bounds": reasoning_bounds(bare.credential)}
    headers = {**bare.owner, "Idempotency-Key": "same"}
    path = f"/projects/{bare.project}/agent-authority-grants"
    first = bare.client.post(path, json=body, headers=headers)
    again = bare.client.post(path, json=body, headers=headers)
    assert first.status_code == again.status_code == 201
    assert first.json()["id"] == again.json()["id"]
    assert len(bare.rows("SELECT * FROM agent_authority_grants")) == 1


def test_an_agent_may_request_reasoning_authority_without_effect(bare):
    response = bare.client.post(
        f"/projects/{bare.project}/agent-authority-requests",
        json={"bounds": reasoning_bounds(bare.credential)},
        headers={**bare.agent, **bare.key()},
    )
    # The reasoning agent holds no authority:request scope: requests stay closed to it.
    assert response.status_code == 403


# -- escalation fences -------------------------------------------------------------------------


def test_reasoning_grant_cannot_be_consumed(prepared):
    state, client, owner, agent, project, bounds, _ = prepared
    world_credential = bounds["subject_credential_id"]
    # A second agent credential (same destination) holds the reasoning grant.
    token = secrets.token_urlsafe(32)
    credential = post(
        client,
        owner,
        "/credentials",
        {
            "token": token,
            "kind": "agent",
            "audience": "control",
            "project_id": project,
            "expires_at": (datetime.now(UTC) + timedelta(hours=3)).isoformat(),
            "external_agent": {
                "destination": "openai:personal",
                "actions": ["authority:consume", "model_spend:record"],
            },
        },
    )["credential_id"]
    assert credential != world_credential
    reasoner = {"Authorization": "Bearer " + token, "X-Agent-Destination": "openai:personal"}
    limits = client.put(
        "/model-spend/limits",
        json={"project_id": project, "provider_id": "*", "per_call_cents": 5, "daily_cents": 20},
        headers={**owner, "Idempotency-Key": "limits"},
    )
    assert limits.status_code == 200, limits.text
    grant = post(
        client,
        owner,
        f"/projects/{project}/agent-authority-grants",
        {
            "bounds": reasoning_bounds(
                credential, destination="openai:personal", model=bounds["model"]
            )
        },
    )
    # Every execution action, with the real execution grant of this project, is refused.
    payload = admit_payload(bounds)
    response = client.post(
        f"/agent-authority-grants/{grant['id']}/consume",
        json=payload,
        headers={**reasoner, "Idempotency-Key": "consume"},
    )
    assert response.status_code == 403, response.text
    assert "Reasoning authority" in response.text
    assert sqlite3.connect(state / "tessera.sqlite3").execute(
        "SELECT COUNT(*) FROM agent_authority_consumptions"
    ).fetchone() == (0,)
    # Delegation of a reasoning grant (to either kind) is refused.
    for child in (
        reasoning_bounds(credential, destination="openai:personal", model=bounds["model"]),
        {**bounds, "subject_credential_id": credential, "delegation_depth": 0},
    ):
        response = client.post(
            f"/agent-authority-grants/{grant['id']}/delegate",
            json={"expected_revision": 1, "expected_epoch": 1, "bounds": child},
            headers={**reasoner, "Idempotency-Key": secrets.token_hex(8)},
        )
        assert response.status_code in {403, 422}, response.text
    assert sqlite3.connect(state / "tessera.sqlite3").execute(
        "SELECT COUNT(*) FROM agent_authority_grants WHERE parent_id IS NOT NULL"
    ).fetchone() == (0,)


def test_execution_grant_cannot_delegate_a_reasoning_child(prepared):
    state, client, owner, agent, project, bounds, _ = prepared
    parent = issue(prepared)
    child = reasoning_bounds(
        bounds["subject_credential_id"],
        destination=bounds["destination"],
        model=bounds["model"],
        budget_cents=1,
    )
    response = client.post(
        f"/agent-authority-grants/{parent['id']}/delegate",
        json={"expected_revision": 1, "expected_epoch": 1, "bounds": child},
        headers={**agent, "Idempotency-Key": "cross-kind"},
    )
    assert response.status_code == 403, response.text
    assert sqlite3.connect(state / "tessera.sqlite3").execute(
        "SELECT COUNT(*) FROM agent_authority_grants WHERE parent_id IS NOT NULL"
    ).fetchone() == (0,)


def test_reasoning_grant_cannot_parent_a_runtime_enrollment(runtime):
    client, owner, project = runtime["client"], runtime["owner"], runtime["project"]
    token = secrets.token_urlsafe(32)
    destination = runtime["enrollment_body"]["destination"]
    subject = post(
        client,
        owner,
        "/credentials",
        {
            "token": token,
            "kind": "agent",
            "audience": "control",
            "project_id": project,
            "expires_at": (datetime.now(UTC) + timedelta(hours=3)).isoformat(),
            "external_agent": {
                "destination": destination,
                "actions": ["authority:consume", "model_spend:record", "context:read"],
            },
        },
    )["credential_id"]
    limits = client.put(
        "/model-spend/limits",
        json={"project_id": project, "provider_id": "*", "per_call_cents": 50, "daily_cents": 50},
        headers={**owner, "Idempotency-Key": "limits"},
    )
    assert limits.status_code == 200, limits.text
    parent = post(
        client,
        owner,
        f"/projects/{project}/agent-authority-grants",
        {
            "bounds": reasoning_bounds(
                subject,
                destination=destination,
                model=runtime["policy_body"]["model"],
                expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            )
        },
    )
    context_body = {
        "credential_id": subject,
        "destination": destination,
        "title": "Reasoning context",
        "body": "Public fixture",
        "classification": "project",
        "disclosure_approved": True,
    }
    context = post(client, owner, f"/projects/{project}/agent-contexts", context_body)
    from tessera_control.store import canonical_digest

    body = {
        **runtime["enrollment_body"],
        "subject_credential_id": subject,
        "context_ref": context["id"],
        "context_digest": canonical_digest(context_body),
        "parent_agent_authority_grant_id": parent["id"],
        "session_ref": "e" * 64,
    }
    response = client.post(
        f"/projects/{project}/runtime-enrollments",
        json=body,
        headers={**owner, "Idempotency-Key": "reasoning-parent"},
    )
    assert response.status_code == 403, response.text
    assert "binding_mismatch" in response.text


# -- the ledger --------------------------------------------------------------------------------


def test_spend_debits_the_grant_and_the_receipt_names_it(bare):
    bare.limits()
    grant = bare.issue(budget_cents=3).json()
    answer = bare.reserve(grant["id"], worst=2)
    assert answer.status_code == 200 and answer.json()["allowed"] is True, answer.text
    call = answer.json()["call_id"]
    assert bare.read(grant["id"])["remaining_budget_cents"] == 1
    settle = bare.client.post(
        f"/model-spend/reservations/{call}/settlement",
        json={"actual_cents": 0.5, "receipt_sha256": "c" * 64},
        headers={**bare.agent, **bare.key()},
    )
    assert settle.status_code == 200, settle.text
    assert bare.read(grant["id"])["remaining_budget_cents"] == 2.5
    (link,) = bare.rows("SELECT * FROM model_spend_grant_links")
    assert (link["call_id"], link["grant_id"]) == (call, grant["id"])
    (reserved,) = bare.events("model_spend.reserved")
    assert reserved["grant_id"] == grant["id"] and reserved["call_id"] == call
    # The budget is a hard cap: a worst case over the remainder is refused, nothing reserved.
    over = bare.reserve(grant["id"], worst=3, seq=2).json()
    assert over["allowed"] is False and over["code"] == "grant_budget_exceeded"
    assert len(bare.rows("SELECT * FROM model_spend_calls")) == 1


@pytest.mark.parametrize(
    "provider,model",
    [("openai", "openai/" + MODEL), ("anthropic", "anthropic/another-model"), ("anthropic", MODEL)],
)
def test_a_reasoning_grant_pays_only_for_its_own_provider_and_model(bare, provider, model):
    bare.limits()
    grant = bare.issue().json()
    answer = bare.reserve(grant["id"], provider=provider, model=model).json()
    assert answer["allowed"] is False and answer["code"] == GRANT_SCOPE_MISMATCH
    assert GRANT_SCOPE_MISMATCH in GRANT_CODES
    assert bare.rows("SELECT * FROM model_spend_calls") == []


def test_revoked_or_expired_reasoning_grant_pays_for_nothing(bare):
    bare.limits()
    grant = bare.issue().json()
    revoke = bare.client.post(
        f"/agent-authority-grants/{grant['id']}/revoke",
        json={"expected_revision": 1, "expected_epoch": 1},
        headers={**bare.owner, **bare.key()},
    )
    assert revoke.status_code == 200, revoke.text
    answer = bare.reserve(grant["id"]).json()
    assert answer["allowed"] is False and answer["code"] == "grant_not_active"
    assert bare.rows("SELECT * FROM model_spend_calls") == []


def test_a_revoked_ancestor_is_seen_at_the_ledger(prepared):
    """The ledger rechecks the whole lineage: a child whose parent was revoked pays nothing."""
    state, client, owner, agent, project, bounds, _ = prepared
    # The prepared agent holds no model_spend:record scope, so the ledger's grant check is
    # exercised on the store directly, inside a transaction as the reservation runs it.
    from tessera_control.grant_model_spend import grant_refusal
    from tessera_control.store import _connect

    parent = issue(prepared)
    child_bounds = {**bounds, "delegation_depth": 0, "budget_cents": 10}
    child = post(
        client,
        agent,
        f"/agent-authority-grants/{parent['id']}/delegate",
        {"expected_revision": 1, "expected_epoch": 1, "bounds": child_bounds},
    )
    actor = Store(state).authenticate(agent["Authorization"].removeprefix("Bearer "))
    with _connect(state / "tessera.sqlite3") as connection:
        refusal, row = grant_refusal(connection, actor, project, child["id"], 1)
        assert refusal is None and row is not None
    post(
        client,
        owner,
        f"/agent-authority-grants/{parent['id']}/revoke",
        {"expected_revision": 1, "expected_epoch": 1},
        expected=200,
    )
    with _connect(state / "tessera.sqlite3") as connection:
        refusal, _ = grant_refusal(connection, actor, project, child["id"], 1)
    assert refusal == {"code": "grant_not_active"}


# -- the runtime fence -------------------------------------------------------------------------


class Bridge:
    def __init__(self, read_only):
        self._read_only = read_only

    def read_only_tools(self):
        if isinstance(self._read_only, Exception):
            raise self._read_only
        return frozenset(self._read_only)


@pytest.fixture
def fenced(tmp_path):
    store = ReasoningStore(tmp_path / "journal")
    binding = {
        "grant_id": "grant1",
        "revision": 1,
        "epoch": 1,
        "subject_credential_id": "agent1",
        "destination": "lab",
        "model": "m1",
        "prompt_sha256": "0" * 64,
    }
    store.bind("run1", binding, 100)
    grant = {
        "revision": 1,
        "epoch": 1,
        "status": "active",
        "effective_status": "active",
        "remaining_budget_cents": 20,
        "bounds": {
            "kind": "reasoning",
            "subject_credential_id": "agent1",
            "destination": "lab",
            "model": "m1",
            "provider_id": "anthropic",
            "actions": ["model:call"],
            "bindings": [],
            "budget_cents": 20,
            "max_duration_seconds": 60,
            "expires_at": "1970-01-01T00:04:00+00:00",
        },
    }
    profile = {
        "max_duration_seconds": 60,
        "max_tokens": 100,
        "model": {"providerID": "anthropic", "id": "m1"},
    }
    harness = Harness()
    supervisor = ReasoningSupervisor(store, harness, lambda _: grant, profile, clock=lambda: 100)
    yield store, supervisor, harness, grant
    store.close()


PAST_THE_FENCES = "prompt_required_or_changed"  # only the deliberately missing prompt stops it


def test_runtime_accepts_a_matching_reasoning_grant(fenced):
    _, supervisor, harness, _ = fenced
    assert supervisor.tick("run1", None)["reason"] == PAST_THE_FENCES
    assert harness.calls[0] == "create"


@pytest.mark.parametrize(
    "change,code",
    [
        ({"kind": "operator"}, "grant_kind_unsupported"),
        ({"provider_id": "openai"}, "grant_scope_changed"),
        ({"actions": ["attempt:status"]}, "grant_scope_changed"),
        ({"actions": []}, "grant_scope_changed"),
        ({"bindings": [{"execution_grant_id": "g"}]}, "grant_scope_changed"),
    ],
)
def test_runtime_blocks_a_reasoning_grant_outside_its_scope(fenced, change, code):
    store, supervisor, harness, grant = fenced
    grant["bounds"].update(change)
    row = supervisor.tick("run1", "bounded goal")
    assert (row["state"], row["reason"]) == ("stop_acknowledged", code)
    assert harness.calls == [] and store.effect("run1", "create") is None


@pytest.mark.parametrize(
    "read_only", [set(), {"tessera_status"}, RuntimeError("bridge down"), None]
)
def test_runtime_blocks_tools_not_marked_read_only_before_the_first_call(fenced, read_only):
    store, supervisor, harness, _ = fenced
    supervisor._allowed_tools = frozenset({"tessera_status", "tessera_submit"})
    if read_only is not None:
        harness.tool_bridge = Bridge(read_only)
    row = supervisor.tick("run1", "bounded goal")
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "tool_not_read_only")
    assert harness.calls == []


def test_runtime_allows_tools_all_marked_read_only(fenced):
    _, supervisor, harness, _ = fenced
    supervisor._allowed_tools = frozenset({"tessera_status"})
    harness.tool_bridge = Bridge({"tessera_status", "tessera_other"})
    assert supervisor.tick("run1", None)["reason"] == PAST_THE_FENCES
    assert harness.calls[0] == "create"


def test_execution_grants_keep_their_tool_contract(fenced):
    _, supervisor, harness, grant = fenced
    for field in ("kind", "provider_id"):
        grant["bounds"].pop(field)
    grant["bounds"]["actions"] = ["attempt:status"]
    supervisor._allowed_tools = frozenset({"tessera_submit"})
    assert supervisor.tick("run1", None)["reason"] == PAST_THE_FENCES
    assert harness.calls[0] == "create"


def test_bridge_reports_only_annotated_allowed_tools():
    from tessera_agent.mcp_tool_bridge import McpToolBridge, ToolBridgeBlocked

    bridge = McpToolBridge.__new__(McpToolBridge)
    bridge.allowed = frozenset({"a", "b", "c", "d"})
    bridge._catalog = None
    with pytest.raises(ToolBridgeBlocked):
        bridge.read_only_tools()
    bridge._catalog = [
        {"name": "a", "read_only": True},
        {"name": "b", "read_only": False},
        {"name": "c"},
        {"name": "d", "read_only": True},
        {"name": "d", "read_only": True},
        {"name": "e", "read_only": True},
    ]
    assert bridge.read_only_tools() == {"a"}


# -- end to end: the first receipted call with no experiment running ---------------------------


class ReasoningOnly(ReasoningGrant):
    """``ReasoningGrant`` with a reasoning-kind grant in a project where nothing is running."""

    def __init__(self, world: Bare, *, budget_cents=100):
        self.state, self.client, self.owner, self.project = (
            world.state,
            world.client,
            world.owner,
            world.project,
        )
        self.token, self.agent, self.n = world.token, world.agent, 100
        self.world = world
        world.limits(per_call=5, daily=20)
        self.bounds = reasoning_bounds(world.credential, budget_cents=budget_cents)
        self.issued = post(
            self.client,
            self.owner,
            f"/projects/{self.project}/agent-authority-grants",
            {"bounds": self.bounds},
        )


def test_first_receipted_call_settles_on_a_reasoning_grant(
    bare, tmp_path, creds, monkeypatch, capsys
):
    grant = ReasoningOnly(bare)
    transport = Transport(ok())
    code, receipt, seen = run(tmp_path, grant, creds, monkeypatch, capsys, transport)
    assert code == 0 and receipt["state"] == "completed", receipt
    assert all(status < 400 for _, _, status in seen)
    (call,) = receipt["model_calls"]
    (row,) = grant.rows("SELECT * FROM model_spend_calls")
    assert row["status"] == "settled" and row["model"] == QUALIFIED
    (link,) = grant.rows("SELECT * FROM model_spend_grant_links")
    assert link["grant_id"] == grant.issued["id"] and link["call_id"] == row["id"]
    after = grant.read()
    assert after["remaining_budget_cents"] == pytest.approx(100 - row["settled_micro"] / MICRO)
    assert grant.rows("SELECT * FROM execution_grants") == []
    assert grant.rows("SELECT * FROM agent_authority_consumptions") == []


def test_a_revoked_reasoning_grant_stops_the_next_run_before_the_transport(
    bare, tmp_path, creds, monkeypatch, capsys
):
    grant = ReasoningOnly(bare)
    grant.revoke()
    transport = Transport(ok())
    code, receipt, _ = run(
        tmp_path, grant, creds, monkeypatch, capsys, transport, revision=2, epoch=2
    )
    assert code == 2 and receipt["reason"] == "grant_revoked"
    assert transport.sent == [] and receipt["model_calls"] == []
    assert grant.rows("SELECT * FROM model_spend_calls") == []


def test_a_reasoning_grant_for_another_provider_never_reaches_the_transport(
    tmp_path, creds, monkeypatch, capsys
):
    world = Bare(tmp_path)
    world.limits()
    issued = post(
        world.client,
        world.owner,
        f"/projects/{world.project}/agent-authority-grants",
        {"bounds": reasoning_bounds(world.credential, provider_id="openai")},
    )
    grant = ReasoningOnly.__new__(ReasoningOnly)
    grant.state, grant.client, grant.owner, grant.project = (
        world.state,
        world.client,
        world.owner,
        world.project,
    )
    grant.token, grant.agent, grant.n, grant.issued = world.token, world.agent, 100, issued
    transport = Transport(ok())
    code, receipt, _ = run(tmp_path, grant, creds, monkeypatch, capsys, transport)
    assert code == 2 and receipt["reason"] == "grant_scope_changed"
    assert transport.sent == [] and grant.rows("SELECT * FROM model_spend_calls") == []
