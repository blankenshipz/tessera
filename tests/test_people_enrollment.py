"""Owner-enrolled people: a second person with their own enrolment root. Synthetic only."""

import hashlib
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_review_jobs import (
    AUTO,
    Policy,
    _enroll,
    _seed,
    enrol_person,
    enrol_second_root,
    finding,
    job,
    resolve_body,
)

from tessera_control import review_jobs
from tessera_control.api import create_app
from tessera_control.approval_trust import decision_class
from tessera_control.store import initialize_state

D = "a" * 64


def iso(**delta):
    return (datetime.now(UTC) + timedelta(**delta)).isoformat()


class World:
    def post(self, path, headers, body=None, key=None):
        return self.client.post(
            path,
            headers={**headers, "Idempotency-Key": key or secrets.token_hex(8)},
            json=body or {},
        )

    def enrol(self, who=None, key=None, **over):
        token = over.pop("token", secrets.token_urlsafe(32))
        body = {"token": token, "label": "Reviewer", "expires_at": iso(days=3), **over}
        response = self.post("/people", who or self.owner, body, key)
        return response, {"Authorization": f"Bearer {token}"}

    def audit(self, event_type):
        with sqlite3.connect(self.db) as db:
            return [
                r[0]
                for r in db.execute(
                    "SELECT payload_json FROM operator_audit_events WHERE event_type=?",
                    (event_type,),
                )
            ]


@pytest.fixture
def w(tmp_path):
    state = tmp_path / "state"
    initialize_state(state)
    w = World()
    w.owner = {"Authorization": (state / "owner.header").read_text().strip().split(": ", 1)[1]}
    w.app = create_app(state)
    w.app.state.review_policy = Policy()
    w.client = TestClient(w.app)
    w.db = state / "tessera.sqlite3"
    w.project = w.post("/projects", w.owner, {"name": "P"}, "p").json()["id"]
    return w


def test_owner_enrols_a_person_and_the_audit_has_no_secret(w):
    response, headers = w.enrol(label="  Second Person ")
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {
        "principal_id",
        "credential_id",
        "label",
        "role",
        "kind",
        "expires_at",
        "enrolled_by_principal_id",
    }
    assert (body["role"], body["kind"], body["label"]) == (
        "human",
        "human_session",
        "Second Person",
    )
    token = headers["Authorization"].split()[1]
    assert token not in response.text
    with sqlite3.connect(w.db) as db:
        owner = db.execute("SELECT id FROM principals WHERE role='human' LIMIT 1").fetchone()[0]
        cred = db.execute(
            "SELECT audience,project_id,parent_id,created_by_principal_id,kind FROM credentials "
            "WHERE id=?",
            (body["credential_id"],),
        ).fetchone()
        assert cred == ("control", None, None, None, "human_session")
        assert body["enrolled_by_principal_id"] == owner
        dump = "\n".join(
            str(r)
            for table in ("operator_audit_events", "events", "idempotency", "enrolled_people")
            for r in db.execute(f"SELECT * FROM {table}")
        )
        token_hash = db.execute(
            "SELECT token_hash FROM principals WHERE id=?", (body["principal_id"],)
        ).fetchone()[0]
    assert token not in dump and token_hash not in dump
    (payload,) = w.audit("person.enrolled")
    assert '"label":"Second Person"' in payload and body["principal_id"] in payload
    assert body["credential_id"] in payload and owner in payload and "expires_at" in payload
    # the person authenticates
    assert w.client.get("/projects", headers=headers).status_code == 200


def test_only_the_root_owner_may_enrol_list_or_renew(w):
    _, person = w.enrol()
    agent = _enroll(w.client, w.owner, w.project, ["review:read"])
    sibling_token = secrets.token_urlsafe(32)
    assert (
        w.post(
            "/credentials",
            w.owner,
            {
                "token": sibling_token,
                "kind": "human_session",
                "audience": "control",
                "expires_at": iso(hours=1),
            },
        ).status_code
        == 201
    )
    sibling = {"Authorization": f"Bearer {sibling_token}"}
    pid = w.client.get("/people", headers=w.owner).json()["people"][0]["principal_id"]
    for who in (person, agent, sibling):
        assert w.enrol(who)[0].status_code == 403
        assert w.client.get("/people", headers=who).status_code == 403
        assert w.post(f"/people/{pid}/renew", who, {"expires_at": iso(days=4)}).status_code == 403
    with sqlite3.connect(w.db) as db:
        assert db.execute("SELECT COUNT(*) FROM enrolled_people").fetchone()[0] == 1


def test_idempotent_replay_and_key_reuse_conflict(w):
    token = secrets.token_urlsafe(32)
    expires = iso(days=2)
    first, _ = w.enrol(key="k1", token=token, expires_at=expires)
    again, _ = w.enrol(key="k1", token=token, expires_at=expires)
    assert first.status_code == again.status_code == 201 and first.json() == again.json()
    different, _ = w.enrol(key="k1", token=token, expires_at=expires, label="Other")
    assert different.status_code == 409
    assert len(w.audit("person.enrolled")) == 1
    assert w.client.post("/people", headers=w.owner, json={}).status_code in (400, 422)
    missing = w.client.post(
        "/people",
        headers=w.owner,
        json={"token": secrets.token_urlsafe(32), "label": "x", "expires_at": iso(days=1)},
    )
    assert missing.status_code in (400, 422)


def test_expiry_bound_is_seven_days(w):
    assert w.enrol(expires_at=iso(days=7, minutes=5))[0].status_code == 409
    assert w.enrol(expires_at=iso(minutes=-1))[0].status_code == 409
    assert w.enrol(expires_at=iso(days=6, hours=23))[0].status_code == 201
    assert w.enrol(expires_at=datetime.now(UTC).replace(tzinfo=None).isoformat())[
        0
    ].status_code in (400, 422)
    assert w.enrol(label="   ")[0].status_code in (400, 422)


def test_renew_extends_and_never_revives(w):
    response, person = w.enrol(expires_at=iso(days=1))
    pid, cid = response.json()["principal_id"], response.json()["credential_id"]
    renewed = w.post(f"/people/{pid}/renew", w.owner, {"expires_at": iso(days=6)})
    assert renewed.status_code == 200, renewed.text
    assert renewed.json()["expires_at"] > response.json()["expires_at"]
    assert w.client.get("/projects", headers=person).status_code == 200
    assert len(w.audit("person.renewed")) == 1
    # cannot shorten, cannot exceed seven days, unknown person
    assert w.post(f"/people/{pid}/renew", w.owner, {"expires_at": iso(days=2)}).status_code == 409
    assert w.post(f"/people/{pid}/renew", w.owner, {"expires_at": iso(days=8)}).status_code == 409
    assert (
        w.post("/people/principal_nope/renew", w.owner, {"expires_at": iso(days=3)}).status_code
        == 404
    )
    # expired credential is not revived
    with sqlite3.connect(w.db) as db:
        db.execute("UPDATE credentials SET expires_at=? WHERE id=?", (iso(seconds=-5), cid))
    assert w.post(f"/people/{pid}/renew", w.owner, {"expires_at": iso(days=3)}).status_code == 403
    assert w.client.get("/projects", headers=person).status_code == 401
    # revoked credential is not revived
    other, _ = w.enrol()
    opid, ocid = other.json()["principal_id"], other.json()["credential_id"]
    assert w.post(f"/credentials/{ocid}/revoke", w.owner).status_code == 200
    assert w.post(f"/people/{opid}/renew", w.owner, {"expires_at": iso(days=3)}).status_code == 403


def test_revoke_then_unauthorized(w):
    response, person = w.enrol()
    assert w.client.get("/projects", headers=person).status_code == 200
    assert (
        w.post(f"/credentials/{response.json()['credential_id']}/revoke", w.owner).status_code
        == 200
    )
    assert w.client.get("/projects", headers=person).status_code == 401
    assert len(w.audit("credential.revoked")) == 1
    listed = w.client.get("/people", headers=w.owner).json()["people"]
    assert listed[0]["revoked_at"] is not None


def test_person_is_a_separate_enrolment_root_and_cannot_issue(w):
    response, person = w.enrol()
    pid = response.json()["principal_id"]
    session_token = secrets.token_urlsafe(32)
    w.post(
        "/credentials",
        w.owner,
        {
            "token": session_token,
            "kind": "human_session",
            "audience": "control",
            "expires_at": iso(hours=1),
        },
    )
    actor = w.app.state.review_jobs.authenticate(session_token)
    owner_actor = w.app.state.review_jobs.authenticate(w.owner["Authorization"].split()[1])
    with sqlite3.connect(w.db) as db:
        db.row_factory = sqlite3.Row
        roots, over = review_jobs._enrolment_roots(db, pid)
        assert roots == {pid} and not over
        assert not review_jobs._same_person(db, pid, owner_actor["id"])
        assert not review_jobs._same_person(db, pid, actor["id"])
        assert review_jobs._same_person(db, actor["id"], owner_actor["id"])
    refusals = [
        w.post(
            "/credentials",
            person,
            {
                "token": secrets.token_urlsafe(32),
                "kind": "human_session",
                "audience": "control",
                "expires_at": iso(hours=1),
            },
        ),
        w.post(
            "/credentials",
            person,
            {
                "token": secrets.token_urlsafe(32),
                "kind": "agent",
                "audience": "control",
                "project_id": w.project,
                "expires_at": iso(hours=1),
            },
        ),
    ]
    assert [r.status_code for r in refusals] == [403, 403]
    assert w.client.get("/credentials", headers=person).status_code == 403


def test_structure_requires_the_enrolment_row(w):
    response, person = w.enrol()
    assert w.client.get("/projects", headers=person).status_code == 200
    with sqlite3.connect(w.db) as db:
        db.execute("DELETE FROM enrolled_people")
    assert w.client.get("/projects", headers=person).status_code == 401


def test_lifetime_is_bounded_at_use_time(w):
    response, person = w.enrol(expires_at=iso(days=1))
    with sqlite3.connect(w.db) as db:
        db.execute(
            "UPDATE credentials SET expires_at=? WHERE id=?",
            (iso(days=7, hours=2), response.json()["credential_id"]),
        )
    assert w.client.get("/projects", headers=person).status_code == 401


def test_enrolled_person_resolves_an_agent_job_in_default_mode(w):
    submitter = _enroll(w.client, w.owner, w.project, ["review:submit", "review:read"])
    reviewer_root = enrol_second_root(w.db, w.owner, w.client)  # an issuing root (raw SQL)
    reviewer = _enroll(w.client, reviewer_root, w.project, ["review:findings", "review:read"])
    _seed(w.db, w.project, "s1", D)
    created = w.post(f"/projects/{w.project}/review-jobs", submitter, job()).json()
    posted = w.post(f"/review-jobs/{created['id']}/findings", reviewer, {"findings": [finding()]})
    assert posted.status_code == 201, posted.text
    f = posted.json()["findings"][0]
    path = f"/review-findings/{f['id']}/resolve"
    # the owner and the owner's sessions are the same person as the submitter
    assert w.post(path, {**w.owner, **AUTO}, resolve_body(f)).status_code == 403
    _, person = w.enrol()
    ok = w.post(path, person, resolve_body(f))
    assert ok.status_code == 201, ok.text
    assert ok.json()["separation_basis"] == "distinct_person"
    # a person-recorded decision is a human_session decision, not owner automation
    with sqlite3.connect(w.db) as db:
        payload = db.execute(
            "SELECT payload_json FROM events WHERE event_type='review_finding.resolved'"
        ).fetchone()[0]
    assert '"credential_kind":"human_session"' in payload
    token = person["Authorization"].split()[1]
    assert decision_class(w.app.state.review_jobs.authenticate(token)) == "human_session"
    # an enrolled person is not a reviewer-agent: still a person enrolled by the owner
    assert enrol_person(w.client, w.owner)["Authorization"]


def test_list_shows_labels_without_secrets_and_pages(w):
    for label in ("One", "Two", "Three"):
        assert w.enrol(label=label)[0].status_code == 201
    listed = w.client.get("/people", headers=w.owner)
    people = listed.json()["people"]
    assert [p["label"] for p in people] == ["One", "Two", "Three"]
    assert "token" not in listed.text
    assert set(people[0]) == {
        "principal_id",
        "credential_id",
        "label",
        "role",
        "kind",
        "expires_at",
        "enrolled_by_principal_id",
        "revoked_at",
        "enrolled_at",
    }
    page = w.client.get("/people?offset=1&limit=1", headers=w.owner).json()["people"]
    assert [p["label"] for p in page] == ["Two"]


def _worker_headers(w):
    """Raw-SQL worker-kind, worker-audience credential (not mintable over /credentials)."""
    token = secrets.token_urlsafe(32)
    with sqlite3.connect(w.db) as db:
        org = db.execute("SELECT org_id FROM principals LIMIT 1").fetchone()[0]
        db.execute(
            "INSERT INTO principals (id,org_id,role,token_hash,revoked_at,created_at) "
            "VALUES ('principal_wk',?,'agent',?,NULL,'2026-01-01T00:00:00Z')",
            (org, hashlib.sha256(token.encode()).hexdigest()),
        )
        db.execute(
            "INSERT INTO credentials (id,principal_id,kind,audience,project_id,parent_id,"
            "expires_at,revoked_at,created_at,created_by_principal_id) "
            "VALUES ('credential_wk','principal_wk','worker','worker',?,NULL,NULL,NULL,"
            "'2026-01-01T00:00:00Z',NULL)",
            (w.project,),
        )
    return {"Authorization": f"Bearer {token}"}


def test_person_submits_owner_reviewer_posts_second_person_resolves(w):
    """Default mode. Observed: a human session may submit (201); the owner's reviewer agent
    posts (201); Q resolves 201 distinct_person; the owner is 403 same_person; the submitter
    P is 403 same_principal (the identical principal is caught before the person walk)."""
    _seed(w.db, w.project, "s1", D)
    _, p = w.enrol(label="P")
    _, q = w.enrol(label="Q")
    reviewer = _enroll(w.client, w.owner, w.project, ["review:findings", "review:read"])
    submitted = w.post(f"/projects/{w.project}/review-jobs", p, job())
    assert submitted.status_code == 201, submitted.text
    posted = w.post(
        f"/review-jobs/{submitted.json()['id']}/findings", reviewer, {"findings": [finding()]}
    )
    assert posted.status_code == 201, posted.text
    f = posted.json()["findings"][0]
    path = f"/review-findings/{f['id']}/resolve"
    for who in (w.owner, {**w.owner, **AUTO}):
        refused = w.post(path, who, resolve_body(f))
        assert refused.status_code == 403 and refused.json()["reason"] == "same_person"
    for who in (p, {**p, **AUTO}):
        refused = w.post(path, who, resolve_body(f))
        assert refused.status_code == 403 and refused.json()["reason"] == "same_principal"
    ok = w.post(path, q, resolve_body(f))
    assert ok.status_code == 201, ok.text
    assert ok.json()["separation_basis"] == "distinct_person"


def test_same_owner_agents_and_worker_and_person_cannot_post_findings(w):
    """Observed: an owner-enrolled agent that submitted then a sibling owner-enrolled agent
    posting is 403 same_person; a worker-kind credential is 401 Unauthorized (control
    audience required); an enrolled person's session is 403 (an enrolled external-agent
    credential is required)."""
    _seed(w.db, w.project, "s1", D)
    _, q = w.enrol(label="Q")
    submitter = _enroll(w.client, w.owner, w.project, ["review:submit", "review:read"])
    sibling = _enroll(w.client, w.owner, w.project, ["review:findings", "review:read"])
    jid = w.post(f"/projects/{w.project}/review-jobs", submitter, job()).json()["id"]
    url = f"/review-jobs/{jid}/findings"
    same = w.post(url, sibling, {"findings": [finding()]})
    assert same.status_code == 403 and same.json()["reason"] == "same_person"
    worker = w.post(url, _worker_headers(w), {"findings": [finding()]})
    assert worker.status_code == 401 and worker.json() == {"detail": "Unauthorized"}
    person = w.post(url, q, {"findings": [finding()]})
    assert person.status_code == 403
    assert person.json() == {"detail": "An enrolled external-agent credential is required."}
    owner = w.post(url, w.owner, {"findings": [finding()]})
    assert owner.status_code == 403, owner.text


def _raw_parentless_human(w, expires_at, enrolled):
    """Raw-SQL parentless human_session (as the deep-chain tests insert), optionally enrolled."""
    token = secrets.token_urlsafe(32)
    with sqlite3.connect(w.db) as db:
        org, owner_id = db.execute("SELECT org_id, id FROM principals LIMIT 1").fetchone()
        db.execute(
            "INSERT INTO principals (id,org_id,role,token_hash,revoked_at,created_at) "
            "VALUES ('principal_raw',?,'human',?,NULL,'2026-01-01T00:00:00Z')",
            (org, hashlib.sha256(token.encode()).hexdigest()),
        )
        db.execute(
            "INSERT INTO credentials (id,principal_id,kind,audience,project_id,parent_id,"
            "expires_at,revoked_at,created_at,created_by_principal_id) "
            "VALUES ('credential_raw','principal_raw','human_session','control',"
            "NULL,NULL,?,NULL,?,NULL)",
            (expires_at, iso()),
        )
        if enrolled:
            db.execute(
                "INSERT INTO enrolled_people (principal_id,org_id,label,"
                "enrolled_by_principal_id,enrolled_at,renewed_at) VALUES (?,?,?,?,?,NULL)",
                ("principal_raw", org, "Raw", owner_id, iso()),
            )
    return {"Authorization": f"Bearer {token}"}


def test_parentless_human_session_needs_an_enrolment_row(w):
    raw = _raw_parentless_human(w, iso(days=1), enrolled=False)
    assert w.client.get("/projects", headers=raw).status_code == 401
    with sqlite3.connect(w.db) as db:
        org, owner_id = db.execute("SELECT org_id, id FROM principals LIMIT 1").fetchone()
        db.execute(
            "INSERT INTO enrolled_people (principal_id,org_id,label,"
            "enrolled_by_principal_id,enrolled_at,renewed_at) VALUES (?,?,?,?,?,NULL)",
            ("principal_raw", org, "Raw", owner_id, iso()),
        )
    assert w.client.get("/projects", headers=raw).status_code == 200


def test_enrolled_root_requires_a_non_null_expiry(w):
    raw = _raw_parentless_human(w, iso(days=1), enrolled=True)
    assert w.client.get("/projects", headers=raw).status_code == 200
    with sqlite3.connect(w.db) as db:
        db.execute("UPDATE credentials SET expires_at=NULL WHERE id='credential_raw'")
    assert w.client.get("/projects", headers=raw).status_code == 401


def _structure_row(**overrides):
    values = {
        "kind": "human_session",
        "role": "human",
        "audience": "control",
        "project_id": None,
        "parent_id": None,
        "expires_at": "2099-01-01T00:00:00Z",
        "principal_id": "principal_x",
    }
    values.update(overrides)
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE enrolled_people (principal_id TEXT PRIMARY KEY)")
    columns = ", ".join(f"? AS {name}" for name in values)
    row = connection.execute(f"SELECT {columns}", tuple(values.values())).fetchone()
    return connection, row


def test_structure_guard_unit_requires_row_project_null_and_expiry():
    """Pin the structure check itself, apart from the separate use-time check."""
    from tessera_control.store import Forbidden, Store

    connection, row = _structure_row()
    with pytest.raises(Forbidden):  # parentless session without an enrolled_people row
        Store._validate_credential_structure(connection, row)
    connection.execute("INSERT INTO enrolled_people VALUES ('principal_x')")
    Store._validate_credential_structure(connection, row)  # now a valid enrolled root
    for override in ({"project_id": "project_1"}, {"expires_at": None}, {"audience": "worker"}):
        connection, row = _structure_row(**override)
        connection.execute("INSERT INTO enrolled_people VALUES ('principal_x')")
        with pytest.raises(Forbidden):
            Store._validate_credential_structure(connection, row)
