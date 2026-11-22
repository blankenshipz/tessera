"""Person-level separation of duties for review jobs (ADR 0084). Synthetic only.

'Person' is the enrolment root: the root principal(s) reached by walking the credential chain.
Sibling credentials of one owner are the same person. A deployment opt-in relaxes this to the
identical principal only, and every use is recorded as ``separation_basis``.
"""

import secrets
import sqlite3

import pytest
from fastapi.testclient import TestClient
from test_review_jobs import (
    AUTO,
    Policy,
    _enroll,
    _enroll_human,
    _seed,
    enrol_person,
    enrol_second_root,
    finding,
    job,
    resolve_body,
)

from tessera_control import review_jobs
from tessera_control.api import create_app
from tessera_control.store import Forbidden, initialize_state

D = "a" * 64


def build(tmp_path, single_owner=False):
    state = tmp_path / ("solo" if single_owner else "default") / "state"
    state.parent.mkdir()
    initialize_state(state)
    owner = {"Authorization": (state / "owner.header").read_text().strip().split(": ", 1)[1]}
    app = create_app(state, review_single_owner=single_owner)
    app.state.review_policy = Policy()
    client = TestClient(app)
    project = client.post(
        "/projects", headers={**owner, "Idempotency-Key": "p"}, json={"name": "R"}
    ).json()["id"]

    class W:
        pass

    w = W()
    w.state, w.app, w.client, w.owner, w.project = state, app, client, owner, project
    w.db = state / "tessera.sqlite3"
    w.store = app.state.review_jobs
    w.submitter = _enroll(client, owner, project, ["review:submit", "review:read"])
    w.reviewer_root = enrol_second_root(w.db, owner, client)
    w.reviewer = _enroll(client, w.reviewer_root, project, ["review:findings", "review:read"])
    _seed(w.db, project, "s1", D)

    def post(path, headers, body, key=None):
        return client.post(
            path, headers={**headers, "Idempotency-Key": key or secrets.token_hex(8)}, json=body
        )

    w.post = post
    w.submit = lambda who=None, body=None: post(
        f"/projects/{project}/review-jobs", who or w.submitter, body or job()
    )

    def ingest(who=None, key=None):
        created = w.submit().json()
        r = post(
            f"/review-jobs/{created['id']}/findings",
            who or w.reviewer,
            {"findings": [finding()]},
            key,
        )
        return created["id"], r

    w.ingest = ingest

    def principal(headers, destination=True):
        actor = w.store.authenticate(headers["Authorization"].split()[1])
        if destination:
            actor["agent_destination"] = "openai:personal"
        return actor

    w.principal = principal
    return w


@pytest.fixture
def world(tmp_path):
    return build(tmp_path)


@pytest.fixture
def solo(tmp_path):
    return build(tmp_path, single_owner=True)


def resolve(w, who, jid_finding):
    path = f"/review-findings/{jid_finding['id']}/resolve"
    return w.post(path, {**who, **AUTO}, resolve_body(jid_finding))


def ingested(w):
    jid, r = w.ingest()
    assert r.status_code == 201, r.text
    return jid, r.json()["findings"][0]


def test_opt_in_is_off_by_default_and_visible_in_status(world, solo):
    assert world.app.state.review_single_owner is False
    status = world.client.get("/external-agent/status", headers=world.submitter).json()
    assert status["review_single_owner"] is False and "actions" in status
    assert solo.app.state.review_single_owner is True
    assert (
        solo.client.get("/external-agent/status", headers=solo.submitter).json()[
            "review_single_owner"
        ]
        is True
    )


def test_owner_sibling_session_is_refused_by_default(world):
    _, f = ingested(world)
    sibling = _enroll_human(world)
    refused = resolve(world, sibling, f)
    assert refused.status_code == 403 and refused.json()["reason"] == "same_person"
    assert resolve(world, world.owner, f).status_code == 403
    with sqlite3.connect(world.db) as db:
        assert db.execute("SELECT COUNT(*) FROM review_finding_decisions").fetchone()[0] == 0


def test_second_enrolment_root_resolves_with_distinct_person(world):
    _, f = ingested(world)
    second = enrol_person(world.client, world.owner)
    ok = resolve(world, second, f)
    assert ok.status_code == 201, ok.text
    assert ok.json()["separation_basis"] == "distinct_person"
    assert ok.json()["decision"]["separation_basis"] == "distinct_person"
    with sqlite3.connect(world.db) as db:
        assert db.execute("SELECT separation_basis FROM review_finding_decisions").fetchall() == [
            ("distinct_person",)
        ]
    # the reviewer's own root is the same person as the reviewer who ingested
    other = {"Authorization": world.reviewer_root["Authorization"]}
    _, f2 = ingested(world)
    assert resolve(world, other, f2).json()["reason"] == "same_person"


def test_opt_in_allows_owner_sibling_and_records_basis_everywhere(solo):
    jid, f = ingested(solo)
    sibling = _enroll_human(solo)
    path = f"/review-findings/{f['id']}/resolve"
    body = resolve_body(f)
    r = solo.post(path, {**sibling, **AUTO}, body, "k1")
    assert r.status_code == 201, r.text
    assert r.json()["separation_basis"] == "single_owner_opt_in"
    assert r.json()["decision"]["separation_basis"] == "single_owner_opt_in"
    with sqlite3.connect(solo.db) as db:
        assert db.execute("SELECT separation_basis FROM review_finding_decisions").fetchall() == [
            ("single_owner_opt_in",)
        ]
        payload = db.execute(
            "SELECT payload_json FROM events WHERE event_type='review_finding.resolved'"
        ).fetchone()[0]
    assert '"separation_basis": "single_owner_opt_in"' in payload.replace('":"', '": "')
    read = solo.client.get(f"/review-findings/{f['id']}", headers=solo.owner).json()
    assert read["separation_basis"] == "single_owner_opt_in"
    # idempotent replays (same key, and a fresh key) return the stored basis
    assert solo.post(path, {**sibling, **AUTO}, body, "k1").json() == r.json()
    again = solo.post(path, {**sibling, **AUTO}, body)
    assert again.status_code == 200 and again.json()["separation_basis"] == "single_owner_opt_in"
    status = solo.client.get("/external-agent/status", headers=solo.submitter).json()
    assert status["review_single_owner"] is True


def test_opt_in_with_distinct_person_still_records_distinct(solo):
    _, f = ingested(solo)
    second = enrol_person(solo.client, solo.owner)
    assert resolve(solo, second, f).json()["separation_basis"] == "distinct_person"


def test_opt_in_still_refuses_the_identical_principal(solo):
    human = _enroll_human(solo)
    created = solo.submit(who=human).json()
    r = solo.post(
        f"/review-jobs/{created['id']}/findings", solo.reviewer, {"findings": [finding()]}
    )
    f = r.json()["findings"][0]
    refused = resolve(solo, human, f)
    assert refused.status_code == 403 and refused.json()["reason"] == "same_principal"
    # the owner (same person, a different principal) may, under the opt-in
    assert resolve(solo, solo.owner, f).json()["separation_basis"] == "single_owner_opt_in"


def _deep_chain(db, org, length, tag):
    """principals chain[0] <- chain[1] <- ... each created by the previous; returns the leaf."""
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        previous = None
        for n in range(length):
            pid = f"principal_{tag}{n}"
            conn.execute(
                "INSERT INTO principals (id,org_id,role,token_hash,revoked_at,created_at) "
                "VALUES (?,?,'human',?,NULL,'t')",
                (pid, org, secrets.token_hex(32)),
            )
            conn.execute(
                "INSERT INTO credentials (id,principal_id,kind,audience,project_id,parent_id,"
                "expires_at,revoked_at,created_at,created_by_principal_id) "
                "VALUES (?,?,'human_session','control',NULL,NULL,NULL,NULL,'t',?)",
                (f"credential_{tag}{n}", pid, previous),
            )
            previous = pid
    return previous


def _org(db):
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT org_id FROM principals LIMIT 1").fetchone()[0]


def test_chain_walk_within_bound_is_a_distinct_person(world):
    jid, f = ingested(world)
    leaf = _deep_chain(world.db, _org(world.db), 10, "ok")
    with sqlite3.connect(world.db) as db:
        db.execute("UPDATE review_jobs SET actor_id=? WHERE id=?", (leaf, jid))
    second = enrol_person(world.client, world.owner)
    assert resolve(world, second, f).status_code == 201


def test_chain_deeper_than_the_bound_fails_closed(world):
    jid, f = ingested(world)
    leaf = _deep_chain(world.db, _org(world.db), 80, "deep")
    with sqlite3.connect(world.db) as db:
        db.execute("UPDATE review_jobs SET actor_id=? WHERE id=?", (leaf, jid))
    second = enrol_person(world.client, world.owner)
    refused = resolve(world, second, f)
    assert refused.status_code == 403 and refused.json()["reason"] == "same_person"
    with sqlite3.connect(world.db) as db:
        assert db.execute("SELECT COUNT(*) FROM review_finding_decisions").fetchone()[0] == 0


def test_ingest_follows_the_person_rule(world, solo):
    sibling = _enroll(world.client, world.owner, world.project, ["review:findings"])
    jid, r = world.ingest(sibling)
    assert r.status_code == 403 and r.json()["reason"] == "same_person"
    jid, ok = world.ingest()
    assert ok.status_code == 201
    assert ok.json()["job"]["findings_separation_basis"] == "distinct_person"
    # under the opt-in the same person may supply findings, recorded on the job
    sibling = _enroll(solo.client, solo.owner, solo.project, ["review:findings"])
    jid, r = solo.ingest(sibling, key="ing")
    assert r.status_code == 201, r.text
    assert r.json()["job"]["findings_separation_basis"] == "single_owner_opt_in"
    replay = solo.post(f"/review-jobs/{jid}/findings", sibling, {"findings": [finding()]}, "ing")
    assert replay.json() == r.json()
    with sqlite3.connect(solo.db) as db:
        assert db.execute(
            "SELECT findings_separation_basis FROM review_jobs WHERE id=?", (jid,)
        ).fetchone() == ("single_owner_opt_in",)
        payload = db.execute(
            "SELECT payload_json FROM events WHERE event_type='review_job.completed'"
        ).fetchone()[0]
    assert "single_owner_opt_in" in payload
    # but never the submitting credential itself
    both = _enroll(solo.client, solo.owner, solo.project, ["review:submit", "review:findings"])
    own = solo.submit(who=both).json()
    refused = solo.post(f"/review-jobs/{own['id']}/findings", both, {"findings": [finding()]})
    assert refused.status_code == 403 and refused.json()["reason"] == "same_principal"


def test_runner_seams_follow_the_person_rule(world, solo):
    sibling = _enroll(world.client, world.owner, world.project, ["review:findings"])
    j = world.submit().json()
    with pytest.raises(Forbidden):
        world.store.mark_running(world.principal(sibling), j["id"], "r0")
    other = world.principal(world.reviewer)
    assert world.store.mark_running(other, j["id"], "r1")[1]["state"] == "running"
    sibling = _enroll(solo.client, solo.owner, solo.project, ["review:findings"])
    k = solo.submit().json()
    assert solo.store.mark_running(solo.principal(sibling), k["id"], "r2")[1]["state"] == "running"
    both = _enroll(solo.client, solo.owner, solo.project, ["review:submit", "review:findings"])
    mine = solo.submit(who=both).json()
    with pytest.raises(Forbidden):
        solo.store.mark_running(solo.principal(both), mine["id"], "r3")


def _lineage(db, nodes, edges):
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        for ident, sha in nodes:
            conn.execute(
                "INSERT INTO artifact_lineage_nodes (id,org_id,project_id,kind,sha256,label,"
                "origin,created_by_principal_id,created_at) SELECT ?,org_id,id,'other',?,'l',"
                "'external_import',(SELECT id FROM principals LIMIT 1),'t' FROM projects",
                (ident, sha),
            )
        for n, (child, parent) in enumerate(edges):
            conn.execute(
                "INSERT INTO artifact_lineage_edges (id,org_id,project_id,child_node_id,"
                "parent_node_id,relation,rationale,created_by_principal_id,created_at) "
                "SELECT ?,org_id,id,?,?,'derived_from','r',"
                "(SELECT id FROM principals LIMIT 1),'t' FROM projects",
                (f"e{n}", child, parent),
            )


def _declare_holdout(w, partition):
    import json

    spec = {"stages": [{"evaluation": {"holdout": {"partition_digest": partition}}}]}
    with sqlite3.connect(w.db) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        columns = [r[1] for r in db.execute("PRAGMA table_info(campaigns)") if r[3]]
        values = {
            "id": "camp",
            "project_id": w.project,
            "spec_json": json.dumps(spec),
            "status": "submitted",
        }
        for c in columns:
            values.setdefault(
                c, "0" * 64 if "digest" in c else 1 if "cap" in c or c == "revision" else "x"
            )
        db.execute(
            f"INSERT INTO campaigns ({','.join(values)}) VALUES ({','.join('?' * len(values))})",
            list(values.values()),
        )


def test_lineage_descendant_of_a_declared_holdout_is_sealed(world):
    held, mid, leaf = "1" * 64, "2" * 64, "3" * 64
    _declare_holdout(world, held)
    _lineage(
        world.db,
        [("g", held), ("m", mid), ("c", leaf)],
        [("m", "g"), ("c", "m")],  # leaf derives from mid derives from the held partition
    )
    _seed(world.db, world.project, "sleaf", leaf)
    _seed(world.db, world.project, "sfree", "4" * 64)
    sealed = world.submit(body=job(inputs=[{"snapshot_id": "sleaf", "digest": leaf}]))
    assert sealed.status_code == 409 and sealed.json()["reason"] == "input_sealed"
    free = world.submit(body=job(inputs=[{"snapshot_id": "sfree", "digest": "4" * 64}]))
    assert free.status_code == 201


def test_lineage_walk_bound_exceeded_seals_all_inputs(world, monkeypatch):
    chain = [f"x{n}" for n in range(6)]
    digests = [f"{n + 5:064x}" for n in range(6)]
    _lineage(
        world.db,
        list(zip(chain, digests, strict=True)),
        [(chain[n], chain[n + 1]) for n in range(5)],
    )
    _seed(world.db, world.project, "sdeep", digests[0])
    body = job(inputs=[{"snapshot_id": "sdeep", "digest": digests[0]}])
    assert world.submit(body=body).status_code == 201  # no holdout anywhere: allowed
    monkeypatch.setattr(review_jobs, "_LINEAGE_WALK_LIMIT", 3)
    sealed = world.submit(body=body)
    assert sealed.status_code == 409 and sealed.json()["reason"] == "input_sealed"
    # unrelated input without lineage still walks nothing and is allowed? it has no rows
    _seed(world.db, world.project, "sfree", "9" * 64)
    free = world.submit(body=job(inputs=[{"snapshot_id": "sfree", "digest": "9" * 64}]))
    assert free.status_code == 201
