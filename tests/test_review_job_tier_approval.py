"""Review-job tiers from configuration and the per-job tier approval's bound grant.

Synthetic only: the in-process control plane, a stub destination policy, synthetic model ids.
No model is called and no provider is contacted.
"""

# ruff: noqa: F811
import json
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from test_review_jobs import job, world  # noqa: F401

from tessera_control.cli import parser
from tessera_control.migrations import (
    LATEST_SCHEMA_VERSION,
    MIGRATIONS,
    REVIEW_TIERS_SCHEMA_VERSION,
)
from tessera_control.review_tiers import (
    DEFAULT_TIERS,
    ReviewTiers,
    load_review_tiers,
    parse_review_tiers,
)
from tessera_control.store import initialize_state, migrate_state

SUBJECT_ACTIONS = ["authority:consume", "model_spend:record"]
OLD_TIER_CHECK = "tier TEXT NOT NULL CHECK(tier IN ('haiku','sonnet','opus','fable'))"


def tier_grant(w, model, provider="dest-1", budget=100, limit_provider=None):
    """A human-issued reasoning grant of ``w.project`` for ``provider``/``model``; its id."""
    token = secrets.token_urlsafe(32)
    created = w.post(
        "/credentials",
        w.owner,
        {
            "token": token,
            "kind": "agent",
            "audience": "control",
            "project_id": w.project,
            "expires_at": (datetime.now(UTC) + timedelta(hours=3)).isoformat(),
            "external_agent": {"actions": SUBJECT_ACTIONS, "destination": "lab"},
        },
    )
    assert created.status_code == 201, created.text
    limits = w.client.put(
        "/model-spend/limits",
        json={
            "project_id": w.project,
            "provider_id": limit_provider or provider,
            "per_call_cents": 5,
            "daily_cents": 20,
        },
        headers={**w.owner, "Idempotency-Key": secrets.token_hex(8)},
    )
    assert limits.status_code == 200, limits.text
    bounds = {
        "kind": "reasoning",
        "subject_credential_id": created.json()["credential_id"],
        "destination": "lab",
        "model": model,
        "purpose": "pays for one review job",
        "provider_id": provider,
        "actions": ["model:call"],
        "budget_cents": budget,
        "max_duration_seconds": 3600,
        "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
    }
    issued = w.post(f"/projects/{w.project}/agent-authority-grants", w.owner, {"bounds": bounds})
    assert issued.status_code == 201, issued.text
    return issued.json()["id"]


def approve(w, job_id, grant_id=None, revision=1, who=None, key=None):
    body = {"expected_revision": revision, "rationale": "the spend is worth it"}
    if grant_id is not None:
        body["grant_id"] = grant_id
    return w.post(f"/review-jobs/{job_id}/approve-tier", who or w.owner, body, key)


def runner(w):
    store = w.app.state.review_jobs
    return store, store.authenticate(w.resolver["Authorization"].split()[1])


def events(w, kind):
    with sqlite3.connect(w.db) as db:
        return [
            json.loads(r[0])
            for r in db.execute(
                "SELECT payload_json FROM events WHERE event_type=? ORDER BY rowid", (kind,)
            )
        ]


# -- configuration ------------------------------------------------------------------------


def test_default_config_reproduces_the_built_in_tiers(world):
    assert world.app.state.review_jobs.tiers == DEFAULT_TIERS
    assert DEFAULT_TIERS.tiers == ("haiku", "sonnet", "opus", "fable")
    assert [DEFAULT_TIERS.requires_approval(t) for t in DEFAULT_TIERS.tiers] == [
        False,
        False,
        False,
        True,
    ]
    assert world.submit(job(tier="sonnet")).json()["state"] == "queued"
    assert world.submit(job(tier="fable")).json()["state"] == "awaiting_approval"


def test_config_file_validation(tmp_path):
    path = tmp_path / "tiers.json"
    path.write_text(
        json.dumps({"tiers": ["small", "large"], "approval_from": "large", "models": {}})
    )
    assert load_review_tiers(path) == ReviewTiers(("small", "large"), "large", {})
    assert parse_review_tiers({"tiers": ["a"], "approval_from": None}).requires_approval("a") is (
        False
    )
    for bad in (
        {"tiers": []},
        {"tiers": ["a", "a"]},
        {"tiers": ["Big"]},
        {"tiers": ["a"], "approval_from": "b"},
        {"tiers": ["a"], "models": {"b": ["m"]}},
        {"tiers": ["a"], "models": {"a": []}},
        {"tiers": ["a"], "extra": 1},
        ["a"],
    ):
        with pytest.raises(ValueError):
            parse_review_tiers(bad)
    path.write_text("{not json")
    with pytest.raises(ValueError):
        load_review_tiers(path)
    assert (
        parser()
        .parse_args(
            ["serve", "--state-dir", str(tmp_path), "--port", "1", "--review-tiers", str(path)]
        )
        .review_tiers
        == path
    )


def test_custom_tier_list_and_threshold(world):
    world.app.state.review_jobs.tiers = ReviewTiers(("small", "medium", "large"), "medium")
    assert world.submit(job(tier="small")).json()["state"] == "queued"
    assert world.submit(job(tier="medium")).json()["state"] == "awaiting_approval"
    assert world.submit(job(tier="large")).json()["state"] == "awaiting_approval"
    # The built-in names are not special once a deployment configures its own tiers.
    refused = world.submit(job(tier="fable"))
    assert refused.status_code == 422 and refused.json()["reason"] == "tier_not_configured"
    world.app.state.review_jobs.tiers = ReviewTiers(("small", "large"), None)
    assert world.submit(job(tier="large")).json()["state"] == "queued"


def test_unknown_tier_is_refused(world):
    refused = world.submit(job(tier="enormous"))
    assert refused.status_code == 422 and refused.json()["reason"] == "tier_not_configured"
    assert world.submit(job(tier="Not A Tier")).status_code == 422  # malformed: request shape
    with sqlite3.connect(world.db) as db:
        assert db.execute("SELECT COUNT(*) FROM review_jobs").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO review_jobs (id,org_id,project_id,credential_id,actor_id,kind,"
                "title,inputs_json,prompt_digest,destination_id,tier,budget_cents,egress,state,"
                "revision,digest,created_at,updated_at) VALUES ('x','o','p','c','a','plan_review',"
                "'t','[]',?,'d','BAD TIER',1,'none','queued',1,?,'t','t')",
                ("a" * 64, "a" * 64),
            )


# -- approval and its grant ---------------------------------------------------------------


def test_above_threshold_without_approval_cannot_run(world):
    j = world.submit(job(tier="fable")).json()
    store, principal = runner(world)
    with pytest.raises(Exception, match="cannot move"):
        store.mark_running(principal, j["id"], "r1")
    refused = approve(world, j["id"])  # an approval must name a grant
    assert refused.status_code == 409 and refused.json()["reason"] == "tier_grant_required"
    assert world.client.get(f"/review-jobs/{j['id']}", headers=world.owner).json()["state"] == (
        "awaiting_approval"
    )


@pytest.mark.parametrize(
    "grant_kwargs, reason",
    [
        ({"model": "vendor-sonnet-4"}, "tier_grant_scope_mismatch"),  # another tier's model
        ({"model": "vendor-fable-1", "provider": "other-dest"}, "tier_grant_scope_mismatch"),
        ({"model": "vendor-fable-1", "budget": 50}, "tier_grant_budget_insufficient"),
    ],
)
def test_approval_with_a_non_matching_grant_is_refused(world, grant_kwargs, reason):
    j = world.submit(job(tier="fable")).json()
    grant = tier_grant(world, **grant_kwargs)
    refused = approve(world, j["id"], grant)
    assert refused.status_code == 409 and refused.json()["reason"] == reason
    with sqlite3.connect(world.db) as db:
        assert db.execute("SELECT COUNT(*) FROM review_job_approvals").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM review_job_tier_grants").fetchone()[0] == 0
    assert events(world, "review_job.tier_approved") == []


def test_unknown_or_foreign_grant_is_not_found(world):
    j = world.submit(job(tier="fable")).json()
    refused = approve(world, j["id"], "agent_authority_grant_missing")
    assert refused.status_code == 409 and refused.json()["reason"] == "tier_grant_not_found"
    other = world.client.post(
        "/projects", headers={**world.owner, "Idempotency-Key": "p2"}, json={"name": "Other"}
    ).json()["id"]
    project = world.project
    world.project = other
    try:
        foreign = tier_grant(world, model="vendor-fable-1")
    finally:
        world.project = project
    refused = approve(world, j["id"], foreign)
    assert refused.status_code == 409 and refused.json()["reason"] == "tier_grant_not_found"


def test_only_an_authorised_person_approves(world):
    j = world.submit(job(tier="fable")).json()
    grant = tier_grant(world, model="vendor-fable-1")
    assert approve(world, j["id"], grant, who=world.submitter).status_code == 403
    assert approve(world, j["id"], grant, who=world.reviewer).status_code == 403


def test_approval_with_a_matching_grant_runs_and_is_recorded(world):
    j = world.submit(job(tier="fable")).json()
    grant = tier_grant(world, model="vendor-fable-1")
    approved = approve(world, j["id"], grant, key="approve-1")
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["state"] == "queued" and body["revision"] == 2
    bound = body["approval"]["tier_grant"]
    assert bound["grant_id"] == grant and bound["tier"] == "fable"
    assert body["approval"]["grant_id"] == grant  # the key a runner reserves model spend with
    assert bound["provider_id"] == "dest-1" and bound["model"] == "vendor-fable-1"
    assert approve(world, j["id"], grant, key="approve-1").json() == body  # idempotent replay
    (event,) = events(world, "review_job.tier_approved")
    assert event["grant_id"] == grant and event["model"] == "vendor-fable-1"
    with sqlite3.connect(world.db) as db:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE review_job_tier_grants SET model='x'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM review_job_tier_grants")
    store, principal = runner(world)
    assert store.mark_running(principal, j["id"], "run")[1]["state"] == "running"


def test_custom_tier_binds_by_configured_model_ids(world):
    world.app.state.review_jobs.tiers = ReviewTiers(
        ("small", "large"), "large", {"large": ("vendor-xl-2",)}
    )
    j = world.submit(job(tier="large")).json()
    token_match = tier_grant(world, model="vendor-large-1")  # a listed tier ignores tokens
    assert approve(world, j["id"], token_match).json()["reason"] == "tier_grant_scope_mismatch"
    listed = tier_grant(world, model="vendor-xl-2")
    assert approve(world, j["id"], listed).status_code == 200
    store, principal = runner(world)
    assert store.mark_running(principal, j["id"], "run")[1]["state"] == "running"


def test_revoked_grant_after_approval_stops_the_start(world):
    j = world.submit(job(tier="fable")).json()
    grant = tier_grant(world, model="vendor-fable-1")
    assert approve(world, j["id"], grant).status_code == 200
    revoked = world.post(
        f"/agent-authority-grants/{grant}/revoke",
        world.owner,
        {"expected_revision": 1, "expected_epoch": 1},
    )
    assert revoked.status_code == 200, revoked.text
    store, principal = runner(world)
    with pytest.raises(Exception, match="tier_grant_not_active"):
        store.mark_running(principal, j["id"], "run")
    assert world.client.get(f"/review-jobs/{j['id']}", headers=world.owner).json()["state"] == (
        "queued"
    )


def test_legacy_approval_without_a_bound_grant_does_not_start(world):
    j = world.submit(job(tier="fable")).json()
    with sqlite3.connect(world.db) as db:  # an approval recorded before grants were required
        db.execute(
            "INSERT INTO review_job_approvals (job_id,org_id,actor_id,rationale,job_digest,"
            "created_at) SELECT id,org_id,actor_id,'legacy',digest,'t' FROM review_jobs"
        )
        db.execute("UPDATE review_jobs SET state='queued',revision=2")
    store, principal = runner(world)
    with pytest.raises(Exception, match="tier_grant_required"):
        store.mark_running(principal, j["id"], "run")


# -- migration ----------------------------------------------------------------------------


def test_migration_is_one_step_after_the_output_license_labels():
    assert REVIEW_TIERS_SCHEMA_VERSION == 91 <= LATEST_SCHEMA_VERSION
    assert MIGRATIONS[REVIEW_TIERS_SCHEMA_VERSION - 1].__name__ == "migrate_review_tiers_schema"


def test_old_database_upgrades_keeping_rows_and_admitting_new_tiers(world):
    j = world.submit(job(tier="fable")).json()
    kept = world.submit(job(tier="sonnet")).json()
    with sqlite3.connect(world.db) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("PRAGMA legacy_alter_table=ON")  # keep child FKs naming review_jobs
        sql = db.execute("SELECT sql FROM sqlite_master WHERE name='review_jobs'").fetchone()[0]
        start = sql.index("tier TEXT NOT NULL")
        end = sql.index("budget_cents INTEGER")
        old_sql = sql[:start] + OLD_TIER_CHECK + ",\n        " + sql[end:]
        for action in ("update", "delete"):
            db.execute(f"DROP TRIGGER review_job_tier_grants_no_{action}")
        db.execute("DROP TABLE review_job_tier_grants")
        db.execute("ALTER TABLE review_jobs RENAME TO review_jobs_old")
        db.execute(old_sql)
        db.execute("INSERT INTO review_jobs SELECT * FROM review_jobs_old")
        db.execute("DROP TABLE review_jobs_old")
        db.execute("CREATE INDEX review_jobs_project ON review_jobs(org_id,project_id,sequence)")
        db.execute(
            "CREATE TRIGGER review_jobs_no_delete BEFORE DELETE ON review_jobs "
            "BEGIN SELECT RAISE(ABORT,'review jobs are permanent'); END"
        )
        db.execute("UPDATE schema_meta SET schema_version=88")
        before = db.execute("SELECT * FROM review_jobs ORDER BY sequence").fetchall()
    assert migrate_state(world.state) == LATEST_SCHEMA_VERSION
    assert migrate_state(world.state) == LATEST_SCHEMA_VERSION  # a second run is a no-op
    fresh = world.state.parent / "fresh"
    initialize_state(fresh)
    objects = "SELECT type,name,sql FROM sqlite_master WHERE name LIKE 'review_job%' ORDER BY name"
    with (
        sqlite3.connect(world.db) as db,
        sqlite3.connect(fresh / "tessera.sqlite3") as ref,
    ):
        assert db.execute("SELECT * FROM review_jobs ORDER BY sequence").fetchall() == before
        assert db.execute(objects).fetchall() == ref.execute(objects).fetchall()
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    # The old rows still work, and a configured tier the old CHECK refused is now admitted.
    got = world.client.get(f"/review-jobs/{kept['id']}", headers=world.owner).json()
    assert got["tier"] == "sonnet" and got["state"] == "queued"
    grant = tier_grant(world, model="vendor-fable-1")
    assert approve(world, j["id"], grant).status_code == 200
    world.app.state.review_jobs.tiers = ReviewTiers(("small", "large"), "large")
    assert world.submit(job(tier="small")).json()["state"] == "queued"
