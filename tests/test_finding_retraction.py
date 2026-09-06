from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_finding_reaffirmation import _fixture, _run

from tessera_control.api import create_app
from tessera_control.store import Conflict, Forbidden, NotFound, canonical_digest


def test_retracts_needs_review_and_preserves_record_and_event(tmp_path: Path) -> None:
    store, owner, finding, source, _ = _fixture(tmp_path)
    body = {"expected_revision": 3, "reason": "The reviewed claim is withdrawn."}
    reviewer = _human(
        store, owner, store.export_study(owner, finding["study_id"])["study"]["project_id"]
    )
    retracted = _run(store, reviewer, store.retract_finding, finding["id"], body=body)
    assert retracted["status"] == "retracted"
    assert retracted["revision"] == 4
    assert retracted["statement"] == finding["statement"]
    assert retracted["source_ids"] == finding["source_ids"]
    assert retracted["variant_ids"] == finding["variant_ids"]
    assert retracted["created_by_principal_id"] == finding["created_by_principal_id"]
    assert retracted["accepted_by_principal_id"] == finding["accepted_by_principal_id"]
    event = store.export_study(owner, finding["study_id"])["events"][-1]
    assert event["event_type"] == "research.finding_retracted"
    assert event["actor_principal_id"] == reviewer["id"]
    assert event["payload"] == {
        "study_id": finding["study_id"],
        "finding_id": finding["id"],
        "prior_revision": 3,
        "prior_status": "needs_review",
        "reason": body["reason"],
    }
    _run(
        store,
        owner,
        store.assess_source,
        source["id"],
        body={
            "expected_revision": 4,
            "verdict": "unsupported",
            "reason": "Review reopened",
        },
    )
    assert store.export_study(owner, finding["study_id"])["findings"][0] == retracted
    correction = {
        "expected_revision": 5,
        "uri": "urn:test:corrected",
        "quoted_passage": "Corrected passage",
        "content_digest": "d" * 64,
        "reason": "Source correction",
    }
    _run(store, owner, store.correct_source, source["id"], body=correction)
    latest = store.export_study(owner, finding["study_id"])["findings"][0]
    assert latest == retracted


def test_retract_api_validates_reason_replays_and_is_terminal(tmp_path: Path) -> None:
    store, owner, finding, _, _ = _fixture(tmp_path)
    token = (store.database_path.parent / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    body = {"expected_revision": 3, "reason": "Withdrawn after review."}
    with TestClient(create_app(store.database_path.parent)) as client:
        headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "retract-api"}
        invalid = client.post(
            f"/findings/{finding['id']}/retract",
            json={"expected_revision": 3, "reason": " "},
            headers=headers,
        )
        assert invalid.status_code == 422
        first = client.post(f"/findings/{finding['id']}/retract", json=body, headers=headers)
        replay = client.post(f"/findings/{finding['id']}/retract", json=body, headers=headers)
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    with pytest.raises(Conflict):
        store.reaffirm_finding(
            owner,
            finding["id"],
            "reaffirm-terminal",
            canonical_digest({"expected_revision": 4, "reason": "No"}),
            {"expected_revision": 4, "reason": "No"},
        )


def test_retract_candidate_accepted_and_event_failure_rolls_back(
    tmp_path: Path, monkeypatch
) -> None:
    store, owner, finding, source, _ = _fixture(tmp_path)
    variant_id = finding["variant_ids"][0]
    candidate = _run(
        store,
        owner,
        store.create_finding,
        finding["study_id"],
        body={
            "statement": "Candidate claim",
            "source_ids": [source["id"]],
            "variant_ids": [variant_id],
        },
    )
    retracted_candidate = _run(
        store,
        owner,
        store.retract_finding,
        candidate["id"],
        body={"expected_revision": 1, "reason": "Candidate withdrawn."},
    )
    assert retracted_candidate["status"] == "retracted"

    accepted = _run(
        store,
        owner,
        store.create_finding,
        finding["study_id"],
        body={
            "statement": "Accepted claim",
            "source_ids": [source["id"]],
            "variant_ids": [variant_id],
        },
    )
    accepted = _run(
        store, owner, store.accept_finding, accepted["id"], body={"expected_revision": 1}
    )
    retracted_accepted = _run(
        store,
        owner,
        store.retract_finding,
        accepted["id"],
        body={"expected_revision": 2, "reason": "Accepted claim withdrawn."},
    )
    assert retracted_accepted["status"] == "retracted"

    monkeypatch.setattr(
        store,
        "_event_for",
        lambda *args: (_ for _ in ()).throw(RuntimeError("event failure")),
    )
    with pytest.raises(RuntimeError, match="event failure"):
        store.retract_finding(
            owner,
            finding["id"],
            "rollback-retract",
            canonical_digest({"expected_revision": 3, "reason": "Rollback"}),
            {"expected_revision": 3, "reason": "Rollback"},
        )
    with sqlite3.connect(store.database_path) as connection:
        row = connection.execute(
            "SELECT status, revision FROM research_findings WHERE id=?", (finding["id"],)
        ).fetchone()
    assert row == ("needs_review", 3)


def _human(store, owner, project_id, kind="human_session"):
    body = {
        "kind": kind,
        "audience": "control",
        "project_id": project_id,
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    token = f"review-{project_id}-{kind}"
    store.issue_credential(owner, token, canonical_digest(body), body, token)
    return store.authenticate(token)


def test_retraction_authority_stale_conflict_and_terminal_rejection(tmp_path: Path) -> None:
    store, owner, finding, _, _ = _fixture(tmp_path)
    project_id = store.export_study(owner, finding["study_id"])["study"]["project_id"]
    agent = _human(store, owner, project_id, "agent")
    _, other = store.create_project(
        owner, "other", canonical_digest({"name": "Other"}), {"name": "Other"}
    )
    outsider = _human(store, owner, other["id"])
    body = {"expected_revision": 3, "reason": "Withdraw"}
    for actor, error in [(agent, Forbidden), (outsider, NotFound)]:
        with pytest.raises(error):
            store.retract_finding(actor, finding["id"], "denied", canonical_digest(body), body)
    with pytest.raises(Conflict):
        stale = {**body, "expected_revision": 2}
        store.retract_finding(owner, finding["id"], "stale", canonical_digest(stale), stale)
    store.retract_finding(owner, finding["id"], "retract", canonical_digest(body), body)
    changed = {**body, "reason": "Changed"}
    with pytest.raises(Conflict):
        store.retract_finding(owner, finding["id"], "retract", canonical_digest(changed), changed)
    terminal = {**body, "expected_revision": 4}
    with pytest.raises(Conflict):
        store.retract_finding(
            owner, finding["id"], "terminal", canonical_digest(terminal), terminal
        )
    with pytest.raises(Conflict):
        store.accept_finding(
            owner,
            finding["id"],
            "accept",
            canonical_digest({"expected_revision": 4}),
            {"expected_revision": 4},
        )
    store.revoke_credential(owner, owner["credential_id"], "revoke", canonical_digest({}))
    with pytest.raises(Forbidden):
        store.retract_finding(owner, finding["id"], "retract", canonical_digest(body), body)
