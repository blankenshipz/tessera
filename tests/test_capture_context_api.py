"""Actual temporary authenticated context resolution; synthetic curated research text."""

import hashlib
import json
import sqlite3
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from test_agent_authority import enroll, post
from test_generated_output_provenance import registered as registered
from test_separated_capture_contexts import separated as separated

from tessera_control.capture_context_models import CaptureContextsReply


@pytest.fixture
def contexts(separated):
    setup = separated
    token = "synthetic-human-policy-session-token-for-unit-tests-only"
    issuer = post(
        setup["client"],
        setup["owner"],
        "/credentials",
        {
            "token": token,
            "kind": "human_session",
            "audience": "control",
            "project_id": None,
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    human = {"Authorization": "Bearer " + token}

    def create(body):
        return post(
            setup["client"],
            human,
            f"/projects/{setup['project']}/agent-contexts",
            {
                "credential_id": setup["binding"]["credential_id"],
                "destination": setup["author"]["X-Agent-Destination"],
                "title": "curated input or separate capture policy",
                "body": body,
                "classification": "project",
                "disclosure_approved": True,
            },
        )

    binding = deepcopy(setup["binding"])
    input_context = create(setup["input_body"])
    binding.update(context_id=input_context["id"], input_context_id=input_context["id"])
    policy = deepcopy(setup["policy"])
    policy["input_context_id"] = input_context["id"]
    capture = create(json.dumps(policy))
    binding["capture_policy_context_id"] = capture["id"]
    return setup | {
        "binding": binding,
        "http_issuer": issuer,
        "http_input": input_context,
        "http_policy": capture,
        "http_human": human,
    }


def resolve(setup, binding=None, auth=None, project=None):
    return setup["client"].post(
        f"/projects/{project or setup['project']}/agent-capture-contexts/resolve",
        headers=auth or setup["author"],
        json=binding or setup["binding"],
    )


def test_actual_agent_resolves_bodies_and_current_separate_human_issuer(contexts):
    response = resolve(contexts)
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    result = CaptureContextsReply.model_validate(response.json())
    assert result.authority_effect == "none"
    assert result.resolution.input_context.body == contexts["input_body"]
    assert result.resolution.capture_policy.body == contexts["http_policy"]["body"]
    assert (
        result.resolution.input_context.issuer_principal_id
        == contexts["http_issuer"]["principal_id"]
    )
    assert result.resolution.capture_policy.issuer_role == "human"
    assert result.resolution.input_context.context_id != result.resolution.capture_policy.context_id
    assert (
        hashlib.sha256(result.resolution.input_context.body.encode()).hexdigest()
        == result.verified_binding.input_body_digest
    )
    encoded = json.dumps(response.json())
    assert "token_hash" not in encoded and "Authorization" not in encoded
    with sqlite3.connect(contexts["state"] / "tessera.sqlite3") as db:
        events = db.execute("SELECT count(*) FROM events").fetchone()[0]
    assert resolve(contexts).json() == response.json()
    with sqlite3.connect(contexts["state"] / "tessera.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == events


@pytest.mark.parametrize(
    "change",
    [
        "missing_separation",
        "conflated",
        "digest",
        "policy_digest",
        "issuer_claim",
        "actor_pin",
        "credential_pin",
        "tenant_pin",
        "destination_pin",
        "session",
        "skill",
        "config",
        "wrong_destination",
        "other_actor",
        "human",
        "project_route",
    ],
)
def test_closed_binding_identity_input_policy_mixups_denied(contexts, change):
    binding = deepcopy(contexts["binding"])
    auth = contexts["author"]
    project = None
    expected = 403
    if change == "missing_separation":
        binding.pop("input_body_digest")
        expected = 422
    if change == "conflated":
        binding["capture_policy_context_id"] = binding["input_context_id"]
        expected = 422
    if change == "digest":
        binding.update(context_digest="f" * 64, input_body_digest="f" * 64)
        expected = 409
    if change == "policy_digest":
        binding["capture_policy_context_id"] = contexts["binding"]["input_context_id"]
        expected = 422
    if change == "issuer_claim":
        binding["issuer_is_human"] = True
        expected = 422
    if change == "actor_pin":
        binding["actor_principal_id"] = "other"
    if change == "credential_pin":
        binding["credential_id"] = "other"
    if change == "tenant_pin":
        binding["org_id"] = "other"
    if change == "destination_pin":
        binding["destination_digest"] = "sha256:" + "f" * 64
    if change == "session":
        binding["session_id"] = "other"
        expected = 409
    if change == "skill":
        binding["skill_digest"] = "f" * 64
        expected = 409
    if change == "config":
        binding["config_digest"] = "f" * 64
        expected = 409
    if change == "wrong_destination":
        auth = auth | {"X-Agent-Destination": "other"}
    if change == "other_actor":
        auth = contexts["receiver"]
    if change == "human":
        auth = contexts["owner"]
    if change == "project_route":
        project = "other"
    response = resolve(contexts, binding, auth, project)
    assert response.status_code == expected, response.text
    assert "resolution" not in response.json()


@pytest.mark.parametrize(
    "change",
    [
        "issuer_revoked",
        "issuer_role",
        "issuer_expired",
        "agent_revoked",
        "policy_scope",
        "input_body",
        "policy_body",
        "action",
        "context_action",
    ],
)
def test_current_database_role_liveness_disclosure_and_source_pins(contexts, change):
    binding = deepcopy(contexts["binding"])
    auth = contexts["author"]
    if change in ("policy_scope", "input_body", "policy_body"):
        # Issue a different immutable context rather than bypassing record guards.
        source = contexts["http_input"] if change == "input_body" else contexts["http_policy"]
        credential = binding["credential_id"]
        body = source["body"]
        if change == "policy_scope":
            _, other = enroll(
                contexts["client"],
                contexts["owner"],
                contexts["project"],
                ["context:read", "provenance:append"],
            )
            credential = other["credential_id"]
        if change == "input_body":
            body = "changed curated body"
        if change == "policy_body":
            policy = json.loads(body)
            policy["model_id"] = "other"
            body = json.dumps(policy)
        created = post(
            contexts["client"],
            contexts["http_human"],
            f"/projects/{contexts['project']}/agent-contexts",
            {
                "credential_id": credential,
                "destination": auth["X-Agent-Destination"],
                "title": "alternate immutable input or policy",
                "body": body,
                "classification": "project",
                "disclosure_approved": True,
            },
        )
        if change == "input_body":
            binding.update(context_id=created["id"], input_context_id=created["id"])
        else:
            binding["capture_policy_context_id"] = created["id"]
    if change == "action":
        auth, _ = enroll(
            contexts["client"], contexts["owner"], contexts["project"], ["context:read"]
        )
    if change == "context_action":
        auth, _ = enroll(
            contexts["client"], contexts["owner"], contexts["project"], ["provenance:append"]
        )
    with sqlite3.connect(contexts["state"] / "tessera.sqlite3") as db:
        if change == "issuer_revoked":
            db.execute(
                "UPDATE principals SET revoked_at='now' WHERE id=?",
                (contexts["http_issuer"]["principal_id"],),
            )
        if change == "issuer_role":
            db.execute(
                "UPDATE principals SET role='agent' WHERE id=?",
                (contexts["http_issuer"]["principal_id"],),
            )
        if change == "issuer_expired":
            db.execute(
                "UPDATE credentials SET expires_at='2000-01-01T00:00:00Z' WHERE id=?",
                (contexts["http_issuer"]["credential_id"],),
            )
        if change == "agent_revoked":
            db.execute(
                "UPDATE credentials SET revoked_at='now' WHERE id=?",
                (contexts["binding"]["credential_id"],),
            )
    response = resolve(contexts, binding, auth)
    assert response.status_code == (
        401
        if change == "agent_revoked"
        else 409
        if change in ("input_body", "policy_body")
        else 403
    ), response.text
    assert "resolution" not in response.json()


def test_unapproved_disclosure_cannot_be_issued_as_a_resolvable_context(contexts):
    response = contexts["client"].post(
        f"/projects/{contexts['project']}/agent-contexts",
        headers=contexts["http_human"] | {"Idempotency-Key": "unapproved-input"},
        json={
            "credential_id": contexts["binding"]["credential_id"],
            "destination": contexts["author"]["X-Agent-Destination"],
            "title": "not approved for disclosure",
            "body": "must not reach enrolled agent",
            "classification": "project",
            "disclosure_approved": False,
        },
    )
    assert response.status_code == 422
    assert "id" not in response.json()


def test_read_snapshot_never_mixes_contexts_and_next_read_observes_revocation(
    contexts, monkeypatch
):
    from tessera_control import capture_context_api

    with sqlite3.connect(contexts["state"] / "tessera.sqlite3") as db:
        db.execute("PRAGMA journal_mode=WAL")
    original = capture_context_api.resolve_separated_capture_contexts

    def concurrent_revoke(connection, actor, binding):
        result = original(connection, actor, binding)
        # Writer commits after this read's consistent validation snapshot.
        with sqlite3.connect(contexts["state"] / "tessera.sqlite3") as db:
            db.execute(
                "UPDATE principals SET revoked_at='now' WHERE id=?",
                (contexts["http_issuer"]["principal_id"],),
            )
        return result

    monkeypatch.setattr(
        capture_context_api, "resolve_separated_capture_contexts", concurrent_revoke
    )
    first = resolve(contexts)
    assert first.status_code == 200
    assert first.json()["resolution"]["input_context"]["issuer_role"] == "human"
    monkeypatch.setattr(capture_context_api, "resolve_separated_capture_contexts", original)
    assert resolve(contexts).status_code == 403
