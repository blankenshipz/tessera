from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient
from test_finding_reaffirmation import _fixture, _run
from test_finding_retraction import _human

from tessera_control.api import create_app
from tessera_control.store import Conflict, Forbidden, NotFound, canonical_digest


def _revision_body(finding, source, variant, **overrides):
    return {
        "expected_revision": finding["revision"],
        "kind": "narrow",
        "reason": "The claim is narrowed after review.",
        "statement": "Narrowed claim",
        "source_ids": [source["id"]],
        "variant_ids": [variant],
        **overrides,
    }


def _latest_finding(store, owner, finding):
    return next(
        item
        for item in store.export_study(owner, finding["study_id"])["findings"]
        if item["id"] == finding["id"]
    )


def _latest_source(store, owner, source):
    return next(
        item
        for item in store.export_study(owner, source["study_id"])["sources"]
        if item["id"] == source["id"]
    )


def test_revise_route_creates_linked_candidate_and_preserves_old_attribution(tmp_path) -> None:
    store, owner, finding, source, _ = _fixture(tmp_path)
    finding = _latest_finding(store, owner, finding)
    source = _latest_source(store, owner, source)
    reviewer = _human(
        store, owner, store.export_study(owner, finding["study_id"])["study"]["project_id"]
    )
    body = _revision_body(finding, source, finding["variant_ids"][0])
    token = f"review-{reviewer['project_id']}-{reviewer['kind']}"
    with TestClient(create_app(store.database_path.parent)) as client:
        response = client.post(
            f"/findings/{finding['id']}/revise",
            json=body,
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "route-revise"},
        )
    assert response.status_code == 201, response.text
    result = response.json()
    original, replacement = result["original"], result["replacement"]
    assert original["status"] == "superseded"
    assert original["revision"] == finding["revision"] + 1
    assert original["statement"] == finding["statement"]
    assert original["created_by_principal_id"] == finding["created_by_principal_id"]
    assert original["accepted_by_principal_id"] == finding["accepted_by_principal_id"]
    assert replacement["status"] == "candidate"
    assert replacement["revision"] == 1
    assert replacement["created_by_principal_id"] == reviewer["id"]
    assert replacement["accepted_by_principal_id"] is None
    assert replacement["supersedes_finding_id"] == finding["id"]
    assert replacement["revision_kind"] == "narrow"
    assert replacement["revision_reason"] == body["reason"]
    event = store.export_study(owner, finding["study_id"])["events"][-1]
    assert event["event_type"] == "research.finding_revised"
    assert event["payload"] == {
        "study_id": finding["study_id"],
        "finding_id": finding["id"],
        "replacement_finding_id": replacement["id"],
        "prior_revision": finding["revision"],
        "prior_status": finding["status"],
        "kind": "narrow",
        "reason": body["reason"],
    }


def test_revise_validates_request_authority_scope_and_replay(tmp_path) -> None:
    store, owner, finding, source, _ = _fixture(tmp_path)
    finding = _latest_finding(store, owner, finding)
    body = _revision_body(finding, source, finding["variant_ids"][0])
    token = (store.database_path.parent / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    with TestClient(create_app(store.database_path.parent)) as client:
        headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "validate-revise"}
        assert (
            client.post(
                f"/findings/{finding['id']}/revise", json={**body, "reason": " "}, headers=headers
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"/findings/{finding['id']}/revise", json={**body, "kind": "wrong"}, headers=headers
            ).status_code
            == 422
        )
        duplicate_sources = {**body, "source_ids": [source["id"], source["id"]]}
        assert (
            client.post(
                f"/findings/{finding['id']}/revise", json=duplicate_sources, headers=headers
            ).status_code
            == 400
        )
        first = client.post(f"/findings/{finding['id']}/revise", json=body, headers=headers)
        replay = client.post(f"/findings/{finding['id']}/revise", json=body, headers=headers)
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    changed = {**body, "reason": "A different reason changes the command digest."}
    with pytest.raises(Conflict):
        store.revise_finding(
            owner, finding["id"], "validate-revise", canonical_digest(changed), changed
        )
    with pytest.raises(Conflict):
        store.revise_finding(owner, finding["id"], "stale", canonical_digest(body), body)
    project_id = store.export_study(owner, finding["study_id"])["study"]["project_id"]
    agent = _human(store, owner, project_id, "agent")
    with pytest.raises(Forbidden):
        store.revise_finding(agent, finding["id"], "agent", canonical_digest(body), body)
    _, other_project = store.create_project(
        owner,
        "revision-other-project",
        canonical_digest({"name": "Revision other project"}),
        {"name": "Revision other project"},
    )
    outsider = _human(store, owner, other_project["id"])
    with pytest.raises(NotFound):
        store.revise_finding(outsider, finding["id"], "outsider", canonical_digest(body), body)

    project_id = store.export_study(owner, finding["study_id"])["study"]["project_id"]
    foreign_study = _run(
        store,
        owner,
        store.create_study,
        project_id,
        body={"purpose": "Foreign evidence", "protocol": "Separate study"},
    )
    foreign_source = _run(
        store,
        owner,
        store.create_source,
        foreign_study["id"],
        body={
            "uri": "urn:test:foreign-source",
            "quoted_passage": "Separate evidence",
            "content_digest": "f" * 64,
        },
    )
    foreign_variant = _run(
        store,
        owner,
        store.register_variant,
        foreign_study["id"],
        body={
            "name": "foreign variant",
            "method_digest": "f" * 64,
            "input_digest": "e" * 64,
            "metadata": {},
        },
    )
    foreign = {
        **body,
        "expected_revision": first.json()["replacement"]["revision"],
        "source_ids": [foreign_source["id"]],
        "variant_ids": [foreign_variant["id"]],
    }
    with pytest.raises(NotFound):
        store.revise_finding(
            owner,
            first.json()["replacement"]["id"],
            "foreign",
            canonical_digest(foreign),
            foreign,
        )
    store.revoke_credential(owner, owner["credential_id"], "revoke-owner", canonical_digest({}))
    with pytest.raises(Forbidden):
        store.revise_finding(owner, finding["id"], "validate-revise", canonical_digest(body), body)


def test_revise_allows_closed_confirmatory_candidate_then_acceptance(tmp_path) -> None:
    store, owner, finding, source, _ = _fixture(tmp_path)
    finding = _latest_finding(store, owner, finding)
    variant_id = finding["variant_ids"][0]
    _run(
        store,
        owner,
        store.record_variant_status,
        variant_id,
        body={"expected_revision": 1, "status": "completed", "outcome": {"ok": True}},
    )
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE research_studies SET mode='confirmatory' WHERE id=?", (finding["study_id"],)
        )
    study = store.export_study(owner, finding["study_id"])["study"]
    _run(
        store,
        owner,
        store.close_study,
        finding["study_id"],
        body={"expected_revision": study["revision"]},
    )
    body = _revision_body(finding, source, variant_id, kind="supersede")
    revised = _run(store, owner, store.revise_finding, finding["id"], body=body)
    replacement = _latest_finding(store, owner, revised["replacement"])
    accepted = _run(
        store,
        owner,
        store.accept_finding,
        replacement["id"],
        body={"expected_revision": 1},
    )
    assert accepted["status"] == "accepted"


def test_terminal_and_correction_paths_do_not_resurrect_superseded_finding(
    tmp_path, monkeypatch
) -> None:
    store, owner, finding, source, _ = _fixture(tmp_path)
    finding = _latest_finding(store, owner, finding)
    source = _latest_source(store, owner, source)
    body = _revision_body(finding, source, finding["variant_ids"][0])
    revised = _run(store, owner, store.revise_finding, finding["id"], body=body)
    original = revised["original"]
    for method, command in (
        (store.revise_finding, body),
        (store.retract_finding, {"expected_revision": original["revision"], "reason": "No"}),
    ):
        with pytest.raises(Conflict):
            method(
                owner,
                finding["id"],
                "terminal-" + method.__name__,
                canonical_digest(command),
                command,
            )
    correction = {
        "expected_revision": source["revision"],
        "uri": "urn:test:corrected-version",
        "quoted_passage": "Replacement passage",
        "content_digest": "e" * 64,
        "reason": "Correct source without changing history.",
    }
    _run(store, owner, store.correct_source, source["id"], body=correction)
    exported = store.export_study(owner, finding["study_id"])
    original = next(item for item in exported["findings"] if item["id"] == finding["id"])
    assert original["status"] == "superseded"

    replacement = _latest_finding(store, owner, revised["replacement"])
    before_rollback = store.export_study(owner, finding["study_id"])
    event_for = store._event_for
    monkeypatch.setattr(
        store, "_event_for", lambda *args: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    with pytest.raises(RuntimeError, match="boom"):
        store.revise_finding(
            owner,
            replacement["id"],
            "rollback-revise",
            canonical_digest({**body, "expected_revision": replacement["revision"]}),
            {**body, "expected_revision": replacement["revision"]},
        )
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute(
            "SELECT status, revision FROM research_findings WHERE id=?", (replacement["id"],)
        ).fetchone() == ("needs_review", replacement["revision"])
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM idempotency WHERE idempotency_key='rollback-revise'"
            ).fetchone()[0]
            == 0
        )
    assert store.export_study(owner, finding["study_id"]) == before_rollback
    monkeypatch.setattr(store, "_event_for", event_for)
    status, retried = store.revise_finding(
        owner,
        replacement["id"],
        "rollback-revise",
        canonical_digest({**body, "expected_revision": replacement["revision"]}),
        {**body, "expected_revision": replacement["revision"]},
    )
    assert status == 201
    assert retried["original"]["id"] == replacement["id"]
