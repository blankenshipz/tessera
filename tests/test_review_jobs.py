"""Client-requested review jobs, findings and human decisions (ADR 0084). Synthetic only."""

import json
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tessera_control.api import create_app
from tessera_control.store import Forbidden, initialize_state

D = "a" * 64
P = "b" * 64
AUTO = {"X-Tessera-Automation": "test-script"}


class Policy:
    """Stub registry: one enrolled destination; haiku refuses private classes."""

    def check(self, destination_id, tier, data_classes, budget_cents):
        if destination_id != "dest-1":
            return "destination_not_enrolled"
        if (
            "private_code_approved" in data_classes
            or "private_derived" in data_classes
            and (tier == "haiku")
        ):
            return "data_class_not_allowed_for_destination"
        if budget_cents > 500:
            return "budget_exceeds_policy"
        if tier == "opus":
            return "tier_exceeds_policy"
        return None


def _enroll(client, owner, project, actions, destination="openai:personal"):
    token = secrets.token_urlsafe(32)
    response = client.post(
        "/credentials",
        headers={**owner, "Idempotency-Key": secrets.token_hex(8)},
        json={
            "token": token,
            "kind": "agent",
            "audience": "control",
            "project_id": project,
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "external_agent": {"actions": actions, "destination": destination},
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {token}", "X-Agent-Destination": destination}


def _seed(db, project, snapshot_id, digest, data_class=None):
    """Synthetic snapshot row (and optional recorded class) for input lookups."""
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO source_snapshots (id,project_id,digest,manifest_json,created_at,"
            "actor_principal_id) VALUES (?,?,?,'{}','t',(SELECT id FROM principals LIMIT 1))",
            (snapshot_id, project, digest),
        )
        if data_class == "public":
            conn.execute(
                "INSERT INTO snapshot_data_classes (snapshot_id,data_class,origin,license,source,"
                "actor_principal_id,decision_class,recorded_at) VALUES "
                "(?,'public','human_declared','MIT','ref',(SELECT id FROM principals LIMIT 1),"
                "'human_decision','t')",
                (snapshot_id,),
            )


def job(**over):
    body = {
        "kind": "plan_review",
        "title": "review",
        "inputs": [{"snapshot_id": "s1", "digest": D}],
        "prompt_digest": P,
        "destination_id": "dest-1",
        "tier": "sonnet",
        "budget_cents": 100,
    }
    return {**body, **over}


def finding(i="f1", **over):
    return {
        "id": i,
        "severity": "minor",
        "category": "design",
        "claim": "something to look at",
        "evidence": [{"source": "subject", "digest": D}],
        "confidence": 0.5,
        **over,
    }


@pytest.fixture
def world(tmp_path):
    state = tmp_path / "state"
    initialize_state(state)
    owner = {"Authorization": (state / "owner.header").read_text().strip().split(": ", 1)[1]}
    app = create_app(state)
    app.state.review_policy = Policy()
    client = TestClient(app)
    project = client.post(
        "/projects", headers={**owner, "Idempotency-Key": "p"}, json={"name": "R"}
    ).json()["id"]
    submitter = _enroll(client, owner, project, ["review:submit", "review:read"])
    reviewer = _enroll(client, owner, project, ["review:findings", "review:read"])

    class W:
        pass

    w = W()
    w.state, w.app, w.client, w.owner, w.project = state, app, client, owner, project
    w.submitter, w.reviewer = submitter, reviewer
    w.db = state / "tessera.sqlite3"
    w.seed = lambda sid="s1", digest=D, data_class=None, project=project: _seed(
        w.db, project, sid, digest, data_class
    )
    w.seed()

    def post(path, headers, body, key=None):
        return client.post(
            path, headers={**headers, "Idempotency-Key": key or secrets.token_hex(8)}, json=body
        )

    w.post = post
    w.submit = lambda body=None, who=None: post(
        f"/projects/{project}/review-jobs", who or submitter, body or job()
    )
    return w


def ingested(w, findings=None):
    created = w.submit().json()
    r = w.post(
        f"/review-jobs/{created['id']}/findings", w.reviewer, {"findings": findings or [finding()]}
    )
    assert r.status_code == 201, r.text
    return created["id"], r.json()["findings"][0]


def resolve_body(f, **over):
    return {
        "decision": "accept",
        "rationale": "looks right",
        "expected_revision": f["revision"],
        "expected_digest": f["digest"],
        **over,
    }


def test_default_policy_refuses_everything(tmp_path):
    state = tmp_path / "s"
    initialize_state(state)
    owner = {"Authorization": (state / "owner.header").read_text().strip().split(": ", 1)[1]}
    client = TestClient(create_app(state))
    pid = client.post(
        "/projects", headers={**owner, "Idempotency-Key": "p"}, json={"name": "R"}
    ).json()["id"]
    _seed(state / "tessera.sqlite3", pid, "s1", D)
    r = client.post(
        f"/projects/{pid}/review-jobs", headers={**owner, "Idempotency-Key": "j"}, json=job()
    )
    assert r.status_code == 409 and r.json()["reason"] == "destination_not_enrolled"


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"destination_id": "other"}, "destination_not_enrolled"),
        ({"tier": "opus"}, "tier_exceeds_policy"),
        ({"tier": "haiku"}, "data_class_not_allowed_for_destination"),
        ({"budget_cents": 501}, "budget_exceeds_policy"),
    ],
)
def test_submit_policy_refusals_store_nothing(world, over, reason):
    r = world.submit(job(**over))
    assert r.status_code == 409 and r.json()["reason"] == reason
    with sqlite3.connect(world.db) as db:
        assert db.execute("SELECT COUNT(*) FROM review_jobs").fetchone()[0] == 0


def test_sealed_holdout_input_refused(world):
    with sqlite3.connect(world.db) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(
            "INSERT INTO campaign_holdout_claims (id,org_id,project_id,partition_digest,"
            "claim_scope,campaign_id,stage_key,input_name,approval_id,spec_digest,status,opened_at)"
            " SELECT 'c1',org_id,id,?,'s','x','k','n','a','d','opened','t' FROM projects",
            (D,),
        )
    r = world.submit()
    assert r.status_code == 409 and r.json()["reason"] == "input_sealed"


@pytest.mark.parametrize(
    "over",
    [
        {"kind": "other"},
        {"inputs": []},
        {"inputs": [{"snapshot_id": "s", "digest": "x"}]},
        {"inputs": [{"snapshot_id": "s1", "digest": D, "data_class": "public"}]},
        {"budget_cents": 10001},
        {"egress": "internet"},
        {"tier": "gpt"},
        {"extra": 1},
    ],
)
def test_submit_validation_is_422(world, over):
    assert world.submit(job(**over)).status_code == 422


def test_submit_queued_idempotent_and_never_completed_without_runner(world):
    r = world.submit()
    assert r.status_code == 201
    j = r.json()
    assert j["state"] == "queued" and j["receipt_digest"] is None
    assert j["egress"] == "none" and j["execution_authorized"] is False
    key = "same"
    a = world.post(f"/projects/{world.project}/review-jobs", world.submitter, job(), key)
    b = world.post(f"/projects/{world.project}/review-jobs", world.submitter, job(), key)
    assert a.json() == b.json() and a.status_code == b.status_code == 201
    listed = world.client.get(
        f"/projects/{world.project}/review-jobs", headers=world.submitter
    ).json()
    assert [x["state"] for x in listed["jobs"]] == ["queued", "queued"] or len(listed["jobs"]) == 2
    assert all(x["state"] != "completed" for x in listed["jobs"])
    assert (
        world.client.get(f"/review-jobs/{j['id']}", headers=world.reviewer).json()["id"] == j["id"]
    )


def test_agent_without_scope_cannot_submit_or_read(world):
    bare = _enroll(world.client, world.owner, world.project, ["context:read"])
    assert world.submit(who=bare).status_code == 403
    assert (
        world.client.get(f"/projects/{world.project}/review-jobs", headers=bare).status_code == 403
    )


def test_fable_awaits_human_approval(world):
    j = world.submit(job(tier="fable")).json()
    assert j["state"] == "awaiting_approval"
    path = f"/review-jobs/{j['id']}/approve-tier"
    body = {"expected_revision": 1, "rationale": "ok to spend"}
    assert world.post(path, world.submitter, body).status_code == 403
    assert world.post(path, world.reviewer, body).status_code == 403
    # findings are not accepted before approval
    assert (
        world.post(f"/review-jobs/{j['id']}/findings", world.reviewer, {"findings": []}).status_code
        == 409
    )
    assert world.post(path, world.owner, {**body, "expected_revision": 2}).status_code == 409
    r = world.post(path, world.owner, body, "k")
    assert r.status_code == 200 and r.json()["state"] == "queued" and r.json()["revision"] == 2
    assert world.post(path, world.owner, body, "k").json() == r.json()
    assert world.post(path, world.owner, {**body, "expected_revision": 2}).status_code == 409
    with sqlite3.connect(world.db) as db:
        assert db.execute("SELECT COUNT(*) FROM review_job_approvals").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE review_job_approvals SET rationale='x'")


def test_findings_ingest_completes_job(world):
    jid, f = ingested(world, [finding("a"), finding("b", severity="blocking")])
    assert f["state"] == "open" and f["revision"] == 1 and f["untrusted_text"] is True
    done = world.client.get(f"/review-jobs/{jid}", headers=world.submitter).json()
    assert done["state"] == "completed" and done["receipt_digest"] is None
    listed = world.client.get(
        f"/projects/{world.project}/review-findings?job_id={jid}&state=open", headers=world.reviewer
    ).json()["findings"]
    assert len(listed) == 2
    one = world.client.get(f"/review-findings/{f['id']}", headers=world.owner).json()
    assert one["digest"] == f["digest"]
    # a completed job accepts no further findings
    assert (
        world.post(f"/review-jobs/{jid}/findings", world.reviewer, {"findings": []}).status_code
        == 409
    )


def test_receipt_digest_only_when_supplied(world):
    j = world.submit().json()
    r = world.post(
        f"/review-jobs/{j['id']}/findings",
        world.reviewer,
        {"findings": [], "receipt_digest": "c" * 64, "review_md_digest": "d" * 64},
    )
    assert r.json()["job"]["receipt_digest"] == "c" * 64


@pytest.mark.parametrize(
    "payload",
    [
        {"findings": [finding("a"), finding("a")]},
        {"findings": [finding("a"), finding("b", severity="fatal")]},
        {"findings": [finding("a"), finding("b", confidence=1.5)]},
        {"findings": [finding("a"), finding("b", claim="x" * 4097)]},
        {"findings": [finding("a"), {**finding("b"), "extra": 1}]},
        {"findings": [finding(str(i)) for i in range(201)]},
        {"findings": [finding("a", evidence=[{"source": "web"}])]},
        {"nope": 1},
    ],
)
def test_invalid_findings_all_or_nothing(world, payload):
    j = world.submit().json()
    r = world.post(f"/review-jobs/{j['id']}/findings", world.reviewer, payload)
    assert r.status_code == 422 and r.json()["reason"] == "failed_output_invalid"
    with sqlite3.connect(world.db) as db:
        assert db.execute("SELECT COUNT(*) FROM review_findings").fetchone()[0] == 0
        assert db.execute("SELECT state FROM review_jobs").fetchone()[0] == "queued"


def test_submitter_and_unscoped_cannot_ingest(world):
    both = _enroll(world.client, world.owner, world.project, ["review:submit", "review:findings"])
    j = world.submit(who=both).json()
    path = f"/review-jobs/{j['id']}/findings"
    assert world.post(path, both, {"findings": []}).status_code == 403
    assert world.post(path, world.submitter, {"findings": []}).status_code == 403
    assert world.post(path, world.owner, {"findings": []}).status_code == 403
    assert (
        world.post("/review-jobs/missing/findings", world.reviewer, {"findings": []}).status_code
        == 404
    )


def test_resolve_human_only_and_changes_nothing_else(world):
    jid, f = ingested(world)
    path = f"/review-findings/{f['id']}/resolve"
    for who in (world.submitter, world.reviewer):
        assert world.post(path, who, resolve_body(f)).status_code == 403
    with sqlite3.connect(world.db) as db:
        before = {
            t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("tasks", "campaigns", "agent_research_records", "review_jobs")
        }
        job_before = db.execute("SELECT * FROM review_jobs").fetchall()
    h = {**world.owner, **AUTO}
    bad = world.post(path, h, resolve_body(f, expected_digest="e" * 64))
    assert bad.status_code == 409
    assert world.post(path, h, resolve_body(f, expected_revision=2)).status_code == 409
    r = world.post(path, h, resolve_body(f), "k")
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["advisory"] is True and out["execution_authorized"] is False
    assert out["state"] == "accepted" and out["revision"] == 2
    d = out["decision"]
    assert d["finding_digest"] == f["digest"] and d["subject_digests"] == [D]
    assert d["rationale"] == "looks right" and d["actor_id"]
    assert world.post(path, h, resolve_body(f), "k").json() == out
    again = world.post(path, h, resolve_body(f))
    assert again.status_code == 200 and again.json()["decision"] == d
    assert world.post(path, h, resolve_body(f, decision="reject")).status_code == 409
    with sqlite3.connect(world.db) as db:
        after = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}
        assert after == before
        assert db.execute("SELECT * FROM review_jobs").fetchall() == job_before
        assert db.execute("SELECT COUNT(*) FROM review_finding_decisions").fetchone()[0] == 1
        ev = db.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='review_finding.resolved'"
        ).fetchone()[0]
        assert ev == 1
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE review_finding_decisions SET decision='reject'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM review_finding_decisions")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE review_findings SET finding_json='{}'")


def test_resolve_reject(world):
    _, f = ingested(world)
    r = world.post(
        f"/review-findings/{f['id']}/resolve",
        {**world.owner, **AUTO},
        resolve_body(f, decision="reject"),
    )
    assert r.status_code == 201 and r.json()["state"] == "rejected"


def test_cross_tenant_and_project_reads_denied(world):
    jid, f = ingested(world)
    other_state = world.state.parent / "other"
    initialize_state(other_state)
    other = {"Authorization": (other_state / "owner.header").read_text().strip().split(": ", 1)[1]}
    oc = TestClient(create_app(other_state))
    assert oc.get(f"/review-jobs/{jid}", headers=other).status_code == 404
    assert oc.get(f"/review-findings/{f['id']}", headers=other).status_code == 404
    assert oc.get(f"/projects/{world.project}/review-jobs", headers=other).status_code == 404
    second = world.client.post(
        "/projects", headers={**world.owner, "Idempotency-Key": "p2"}, json={"name": "R2"}
    ).json()["id"]
    assert (
        world.client.get(f"/projects/{second}/review-jobs", headers=world.reviewer).status_code
        == 404
    )
    assert (
        world.client.get(f"/projects/{second}/review-findings", headers=world.reviewer).status_code
        == 404
    )
    other_agent = _enroll(world.client, world.owner, second, ["review:read"])
    assert world.client.get(f"/review-jobs/{jid}", headers=other_agent).status_code == 404
    assert world.client.get(f"/review-findings/{f['id']}", headers=other_agent).status_code == 404


def test_runner_seams_and_atomicity(world):
    store = world.app.state.review_jobs
    principal = world.app.state.review_jobs.authenticate(world.owner["Authorization"].split()[1])
    j = world.submit().json()
    assert store.mark_running(principal, j["id"], "r1")[1]["state"] == "running"
    with pytest.raises(Exception, match="cannot move"):
        store.mark_running(principal, j["id"], "r2")
    out = store.mark_failed(principal, j["id"], "r3", "runner_error")[1]
    assert out["state"] == "failed" and out["reason"] == "runner_error"
    assert out["receipt_digest"] is None
    # a failure after the row insert rolls back row, event and idempotency together
    with sqlite3.connect(world.db) as db:
        db.execute(
            "CREATE TRIGGER boom BEFORE INSERT ON events "
            "WHEN NEW.event_type='review_job.submitted' "
            "BEGIN SELECT RAISE(ABORT,'boom'); END"
        )
    with pytest.raises(sqlite3.Error):
        store.submit(principal, world.project, "z", job(), Policy())
    with sqlite3.connect(world.db) as db:
        assert db.execute("SELECT COUNT(*) FROM review_jobs").fetchone()[0] == 1
        assert (
            db.execute("SELECT COUNT(*) FROM idempotency WHERE idempotency_key='z'").fetchone()[0]
            == 0
        )


def test_resolve_refuses_submitter_and_reviewer_principals(world):
    """A human who submitted a job cannot resolve it; a different human can."""
    human = _enroll_human(world)
    created = world.submit(who=human).json()
    r = world.post(
        f"/review-jobs/{created['id']}/findings", world.reviewer, {"findings": [finding()]}
    )
    f = r.json()["findings"][0]
    path = f"/review-findings/{f['id']}/resolve"
    assert world.post(path, {**human, **AUTO}, resolve_body(f)).status_code == 403
    assert world.post(path, {**world.owner, **AUTO}, resolve_body(f)).status_code == 201


def test_resolve_refuses_human_who_ingested_findings(world):
    jid, f = ingested(world)
    store = world.app.state.review_jobs
    with sqlite3.connect(world.db) as db:
        reviewer_principal = db.execute(
            "SELECT findings_actor_id FROM review_jobs WHERE id=?", (jid,)
        ).fetchone()[0]
        owner_principal = db.execute("SELECT id FROM principals WHERE role='human'").fetchone()[0]
        db.execute("UPDATE review_jobs SET findings_actor_id=? WHERE id=?", (owner_principal, jid))
    assert reviewer_principal and store
    path = f"/review-findings/{f['id']}/resolve"
    assert world.post(path, {**world.owner, **AUTO}, resolve_body(f)).status_code == 403


def _enroll_human(world):
    token = secrets.token_urlsafe(32)
    r = world.client.post(
        "/credentials",
        headers={**world.owner, "Idempotency-Key": secrets.token_hex(8)},
        json={
            "token": token,
            "kind": "human_session",
            "audience": "control",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {token}"}


def test_inputs_are_checked_and_class_is_derived(world):
    # made-up snapshot, wrong digest, other project: stable refusals that store nothing
    missing = world.submit(job(inputs=[{"snapshot_id": "nope", "digest": D}]))
    assert missing.status_code == 409 and missing.json()["reason"] == "input_not_found"
    wrong = world.submit(job(inputs=[{"snapshot_id": "s1", "digest": "c" * 64}]))
    assert wrong.status_code == 409 and wrong.json()["reason"] == "input_digest_mismatch"
    second = world.client.post(
        "/projects", headers={**world.owner, "Idempotency-Key": "p2"}, json={"name": "R2"}
    ).json()["id"]
    world.seed("s2", D, project=second)
    other = world.submit(job(inputs=[{"snapshot_id": "s2", "digest": D}]))
    assert other.status_code == 409 and other.json()["reason"] == "input_not_found"
    with sqlite3.connect(world.db) as db:
        assert db.execute("SELECT COUNT(*) FROM review_jobs").fetchone()[0] == 0
    # an unrecorded snapshot is private_derived; the policy sees that, not a client label
    seen = []

    class Spy(Policy):
        def check(self, destination_id, tier, data_classes, budget_cents):
            seen.append(list(data_classes))
            return super().check(destination_id, tier, data_classes, budget_cents)

    world.app.state.review_policy = Spy()
    ok = world.submit()
    assert ok.status_code == 201
    assert seen == [["private_derived"]]
    assert ok.json()["data_classes"] == ["private_derived"]
    assert ok.json()["inputs"] == [
        {"snapshot_id": "s1", "digest": D, "data_class": "private_derived"}
    ]
    # a private snapshot cannot be passed as public: haiku refuses private classes
    spoof = world.submit(job(tier="haiku"))
    assert spoof.json()["reason"] == "data_class_not_allowed_for_destination"
    world.seed("pub", "e" * 64, "public")
    pub = world.submit(job(tier="haiku", inputs=[{"snapshot_id": "pub", "digest": "e" * 64}]))
    assert pub.status_code == 201 and pub.json()["data_classes"] == ["public"]


def test_cross_tenant_input_not_found(world, tmp_path):
    other_state = tmp_path / "other"
    initialize_state(other_state)
    other = {"Authorization": (other_state / "owner.header").read_text().strip().split(": ", 1)[1]}
    oapp = create_app(other_state)
    oapp.state.review_policy = Policy()
    oc = TestClient(oapp)
    opid = oc.post(
        "/projects", headers={**other, "Idempotency-Key": "p"}, json={"name": "O"}
    ).json()["id"]
    _seed(other_state / "tessera.sqlite3", opid, "theirs", "f" * 64)
    # the foreign snapshot id is unknown here, and our snapshot id is unknown there
    r = world.submit(job(inputs=[{"snapshot_id": "theirs", "digest": "f" * 64}]))
    assert r.status_code == 409 and r.json()["reason"] == "input_not_found"
    r = oc.post(
        f"/projects/{opid}/review-jobs",
        headers={**other, "Idempotency-Key": "j"},
        json=job(),
    )
    assert r.status_code == 409 and r.json()["reason"] == "input_not_found"


def test_declared_but_unclaimed_and_derived_holdout_refused(world):
    world.seed("held", "1" * 64)
    world.seed("kid", "2" * 64)
    spec = {
        "stages": [
            {
                "key": "k",
                "evaluation": {
                    "final": True,
                    "holdout": {"input": "x", "partition_digest": "1" * 64, "claim_scope": "s"},
                },
            }
        ]
    }
    with sqlite3.connect(world.db) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        columns = [r[1] for r in db.execute("PRAGMA table_info(campaigns)") if r[3]]
        values = {
            "id": "camp",
            "project_id": world.project,
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
        assert db.execute("SELECT COUNT(*) FROM campaign_holdout_claims").fetchone()[0] == 0
    declared = world.submit(job(inputs=[{"snapshot_id": "held", "digest": "1" * 64}]))
    assert declared.status_code == 409 and declared.json()["reason"] == "input_sealed"
    with sqlite3.connect(world.db) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        for ident, sha in (("n1", "1" * 64), ("n2", "2" * 64)):
            db.execute(
                "INSERT INTO artifact_lineage_nodes (id,org_id,project_id,kind,sha256,label,"
                "origin,created_by_principal_id,created_at) SELECT ?,org_id,id,'other',?,'l',"
                "'external_import',(SELECT id FROM principals LIMIT 1),'t' FROM projects",
                (ident, sha),
            )
        db.execute(
            "INSERT INTO artifact_lineage_edges (id,org_id,project_id,child_node_id,"
            "parent_node_id,relation,rationale,created_by_principal_id,created_at) "
            "SELECT 'e1',org_id,id,'n2','n1','derived_from','r',"
            "(SELECT id FROM principals LIMIT 1),'t' FROM projects"
        )
    derived = world.submit(job(inputs=[{"snapshot_id": "kid", "digest": "2" * 64}]))
    assert derived.status_code == 409 and derived.json()["reason"] == "input_sealed"


def test_runner_seams_accept_trusted_non_human_runners(world):
    store = world.app.state.review_jobs
    token = lambda h: h["Authorization"].split()[1]  # noqa: E731
    reviewer = store.authenticate(token(world.reviewer))
    reviewer["agent_destination"] = "openai:personal"
    submitter = store.authenticate(token(world.submitter))
    submitter["agent_destination"] = "openai:personal"
    bare = _enroll(world.client, world.owner, world.project, ["context:read"])
    bare_principal = store.authenticate(token(bare))
    bare_principal["agent_destination"] = "openai:personal"
    j = world.submit().json()
    for who in (submitter, bare_principal):
        with pytest.raises(Forbidden):
            store.mark_running(who, j["id"], "x")
    assert store.mark_running(reviewer, j["id"], "r1")[1]["state"] == "running"
    assert store.mark_failed(reviewer, j["id"], "r2", "runner_error")[1]["state"] == "failed"
    k = world.submit().json()
    assert (
        store.mark_not_started(reviewer, k["id"], "r3", "no_capacity")[1]["state"] == "not_started"
    )
