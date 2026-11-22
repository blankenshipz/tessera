"""Approval-trust policy: root owner decisions must be labelled automation."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_credential_access_leases import _payload as _lease_payload
from test_dataset_ingestion import _command as _ingestion_command
from test_dataset_ingestion import _setup as _ingestion_setup
from test_dataset_ingestion import _snapshot_payload
from test_input_delivery import _delivery

from tessera_control.api import create_app
from tessera_control.approval_trust import AUTOMATION_HEADER
from tessera_control.credential_leases import CredentialAccessLeaseStore
from tessera_control.store import Store, canonical_digest, initialize_state

pytestmark = pytest.mark.real_owner_trust

LABEL = {"X-Tessera-Automation": "test-script"}
FUTURE = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
DIGEST = "a" * 64


@pytest.fixture
def state(tmp_path: Path) -> Path:
    path = tmp_path / "state"
    initialize_state(path)
    return path


@pytest.fixture
def client(state: Path) -> TestClient:
    return TestClient(create_app(state))


def owner(state: Path) -> dict[str, str]:
    name, value = (state / "owner.header").read_text().strip().split(": ", 1)
    return {name: value}


def mut(headers: dict[str, str], key: str, **extra: str) -> dict[str, str]:
    return headers | {"Idempotency-Key": key} | extra


def bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


def issue(client, state, kind, label, project_id=None, audience="control"):
    value = f"{label}_{'x' * 48}"
    response = client.post(
        "/credentials",
        headers=mut(owner(state), f"issue-{label}"),  # no label: issuing is not a decision
        json={
            "token": value,
            "kind": kind,
            "audience": audience,
            "project_id": project_id,
            "expires_at": FUTURE,
        },
    )
    assert response.status_code == 201, response.text
    return bearer(value)


def draft_task(client, state, key):
    project = client.post(
        "/projects", headers=mut(owner(state), f"p-{key}"), json={"name": "Trust"}
    ).json()
    task = client.post(
        f"/projects/{project['id']}/tasks",
        headers=mut(owner(state), f"t-{key}"),
        json={
            "title": "Approve me",
            "purpose": "Exercise the approval trust policy.",
            "budget_cents": 0,
            "max_duration_seconds": 60,
        },
    ).json()
    return project, task


def approve_body(task):
    return {"expected_revision": task["revision"], "expected_plan_digest": None}


def events(state: Path, event_type: str) -> list[dict]:
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        rows = db.execute(
            "SELECT payload_json FROM events WHERE event_type=? ORDER BY rowid", (event_type,)
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def test_issuing_human_session_still_works_with_bare_owner(client, state):
    credential = issue(client, state, "human_session", "human")
    assert client.get("/me", headers=credential).status_code == 200


def test_task_approval_owner_without_label_is_refused(client, state):
    _, task = draft_task(client, state, "a")
    response = client.post(
        f"/tasks/{task['id']}/approve",
        headers=mut(owner(state), "approve-a"),
        json=approve_body(task),
    )
    assert response.status_code == 403
    assert "human_session" in response.json()["detail"]
    assert "X-Tessera-Automation" in response.json()["detail"]
    assert events(state, "task.approved") == []


def test_task_approval_owner_with_label_is_recorded_as_automation(client, state):
    _, task = draft_task(client, state, "b")
    response = client.post(
        f"/tasks/{task['id']}/approve",
        headers=mut(owner(state), "approve-b", **LABEL),
        json=approve_body(task),
    )
    assert response.status_code == 200, response.text
    (event,) = events(state, "task.approved")
    assert event["credential_kind"] == "local_owner"
    assert event["automation"] is True
    assert event["automation_label"] == "test-script"


def test_task_approval_human_session_is_recorded_as_not_automation(client, state):
    project, task = draft_task(client, state, "c")
    human = issue(client, state, "human_session", "human", project["id"])
    response = client.post(
        f"/tasks/{task['id']}/approve", headers=mut(human, "approve-c"), json=approve_body(task)
    )
    assert response.status_code == 200, response.text
    (event,) = events(state, "task.approved")
    assert event["credential_kind"] == "human_session"
    assert event["automation"] is False
    assert "automation_label" not in event


def test_header_never_grants_agent_or_worker_anything(client, state):
    project, task = draft_task(client, state, "d")
    agent = issue(client, state, "agent", "agent", project["id"])
    worker = issue(client, state, "worker", "worker", project["id"], audience="worker")
    for credential in (agent, worker):
        response = client.post(
            f"/tasks/{task['id']}/approve",
            headers=mut(credential, "approve-d", **LABEL),
            json=approve_body(task),
        )
        assert response.status_code in (401, 403)
    assert events(state, "task.approved") == []
    # A label on a human_session only labels the decision; it never changes authority.
    human = issue(client, state, "human_session", "human", project["id"])
    response = client.post(
        f"/tasks/{task['id']}/approve",
        headers=mut(human, "approve-d2", **LABEL),
        json=approve_body(task),
    )
    assert response.status_code == 200
    assert events(state, "task.approved")[0]["automation"] is True


@pytest.mark.parametrize("value", ["", " ", " padded", "padded ", "x" * 129, "bad\x7flabel"])
def test_automation_label_validation(client, state, value):
    response = client.get("/me", headers=owner(state) | {"X-Tessera-Automation": value})
    assert response.status_code in (400, 422)


def test_automation_label_boundary_is_accepted(client, state):
    response = client.get("/me", headers=owner(state) | {"X-Tessera-Automation": "x" * 128})
    assert response.status_code == 200


def _expiry():
    return datetime.now(UTC) + timedelta(hours=1)


DECISION_ENDPOINTS = {
    "grant": (
        "/projects/p/execution-grants",
        {
            "task_id": "t",
            "expected_task_revision": 1,
            "expected_plan_digest": DIGEST,
            "worker_principal_id": "w",
            "snapshot_id": "s",
            "pinned_image": "registry.example/img@sha256:" + DIGEST,
            "expires_at": FUTURE,
            "max_exposure_cents": 0,
            "currency": "USD",
            "acknowledges_unmeasured_infrastructure": True,
        },
    ),
    "admission": (
        "/experiment-variants/v/admitted-attempts",
        {"expected_variant_revision": 1, "expected_design_revision": 1, "grant_id": "g"},
    ),
    "budget_authorization": (
        "/experiments/e/budget/authorization",
        {
            "expected_revision": 0,
            "ceiling_cents": 5000,
            "expires_at": FUTURE,
            "evidence_digest": DIGEST,
            "source_reference": "synthetic",
            "prior_settled_cents": 0,
            "prior_pending_cents": 0,
            "ancillary_cents": 500,
            "shutdown_cents": 1000,
        },
    ),
    "budget_charge": (
        "/experiments/e/budget/charges",
        {
            "expected_revision": 1,
            "delivery_id": "d",
            "amount_cents": 1,
            "evidence_digest": DIGEST,
            "source_reference": "synthetic",
            "category": "compute",
            "admitted_attempt_id": "a",
        },
    ),
    "budget_bounds": (
        "/experiments/e/budget/bounds",
        {
            "expected_revision": 1,
            "evidence_digest": DIGEST,
            "source_reference": "synthetic-resolution",
            "ancillary_cents": 1,
        },
    ),
    "route_approve": (
        "/remote-executions/r/commands",
        {"expected_revision": 1, "command": "approve", "proposal_digest": DIGEST},
    ),
    "route_start": (
        "/remote-executions/r/commands",
        {"expected_revision": 1, "command": "start", "proposal_digest": DIGEST},
    ),
    "capability": (
        "/capabilities/c/approve",
        {"expected_revision": 1, "expires_at": FUTURE, "declaration_digest": DIGEST},
    ),
    "conclusion_review": ("/conclusions/c/review", {"expected_revision": 1}),
    "review_decision": (
        "/interaction/decisions/resolve",
        {"decision_id": "d", "expected_revision": 1, "decision": "approve"},
    ),
    "ingestion_review": (
        "/dataset-ingestion-proposals/i/review",
        {"expected_revision": 1, "decision": "approve", "rationale": "ok"},
    ),
    "agent_authority_grant": (
        "/projects/p/agent-authority-grants",
        {
            "bounds": {
                "subject_credential_id": "c",
                "destination": "d",
                "model": "m",
                "purpose": "p",
                "actions": ["attempt:admit"],
                "bindings": [
                    {
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
                ],
                "budget_cents": 0,
                "max_duration_seconds": 60,
                "expires_at": FUTURE,
            }
        },
    ),
    "autonomy_approve": (
        "/autonomy-sessions/a/commands",
        {"expected_revision": 1, "command": "approve", "plan_digest": DIGEST},
    ),
    "autonomy_resume": (
        "/autonomy-sessions/a/commands",
        {"expected_revision": 1, "command": "resume"},
    ),
    "credential_lease_create": (
        "/execution-grants/g/credential-access-leases",
        {
            "execution_lease_id": "l",
            "expected_epoch": 1,
            "secret_ref": "ref",
            "audience": "aud",
            "purpose": "pur",
            "scope": ["read"],
            "expires_at": FUTURE,
        },
    ),
    "credential_lease_renew": (
        "/credential-access-leases/l/renew",
        {"expected_revision": 1, "expires_at": FUTURE},
    ),
    "snapshot_release": (
        "/snapshots/s/releases",
        {
            "expected_digest": DIGEST,
            "worker_principal_id": "w",
            "classification": "public",
            "permits_worker_transfer": True,
            "rationale": "ok",
            "expires_at": FUTURE,
        },
    ),
}

# Reduce-only actions: they stop, cancel, pause, reject or revoke, so they are not
# gated. They still record provenance.
REDUCE_ONLY_ENDPOINTS = {
    "route_pause": (
        "/remote-executions/r/commands",
        {"expected_revision": 1, "command": "pause", "proposal_digest": DIGEST},
    ),
    "route_cancel": (
        "/remote-executions/r/commands",
        {"expected_revision": 1, "command": "cancel", "proposal_digest": DIGEST},
    ),
    "review_reject": (
        "/interaction/decisions/resolve",
        {"decision_id": "d", "expected_revision": 1, "decision": "reject"},
    ),
    "ingestion_reject": (
        "/dataset-ingestion-proposals/i/review",
        {"expected_revision": 1, "decision": "reject", "rationale": "no"},
    ),
    "autonomy_reject": (
        "/autonomy-sessions/a/commands",
        {"expected_revision": 1, "command": "reject"},
    ),
    "autonomy_cancel": (
        "/autonomy-sessions/a/commands",
        {"expected_revision": 1, "command": "cancel"},
    ),
    "credential_lease_revoke": (
        "/credential-access-leases/l/revoke",
        {"expected_revision": 1},
    ),
    "snapshot_release_revoke": ("/snapshot-releases/r/revoke", {"expected_revision": 1}),
    "agent_authority_revoke": (
        "/agent-authority-grants/g/revoke",
        {"expected_revision": 1, "expected_epoch": 1},
    ),
}


@pytest.mark.parametrize("name", sorted(DECISION_ENDPOINTS))
def test_every_decision_family_refuses_bare_owner_but_not_labelled_callers(client, state, name):
    path, body = DECISION_ENDPOINTS[name]
    human = issue(client, state, "human_session", "human")
    bare = client.post(path, headers=mut(owner(state), "bare"), json=body)
    assert bare.status_code == 403, bare.text
    assert "X-Tessera-Automation" in bare.json()["detail"]
    # Past the trust gate the unknown ids are simply not found / conflicting.
    for headers in (mut(owner(state), "lab", **LABEL), mut(human, "human")):
        response = client.post(path, headers=headers, json=body)
        assert response.status_code != 403 or "X-Tessera-Automation" not in response.text, name
        assert response.status_code in (404, 409, 422), (name, response.text)


@pytest.mark.parametrize("name", sorted(REDUCE_ONLY_ENDPOINTS))
def test_reduce_only_actions_pass_the_trust_gate_for_bare_owner(client, state, name):
    """Fake ids: the request must get past the gate and fail only as not found/conflict."""
    path, body = REDUCE_ONLY_ENDPOINTS[name]
    response = client.post(path, headers=mut(owner(state), "reduce"), json=body)
    assert "X-Tessera-Automation" not in response.text, (name, response.text)
    assert response.status_code in (404, 409, 422), (name, response.text)


@contextmanager
def _labelled_setup():
    """Label owner authentication only while building fixtures, never for the request under test."""
    original = Store.authenticate

    def authenticate(self, token):
        actor = original(self, token)
        if actor.get("kind") == "local_owner" and not actor.get("automation_label"):
            actor["automation_label"] = "fixture-setup"
        return actor

    Store.authenticate = authenticate
    try:
        yield
    finally:
        Store.authenticate = original


def _ingestion_proposal(base: Path):
    """A real, pending dataset-ingestion proposal in a fresh state directory."""
    with _labelled_setup():
        store, setup_owner, study_id, source = _ingestion_setup(base)
        snapshot = _ingestion_command(
            store.register_dataset_snapshot,
            setup_owner,
            study_id,
            payload=_snapshot_payload(source),
        )
        proposal = _ingestion_command(
            store.create_ingestion_proposal,
            setup_owner,
            study_id,
            payload={
                "snapshot_id": snapshot["id"],
                "expected_snapshot_revision": snapshot["revision"],
                "intended_use": "Review-only trust-policy fixture.",
            },
        )
    return base / "state", proposal


def test_ingestion_reject_by_bare_owner_succeeds_and_stamps_provenance(tmp_path):
    state_dir, proposal = _ingestion_proposal(tmp_path)
    client = TestClient(create_app(state_dir))
    response = client.post(
        f"/dataset-ingestion-proposals/{proposal['id']}/review",
        headers=mut(owner(state_dir), "reject"),
        json={
            "expected_revision": proposal["revision"],
            "decision": "reject",
            "rationale": "Not wanted.",
        },
    )
    assert response.status_code == 200, response.text
    (event,) = events(state_dir, "research.dataset_ingestion_reviewed")
    assert event["decision_class"] == "owner_unlabelled"
    assert event["credential_kind"] == "local_owner"
    assert event["automation"] is False
    assert "automation_label" not in event


def test_ingestion_approve_by_bare_owner_is_refused_without_changing_state(tmp_path):
    state_dir, proposal = _ingestion_proposal(tmp_path)
    client = TestClient(create_app(state_dir))
    response = client.post(
        f"/dataset-ingestion-proposals/{proposal['id']}/review",
        headers=mut(owner(state_dir), "approve-bare"),
        json={
            "expected_revision": proposal["revision"],
            "decision": "approve",
            "rationale": "ok",
        },
    )
    assert response.status_code == 403, response.text
    assert events(state_dir, "research.dataset_ingestion_reviewed") == []


def test_desktop_owner_gated_action_outside_task_approval_is_unverified(tmp_path):
    """Ingestion approval (not task approval) with the desktop label is never human."""
    state_dir, proposal = _ingestion_proposal(tmp_path)
    client = TestClient(create_app(state_dir))
    response = client.post(
        f"/dataset-ingestion-proposals/{proposal['id']}/review",
        headers=mut(owner(state_dir), "approve-desktop", **{AUTOMATION_HEADER: "desktop-owner"}),
        json={
            "expected_revision": proposal["revision"],
            "decision": "approve",
            "rationale": "ok",
        },
    )
    assert response.status_code == 200, response.text
    (event,) = events(state_dir, "research.dataset_ingestion_reviewed")
    assert event["decision_class"] == "owner_automation_unverified"
    assert event["automation_label"] == "desktop-owner"
    assert event["credential_kind"] == "local_owner"


def test_credential_lease_revoke_by_bare_owner_succeeds_and_stamps_provenance(tmp_path):
    with _labelled_setup():
        state_dir, _, setup_owner, _, grant, lease = _delivery(tmp_path)
        store = CredentialAccessLeaseStore(state_dir)
        payload = _lease_payload(lease)
        _, access = store.create(
            setup_owner, grant["id"], "access", canonical_digest(payload), payload
        )
    client = TestClient(create_app(state_dir))
    response = client.post(
        f"/credential-access-leases/{access['id']}/revoke",
        headers=mut(owner(state_dir), "revoke-lease"),
        json={"expected_revision": access["revision"]},
    )
    assert response.status_code == 200, response.text
    event = events(state_dir, "credential_access.lease_revoked")[-1]
    assert event["decision_class"] == "owner_unlabelled"
    assert event["credential_kind"] == "local_owner"
    assert event["automation"] is False


def test_route_has_no_reject_command_so_route_reduce_only_actions_are_pause_and_cancel(
    client, state
):
    """Policy pin: routes are approve/start (gated) and pause/cancel (ungated).

    There is no route ``reject`` command; the model refuses it at validation, before
    any trust check, so it can neither bypass the gate nor be confused with a gated
    action. ``docs/agent-authority-interface.md`` lists route ``pause``/``cancel`` as
    the ungated route actions, which matches.
    """
    from typing import get_args

    from tessera_control.remote_execution_models import RemoteCommand

    assert set(get_args(RemoteCommand.model_fields["command"].annotation)) == {
        "approve",
        "start",
        "pause",
        "cancel",
    }
    response = client.post(
        "/remote-executions/r/commands",
        headers=mut(owner(state), "route-reject"),
        json={"expected_revision": 1, "command": "reject", "proposal_digest": DIGEST},
    )
    assert response.status_code == 422, response.text
    assert "X-Tessera-Automation" not in response.text


def test_decision_classes():
    from tessera_control.approval_trust import decision_class, decision_provenance

    human = {"kind": "human_session"}
    assert decision_class(human) == "human_session"
    assert decision_class(human | {"automation_label": "x"}) == "human_session"
    assert decision_class({"kind": "local_owner", "automation_label": "desktop-owner"}) == (
        "owner_automation_unverified"
    )
    assert decision_class({"kind": "local_owner", "automation_label": "salad-live-setup"}) == (
        "owner_automation_labelled"
    )
    assert decision_class({"kind": "local_owner"}) == "owner_unlabelled"
    assert decision_class({"kind": "agent"}) == "other_credential"
    assert decision_provenance({"kind": "local_owner", "automation_label": "desktop-owner"}) == {
        "credential_kind": "local_owner",
        "decision_class": "owner_automation_unverified",
        "automation": True,
        "automation_label": "desktop-owner",
    }


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("desktop-owner", "owner_automation_unverified"),
        ("test-script", "owner_automation_labelled"),
    ],
)
def test_owner_task_approval_records_decision_class(client, state, label, expected):
    _, task = draft_task(client, state, "cls")
    response = client.post(
        f"/tasks/{task['id']}/approve",
        headers=mut(owner(state), "approve-cls", **{"X-Tessera-Automation": label}),
        json=approve_body(task),
    )
    assert response.status_code == 200, response.text
    (event,) = events(state, "task.approved")
    assert event["decision_class"] == expected
    assert event["credential_kind"] == "local_owner"
    assert event["automation_label"] == label


def test_human_session_task_approval_records_human_class(client, state):
    project, task = draft_task(client, state, "hcls")
    human = issue(client, state, "human_session", "human", project["id"])
    client.post(
        f"/tasks/{task['id']}/approve", headers=mut(human, "approve-hcls"), json=approve_body(task)
    )
    (event,) = events(state, "task.approved")
    assert event["decision_class"] == "human_session"


def test_salad_setup_script_labels_owner_decisions():
    text = (Path(__file__).parents[1] / "scripts/salad_live_setup.py").read_text()
    assert 'AUTOMATION_LABEL = "salad-live-setup"' in text
    assert 'headers["X-Tessera-Automation"] = AUTOMATION_LABEL' in text
    assert 'self.owner["automation_label"] = AUTOMATION_LABEL' in text
