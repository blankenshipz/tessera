"""Shared fakes for the research-loop steering tests: a review policy and a faked model call.

The model call is faked at the transport: the real reasoning supervisor, store and direct
Messages API adapter run over a fake transport, so the model-call receipt is the real
metadata-only record (ADR 0083) and nothing touches a network or a provider. The credential is
an API-key reference (ADR 0082); the synthetic key sits in a private temporary file.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tessera_agent.anthropic_messages import COUNT_URL, AnthropicMessagesReasoning
from tessera_agent.remote_reasoning_runtime import ReasoningSupervisor
from tessera_agent.remote_reasoning_store import ReasoningStore

PROMPT = "synthetic-review-prompt-text-never-journaled"
CREDENTIAL = "anthropic-api-key://EXAMPLE-OWNER/EXAMPLE-REFERENCE"
API_KEY = "sk-ant-SYNTHETIC-KEY-never-journaled"
COMPLETION = "synthetic-completion-text-never-journaled"
DAY = datetime(2026, 11, 22, 10, tzinfo=UTC).timestamp()
SOURCE_DIGEST = hashlib.sha256(b"source").hexdigest()
JOB_DESTINATION = "dest-1"  # the one model destination the review policy enrolls
MODEL_ID = "claude-sonnet-4-5"
MODEL = "anthropic/" + MODEL_ID


class ReviewPolicy:
    """One enrolled destination; budget at most 500 cents; opus exceeds policy; private
    classes are not admitted on the cheap tier or as approved private code."""

    def check(self, destination_id, tier, data_classes, budget_cents):
        if destination_id != JOB_DESTINATION:
            return "destination_not_enrolled"
        if "private_code_approved" in data_classes or (
            "private_derived" in data_classes and tier == "haiku"
        ):
            return "data_class_not_allowed_for_destination"
        if budget_cents > 500:
            return "budget_exceeds_policy"
        if tier == "opus":
            return "tier_exceeds_policy"
        return None


class _Transport:
    """Fake allowlisted transport for the direct Messages API runtime: no network is used."""

    def post_json(self, url, headers, body, *, timeout=None):
        if url == COUNT_URL:
            return 200, {}, {"input_tokens": 100}
        return (
            200,
            {},
            {
                "model": MODEL_ID,
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
                "content": [{"type": "text", "text": COMPLETION}],
            },
        )


def _profile() -> dict:
    return {
        "run_id": "run1",
        "grant_id": "g1",
        "revision": 1,
        "epoch": 1,
        "subject_credential_id": "agent1",
        "destination": "lab",
        "account_mode": "anthropic-api-key-ref",
        "credential_ref": CREDENTIAL,
        "model": {"providerID": "anthropic", "id": MODEL_ID},
        "max_steps": 3,
        "max_tokens": 5000,
        "max_duration_seconds": 3600,
        "permissions": [{"action": "*", "resource": "*", "effect": "deny"}],
        "limits": {
            "max_output_tokens_per_call": 1000,
            "max_input_tokens_per_call": 500,
            "max_cost_cents_per_call": 10,
            "daily_token_cap": 1200,
            "daily_cost_cap_cents": 100,
        },
        "pricing_cents_per_mtok": {
            "input": 300,
            "output": 1500,
            "cache_read": 30,
            "cache_write": 375,
        },
    }


def fake_model_call(directory: Path) -> dict:
    """Run one faked model call in ``directory`` and return its stored receipt.

    The real supervisor, store and Messages API adapter run over a fake transport, with a
    synthetic API key in a private temporary file read by credential reference. The receipt is
    the real metadata-only record (ADR 0083): the key, the prompt and the completion are not in it.
    """
    directory.mkdir(parents=True, exist_ok=True)
    credentials = directory / "credentials"
    credentials.mkdir(mode=0o700)
    key_file = credentials / "anthropic-api-key"
    key_file.write_text(API_KEY + "\n")
    key_file.chmod(0o600)
    profile = _profile()
    clock = lambda: DAY  # noqa: E731
    store = ReasoningStore(directory / "state")
    store.bind(
        "run1",
        {
            "grant_id": "g1",
            "revision": 1,
            "epoch": 1,
            "subject_credential_id": "agent1",
            "destination": "lab",
            "model": MODEL_ID,
            "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
            "profile_sha256": hashlib.sha256(b"profile").hexdigest(),
        },
        clock(),
    )
    grant = {
        "revision": 1,
        "epoch": 1,
        "status": "active",
        "effective_status": "active",
        "remaining_budget_cents": 50,
        "bounds": {
            "subject_credential_id": "agent1",
            "destination": "lab",
            "model": MODEL_ID,
            "budget_cents": 100,
            "max_duration_seconds": 7200,
            "expires_at": "2100-01-01T00:00:00+00:00",
        },
    }
    adapter = AnthropicMessagesReasoning(
        profile, credentials_dir=credentials, transport=_Transport(), clock=clock
    )
    supervisor = ReasoningSupervisor(
        store, adapter, lambda _: grant, profile, clock=clock, holdout_lookup=_unsealed
    )
    assert supervisor.tick("run1", PROMPT)["state"] == "completed"
    (receipt,) = store.model_calls("run1")
    return receipt


def _unsealed(project, digest):
    return {"project_id": project, "claims": [], "truncated": False}


def seed_private_snapshot(database: Path, project_id: str, snapshot_id: str, digest: str) -> None:
    """A synthetic snapshot row with no recorded data class: control derives `private_derived`
    for it (ADR 0039), whatever a client says about it."""
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO source_snapshots (id,project_id,digest,manifest_json,created_at,"
            "actor_principal_id) VALUES (?,?,?,'{}','t',(SELECT id FROM principals LIMIT 1))",
            (snapshot_id, project_id, digest),
        )


def enrol_person(client, owner_headers: dict, token: str, label: str = "decider") -> None:
    """Enrol a second person through the real owner-only endpoint (POST /people): a human
    session that is its own enrolment root (it can resolve findings the owner's agents posted)."""
    response = client.post(
        "/people",
        headers={**owner_headers, "Idempotency-Key": "enrol-" + label},
        json={
            "token": token,
            "label": label,
            "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        },
    )
    assert response.status_code == 201, response.text


def second_root_agent(env, token: str, actions: list[str], destination: str) -> None:
    """An agent credential issued by a SECOND enrolment root, so control treats it as another
    person than the owner's agents (the reviewer may not be the submitter's person)."""
    from tessera_control.store import canonical_digest

    ident = hashlib.sha256(token.encode()).hexdigest()[:16]
    with sqlite3.connect(env.state / "tessera.sqlite3") as connection:
        org = connection.execute("SELECT org_id FROM principals LIMIT 1").fetchone()[0]
        connection.execute(
            "INSERT INTO principals (id,org_id,role,token_hash,revoked_at,created_at) "
            "VALUES (?,?,'human',?,NULL,'2026-01-01T00:00:00Z')",
            ("principal_" + ident, org, hashlib.sha256((token + "-root").encode()).hexdigest()),
        )
        connection.execute(
            "INSERT INTO credentials (id,principal_id,kind,audience,project_id,parent_id,"
            "expires_at,revoked_at,created_at,created_by_principal_id) "
            "VALUES (?,?,'local_owner','control',NULL,NULL,NULL,NULL,"
            "'2026-01-01T00:00:00Z',NULL)",
            ("credential_" + ident, "principal_" + ident),
        )
    root = env.plain.authenticate(token + "-root")
    body = {
        "kind": "agent",
        "audience": "control",
        "project_id": env.project["id"],
        "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        "external_agent": {"actions": actions, "destination": destination},
    }
    env.plain.issue_credential(root, "cred-" + ident, canonical_digest(body), body, token)
