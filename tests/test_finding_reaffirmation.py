from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tessera_control.api import create_app
from tessera_control.research import ResearchStore
from tessera_control.store import Conflict, Forbidden, NotFound, canonical_digest, initialize_state

_KEYS = count()


def _setup(tmp_path: Path) -> tuple[ResearchStore, dict[str, object]]:
    state = tmp_path / "state"
    initialize_state(state)
    store = ResearchStore(state)
    token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    return store, store.authenticate(token)


def _run(store: ResearchStore, actor: dict[str, object], method, *args, body):
    status, result = method(
        actor,
        *args,
        "test-key-" + method.__name__ + str(next(_KEYS)),
        canonical_digest(body),
        body,
    )
    assert status in {200, 201}
    return result


def _fixture(tmp_path: Path, *, mode: str = "exploratory"):
    store, owner = _setup(tmp_path)
    project_body = {"name": "Reaffirmation"}
    _, project = store.create_project(
        owner, "project-reaffirm", canonical_digest(project_body), project_body
    )
    study_body = {
        "purpose": "Synthetic review",
        "protocol": "Retain evidence",
        "mode": mode,
    }
    study = _run(store, owner, store.create_study, project["id"], body=study_body)
    variant = _run(
        store,
        owner,
        store.register_variant,
        study["id"],
        body={
            "name": "variant",
            "method_digest": "a" * 64,
            "input_digest": "b" * 64,
            "metadata": {"synthetic": True},
        },
    )
    source = _run(
        store,
        owner,
        store.create_source,
        study["id"],
        body={
            "uri": "urn:test:source",
            "quoted_passage": "Supported passage",
            "content_digest": "c" * 64,
        },
    )
    source = _run(
        store,
        owner,
        store.assess_source,
        source["id"],
        body={"expected_revision": 1, "verdict": "supported", "reason": "Initial review"},
    )
    finding = _run(
        store,
        owner,
        store.create_finding,
        study["id"],
        body={
            "statement": "Stable statement",
            "source_ids": [source["id"]],
            "variant_ids": [variant["id"]],
        },
    )
    accepted = _run(
        store, owner, store.accept_finding, finding["id"], body={"expected_revision": 1}
    )
    _run(
        store,
        owner,
        store.assess_source,
        source["id"],
        body={"expected_revision": 2, "verdict": "unsupported", "reason": "Recheck"},
    )
    supported = _run(
        store,
        owner,
        store.assess_source,
        source["id"],
        body={"expected_revision": 3, "verdict": "supported", "reason": "Resolved"},
    )
    return store, owner, accepted, source, supported


def test_reaffirmation_preserves_history_and_attribution(tmp_path: Path) -> None:
    store, owner, accepted, source, supported = _fixture(tmp_path)
    before = store.export_study(owner, accepted["study_id"])
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    with sqlite3.connect(store.database_path) as connection:
        project_id = connection.execute(
            "SELECT project_id FROM research_studies WHERE id=?", (accepted["study_id"],)
        ).fetchone()[0]
    human_body = {
        "kind": "human_session",
        "audience": "control",
        "project_id": project_id,
        "expires_at": expires,
    }
    store.issue_credential(
        owner, "second-human-key", canonical_digest(human_body), human_body, "second-human-token"
    )
    second_human = store.authenticate("second-human-token")
    reaffirmed = _run(
        store,
        second_human,
        store.reaffirm_finding,
        accepted["id"],
        body={"expected_revision": 3, "reason": "Evidence was restored and reviewed."},
    )
    assert reaffirmed["status"] == "accepted"
    assert reaffirmed["revision"] == 4
    assert reaffirmed["statement"] == accepted["statement"]
    assert reaffirmed["source_ids"] == accepted["source_ids"]
    assert reaffirmed["variant_ids"] == accepted["variant_ids"]
    assert reaffirmed["accepted_by_principal_id"] == owner["id"]
    exported = store.export_study(owner, accepted["study_id"])
    event = exported["events"][-1]
    assert event["event_type"] == "research.finding_reaffirmed"
    assert event["actor_principal_id"] == second_human["id"]
    assert event["payload"] == {
        "study_id": accepted["study_id"],
        "finding_id": accepted["id"],
        "prior_revision": 3,
        "reason": "Evidence was restored and reviewed.",
        "prior_status": "needs_review",
    }
    assert len(exported["findings"]) == len(before["findings"]) == 1
    assert source["id"] == supported["id"]


def test_reaffirmation_rejects_stale_revision_and_replays_same_key(tmp_path: Path) -> None:
    store, owner, accepted, _, _ = _fixture(tmp_path)
    body = {"expected_revision": 3, "reason": "Reviewed again."}
    first = store.reaffirm_finding(
        owner, accepted["id"], "reaffirm-key", canonical_digest(body), body
    )
    assert first == store.reaffirm_finding(
        owner, accepted["id"], "reaffirm-key", canonical_digest(body), body
    )
    with pytest.raises(Conflict):
        store.reaffirm_finding(
            owner,
            accepted["id"],
            "reaffirm-key",
            canonical_digest({**body, "reason": "Different"}),
            {**body, "reason": "Different"},
        )
    with pytest.raises(Conflict):
        store.reaffirm_finding(
            owner,
            accepted["id"],
            "stale-key",
            canonical_digest(body),
            body,
        )


def test_reaffirmation_event_failure_rolls_back(tmp_path: Path, monkeypatch) -> None:
    store, owner, accepted, _, _ = _fixture(tmp_path)
    monkeypatch.setattr(
        store, "_event_for", lambda *args: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    body = {"expected_revision": 3, "reason": "Should roll back."}
    with pytest.raises(RuntimeError, match="boom"):
        store.reaffirm_finding(owner, accepted["id"], "rollback-key", canonical_digest(body), body)
    finding = next(
        item
        for item in store.export_study(owner, accepted["study_id"])["findings"]
        if item["id"] == accepted["id"]
    )
    assert finding["status"] == "needs_review"
    assert finding["revision"] == 3


def test_reaffirmation_api_validates_reason_and_returns_record(tmp_path: Path) -> None:
    store, owner, accepted, _, _ = _fixture(tmp_path)
    state = store.database_path.parent
    token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    with TestClient(create_app(state)) as client:
        headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "api-reaffirm"}
        invalid = client.post(
            f"/findings/{accepted['id']}/reaffirm",
            json={"expected_revision": 3, "reason": "   "},
            headers=headers,
        )
        assert invalid.status_code == 422
        response = client.post(
            f"/findings/{accepted['id']}/reaffirm",
            json={"expected_revision": 3, "reason": "API review"},
            headers=headers,
        )
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"


def test_reaffirmation_rejects_agent_and_cross_project_scope(tmp_path: Path) -> None:
    store, owner, accepted, _, _ = _fixture(tmp_path)
    other_body = {"name": "Other project"}
    _, other_project = store.create_project(
        owner, "other-project", canonical_digest(other_body), other_body
    )
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    body = {
        "kind": "agent",
        "audience": "control",
        "project_id": other_project["id"],
        "expires_at": expires,
    }
    _, _ = store.issue_credential(owner, "agent-key", canonical_digest(body), body, "agent-token")
    agent = store.authenticate("agent-token")
    command = {"expected_revision": 3, "reason": "Agent review"}
    with pytest.raises(Forbidden, match="Human review"):
        store.reaffirm_finding(
            agent, accepted["id"], "agent-reaffirm", canonical_digest(command), command
        )
    project_body = {"name": "Scoped project"}
    _, scoped_project = store.create_project(
        owner, "scoped-project", canonical_digest(project_body), project_body
    )
    scoped_body = {
        **body,
        "kind": "human_session",
        "project_id": scoped_project["id"],
    }
    store.issue_credential(
        owner, "scoped-agent-key", canonical_digest(scoped_body), scoped_body, "scoped-token"
    )
    scoped = store.authenticate("scoped-token")
    with pytest.raises(NotFound):
        store.reaffirm_finding(
            scoped, accepted["id"], "scoped-reaffirm", canonical_digest(command), command
        )


def test_project_scoped_human_can_access_only_its_project_records(tmp_path: Path) -> None:
    store, owner, accepted, source, _ = _fixture(tmp_path)
    finding = store.export_study(owner, accepted["study_id"])["findings"][0]
    project_id = store.export_study(owner, accepted["study_id"])["study"]["project_id"]
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    body = {
        "kind": "human_session",
        "audience": "control",
        "project_id": project_id,
        "expires_at": expires,
    }
    store.issue_credential(
        owner, "same-project-human", canonical_digest(body), body, "same-project-token"
    )
    scoped = store.authenticate("same-project-token")
    assessed = _run(
        store,
        scoped,
        store.assess_source,
        source["id"],
        body={"expected_revision": 4, "verdict": "supported", "reason": "Scoped review"},
    )
    assert assessed["id"] == source["id"]
    recorded = _run(
        store,
        scoped,
        store.record_variant_status,
        finding["variant_ids"][0],
        body={"expected_revision": 1, "status": "completed"},
    )
    assert recorded["status"] == "completed"

    other_body = {"name": "Other project"}
    _, other_project = store.create_project(
        owner, "other-project", canonical_digest(other_body), other_body
    )
    other_credential = {**body, "project_id": other_project["id"]}
    store.issue_credential(
        owner,
        "other-project-human",
        canonical_digest(other_credential),
        other_credential,
        "other-project-token",
    )
    other = store.authenticate("other-project-token")
    with pytest.raises(NotFound):
        store.assess_source(
            other,
            source["id"],
            "other-source",
            canonical_digest(
                {"expected_revision": 5, "verdict": "supported", "reason": "No access"}
            ),
            {"expected_revision": 5, "verdict": "supported", "reason": "No access"},
        )
    with pytest.raises(NotFound):
        store.record_variant_status(
            other,
            finding["variant_ids"][0],
            "other-variant",
            canonical_digest({"expected_revision": 2, "status": "failed"}),
            {"expected_revision": 2, "status": "failed"},
        )


def test_reaffirmation_rejects_superseded_source_confirmatory_open_and_revoked_replay(
    tmp_path: Path,
) -> None:
    store, owner, accepted, source, _ = _fixture(tmp_path)
    correction_body = {
        "expected_revision": 4,
        "uri": "urn:test:replacement",
        "quoted_passage": "Replacement passage",
        "content_digest": "d" * 64,
        "reason": "Correction",
    }
    correction = _run(store, owner, store.correct_source, source["id"], body=correction_body)
    replacement = correction["replacement"]
    _run(
        store,
        owner,
        store.assess_source,
        replacement["id"],
        body={"expected_revision": 1, "verdict": "supported", "reason": "Replacement review"},
    )
    command = {"expected_revision": 3, "reason": "Should fail"}
    with pytest.raises(Conflict, match="unsuperseded"):
        store.reaffirm_finding(
            owner, accepted["id"], "superseded-key", canonical_digest(command), command
        )

    confirmatory_root = tmp_path / "confirmatory"
    confirmatory_root.mkdir()
    confirm_store, confirm_owner, confirm_finding, _, _ = _fixture(confirmatory_root)
    with sqlite3.connect(confirm_store.database_path) as connection:
        connection.execute(
            "UPDATE research_studies SET mode='confirmatory' WHERE id=?",
            (confirm_finding["study_id"],),
        )
    with pytest.raises(Conflict, match="closed"):
        confirm_store.reaffirm_finding(
            confirm_owner,
            confirm_finding["id"],
            "confirmatory-key",
            canonical_digest(command),
            command,
        )
    variant_id = confirm_store.export_study(confirm_owner, confirm_finding["study_id"])["findings"][
        0
    ]["variant_ids"][0]
    _run(
        confirm_store,
        confirm_owner,
        confirm_store.record_variant_status,
        variant_id,
        body={"expected_revision": 1, "status": "completed"},
    )
    study = confirm_store.export_study(confirm_owner, confirm_finding["study_id"])
    study_revision = study["study"]["revision"]
    _run(
        confirm_store,
        confirm_owner,
        confirm_store.close_study,
        confirm_finding["study_id"],
        body={"expected_revision": study_revision},
    )
    reaffirmed = _run(
        confirm_store,
        confirm_owner,
        confirm_store.reaffirm_finding,
        confirm_finding["id"],
        body={"expected_revision": 3, "reason": "Closed ledger review"},
    )
    assert reaffirmed["status"] == "accepted"

    replay_root = tmp_path / "replay"
    replay_root.mkdir()
    replay_store, replay_owner, replay_finding, _, _ = _fixture(replay_root)
    replay_body = {"expected_revision": 3, "reason": "Replay review"}
    replay_store.reaffirm_finding(
        replay_owner,
        replay_finding["id"],
        "revoked-replay-key",
        canonical_digest(replay_body),
        replay_body,
    )
    credential_id = replay_owner["credential_id"]
    replay_store.revoke_credential(
        replay_owner, credential_id, "revoke-owner", canonical_digest({})
    )
    with pytest.raises(Forbidden, match="Unauthorized"):
        replay_store.reaffirm_finding(
            replay_owner,
            replay_finding["id"],
            "revoked-replay-key",
            canonical_digest(replay_body),
            replay_body,
        )
