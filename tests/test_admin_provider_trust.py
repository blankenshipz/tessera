"""Provider trust admin routes (ADR 0040 section 4): reads, human-only reload, no write."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from campaign_fixture import Env
from fastapi.testclient import TestClient
from provider_trust_fixture import ALL_CLASSES, DERIVED

from tessera_control.api import create_app
from tessera_control.provider_trust import (
    current_policy,
    install_policy,
    load_policy_file,
    loaded_at,
)
from tessera_control.store import canonical_digest

DESTINATION = "planner.local"


def _file(path, providers):
    path.write_text(json.dumps({"schema": "tessera.provider-trust.v1", "providers": providers}))
    path.chmod(0o600)
    return path


def _entry(classes, principals):
    return {"data_classes": list(classes), "worker_principal_ids": list(principals)}


class World:
    def __init__(self, tmp_path):
        self.env = Env(tmp_path)
        self.path = _file(tmp_path / "trust.json", {"salad": _entry(DERIVED, ["w-1"])})
        install_policy(load_policy_file(self.path))
        self.app = create_app(self.env.state)
        self.app.state.provider_trust_path = self.path
        self.client = TestClient(self.app)
        self.owner = {
            "Authorization": (self.env.state / "owner.header")
            .read_text()
            .strip()
            .split(": ", 1)[1],
            "X-Tessera-Automation": "test-automation",
        }

    def credential(self, name, **body):
        body = {
            "audience": "control",
            "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            **body,
        }
        token = f"synthetic-{name}-token-" + "x" * 30
        self.env.plain.issue_credential(
            self.env.owner, f"cred-{name}", canonical_digest(body), body, token
        )
        return {"Authorization": f"Bearer {token}", "X-Agent-Destination": DESTINATION}

    def human(self):
        return self.credential("human", kind="human_session")

    def agent(self, actions):
        return self.credential(
            "agent-" + "-".join(a.replace(":", "_") for a in actions),
            kind="agent",
            project_id=self.env.project["id"],
            external_agent={"actions": actions, "destination": DESTINATION},
        )

    def reload(self, headers, key="k1"):
        return self.client.post(
            "/admin/provider-trust/reload", headers={**headers, "Idempotency-Key": key}
        )

    def events(self, event_type):
        with sqlite3.connect(self.env.state / "tessera.sqlite3") as db:
            return db.execute(
                "SELECT decision, payload_json FROM operator_audit_events WHERE event_type=?",
                (event_type,),
            ).fetchall()


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_a_human_reads_the_setting_with_its_digest_and_load_time(world):
    response = world.client.get("/admin/provider-trust", headers=world.human())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["digest"] == current_policy().digest and body["source"] == "file"
    assert body["policy"]["providers"]["salad"]["worker_principal_ids"] == ["w-1"]
    assert body["loaded_at"] == loaded_at() and body["reload_configured"] is True


def test_an_agent_reads_only_with_the_provider_trust_scope(world):
    allowed = world.agent(["provider_trust:read"])
    assert world.client.get("/admin/provider-trust", headers=allowed).status_code == 200
    other = world.agent(["campaign:read"])
    assert world.client.get("/admin/provider-trust", headers=other).status_code == 403
    assert world.client.get("/admin/provider-trust").status_code == 401
    wrong_destination = {**allowed, "X-Agent-Destination": "elsewhere"}
    assert world.client.get("/admin/provider-trust", headers=wrong_destination).status_code == 403


def test_reload_is_human_only(world):
    agent = world.agent(["provider_trust:read"])
    assert world.reload(agent).status_code == 403
    assert world.reload(world.human()).status_code == 200


def test_reload_installs_a_changed_file_and_audits_one_event(world):
    human = world.human()
    before = current_policy().digest
    _file(
        world.path,
        {"salad": _entry(ALL_CLASSES, ["w-1"]), "self-hosted": _entry(DERIVED, ["w-2"])},
    )
    response = world.reload(human, "k-change")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["changed"] is True and body["status"] == "loaded"
    assert current_policy().digest == body["digest"] != before
    (changed,) = world.events("provider_trust.policy_changed")
    payload = json.loads(changed[1])
    assert (
        payload["old_digest"] == current_policy().digest or payload["new_digest"] == body["digest"]
    )
    assert payload["actor"] and payload["providers_changed"]["self-hosted"]["provider"] == "added"
    # Replaying the same key returns the stored response; a new key with an identical file
    # appends nothing.
    assert world.reload(human, "k-change").json() == body
    again = world.reload(human, "k-again")
    assert again.status_code == 200 and again.json()["changed"] is False
    assert len(world.events("provider_trust.policy_changed")) == 1


def test_a_refused_reload_keeps_the_last_good_setting_and_is_audited(world):
    human = world.human()
    kept = current_policy().digest
    world.path.chmod(0o666)
    response = world.reload(human)
    assert response.status_code == 409
    assert response.json()["status"] == "refused" and response.json()["kept_digest"] == kept
    assert current_policy().digest == kept
    ((decision, payload),) = world.events("provider_trust.reload_refused")
    assert decision == "denied" and json.loads(payload)["kept_digest"] == kept
    assert world.events("provider_trust.policy_changed") == []


def test_reload_without_a_configured_file_is_a_conflict(world):
    world.app.state.provider_trust_path = None
    assert world.reload(world.human()).status_code == 409


def test_there_is_no_write_route(world):
    headers = {**world.human(), "Idempotency-Key": "w"}
    for method in ("put", "patch", "delete"):
        assert (
            getattr(world.client, method)("/admin/provider-trust", headers=headers).status_code
            == 405
        )
    assert world.client.post("/admin/provider-trust", headers=headers, json={}).status_code == 405
    assert world.client.put("/admin/provider-trust/reload", headers=headers).status_code == 405
    assert (
        world.client.post("/admin/provider-trust/policy", headers=headers, json={}).status_code
        == 404
    )
