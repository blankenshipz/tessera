"""Code review jobs: subject, numbered findings + verdict + receipt, runner protocol (ADR 0092).

Synthetic only: no model runs; the runner protocol is driven directly with injected clocks.
Subject snapshots are seeded with real manifests and retained bytes, so custody reads verify.
"""

import hashlib
import json
import sqlite3

import pytest
from test_review_jobs import (  # noqa: F401
    AUTO,
    Policy,
    _enroll_human,
    _seed,
    finding,
    job,
    resolve_body,
    world,
)

from tessera_agent.code_review_child import CODE_REVIEW_PROMPT_SHA256
from tessera_control.code_review_jobs import RUNNER_ACTOR
from tessera_control.review_jobs import ReviewRefused
from tessera_control.review_jobs_models import ModelCallReceipt
from tessera_control.store import Conflict, canonical_digest

# Synthetic subject content: a one-line change and a rubric.
HEAD_FILES = {"src/calc.py": b"def add(a, b):\n    return a + b\n"}
BASE_FILES = {"src/calc.py": b"def add(a, b):\n    return a - b\n"}
RUBRIC_FILES = {"rubric.md": b"Check correctness and tests.\n"}


def manifest(files):
    """A snapshot manifest (ADR 0037 shape) for ``files``."""
    entries = [
        {
            "relative_path": path,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "provenance": "imported_unverified",
            "classification": "unclassified",
        }
        for path, content in sorted(files.items())
    ]
    return {
        "root": "subject",
        "entries": entries,
        "exclusions": [],
        "total_bytes": sum(len(content) for content in files.values()),
    }


def manifest_digest(files):
    canonical = json.dumps(manifest(files), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


H, B, R = manifest_digest(HEAD_FILES), manifest_digest(BASE_FILES), manifest_digest(RUBRIC_FILES)
POLICY = "4" * 64
# Head, base and rubric are existing snapshots in the project's custody (ADR 0084 inputs).
SNAPSHOTS = {H: "snap-head", B: "snap-base", R: "snap-rubric"}
CONTENT = {H: HEAD_FILES, B: BASE_FILES, R: RUBRIC_FILES}
# Code review jobs carry the pinned prompt; a bound receipt names the same prompt digest.
PROMPT = CODE_REVIEW_PROMPT_SHA256


def seed_subject(db, project):
    """Record the head, base and rubric snapshots in ``project`` (unrecorded class) with their
    manifests and retained bytes."""
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        for digest, snapshot_id in SNAPSHOTS.items():
            files = CONTENT[digest]
            conn.execute(
                "INSERT INTO source_snapshots (id,project_id,digest,manifest_json,created_at,"
                "actor_principal_id) VALUES (?,?,?,?,'t',(SELECT id FROM principals LIMIT 1))",
                (snapshot_id, project, digest, json.dumps(manifest(files))),
            )
            conn.executemany(
                "INSERT INTO snapshot_files (snapshot_id,relative_path,content) VALUES (?,?,?)",
                [(snapshot_id, path, content) for path, content in files.items()],
            )


class CRPolicy(Policy):
    """The stub registry, also enrolling the ``anthropic`` and ``openai`` destinations."""

    def check(self, destination_id, tier, data_classes, budget_cents):
        if destination_id in ("anthropic", "openai"):
            destination_id = "dest-1"
        return super().check(destination_id, tier, data_classes, budget_cents)


@pytest.fixture(autouse=True)
def _subject_snapshots(request):
    if "world" in request.fixturenames:
        w = request.getfixturevalue("world")
        seed_subject(w.db, w.project)
        w.app.state.review_policy = CRPolicy()


def inputs(*digests):
    return [{"snapshot_id": SNAPSHOTS[d], "digest": d} for d in digests]


def cr_job(mode="diff", **over):
    subject = {"mode": mode, "head_digest": H, "rubric_digest": R}
    if mode == "diff":
        subject["base_digest"] = B
    body = job(
        kind="code_review",
        title="review change",
        inputs=inputs(H, B, R) if mode == "diff" else inputs(H, R),
        code_review=subject,
        destination_id="anthropic",
        egress="model_runtime",
        prompt_digest=PROMPT,
    )
    return {**body, **over}


def bound_digests(mode="diff"):
    """A receipt's ``input_digests`` for every input of a ``mode`` job."""
    return {SNAPSHOTS[d]: d for d in ((H, B, R) if mode == "diff" else (H, R))}


def receipt(mode="diff", **over):
    """Default: the Anthropic direct Messages API receipt (ADR 0083), every field present and
    bound to the default job (its inputs, most private class and destination)."""
    return {
        "model_configured": "anthropic/claude-sonnet-4-5",
        "model_reported": "claude-sonnet-4-5-20250929",
        "model_identity_basis": "provider_response_model",
        "tier": "sonnet",
        "input_digests": bound_digests(mode),
        "tokens": {"input": 1200, "output": 300, "total": 1500},
        "cost": 0.81,  # cents, from pinned prices
        "cost_basis": "computed_from_pinned_prices",
        "cost_cents": 1,
        "policy_sha256": POLICY,
        "attempts": 1,
        "run_id": "run-code-review-1",
        "seq": 1,
        "input_class": "private_derived",
        "prompt_sha256": PROMPT,
        "profile_sha256": "6" * 64,
        "input_sha256": "7" * 64,
        "output_sha256": "8" * 64,
        "limits": {"max_output_tokens": 4096, "daily_tokens": 200000, "daily_cost_cents": 500},
        "daily_tokens_after": 1500,
        "daily_cost_cents_after": 1,
        "cost_cents_estimate_max": 7,
        "observed_at": 1700000000.5,
        **over,
    }


def openai_included_receipt(mode="diff", **over):
    """ADR 0083 OpenAI included-OAuth receipt: harness-reported model, zero cost. Bound to a job
    whose destination is ``openai``."""
    return {
        "model_configured": "openai/gpt-codex",
        "model_reported": None,
        "model_identity_basis": "harness_reported_unverified",
        "tier": "sonnet",
        "input_digests": bound_digests(mode),
        "input_class": "private_derived",
        "tokens": {"input": 10, "output": 5, "total": 15},
        "cost": 0.0,
        "cost_basis": "included_subscription_usage",
        "cost_cents": None,
        "policy_sha256": POLICY,
        "attempts": 1,
        "prompt_sha256": PROMPT,
        **over,
    }


def normalized(value):
    """The stored form: every receipt field present, absent optional fields null."""
    return ModelCallReceipt.model_validate(value).model_dump(mode="json")


def output(findings=None, verdict="MERGE-WITH-FOLLOWUPS", mode="diff", **over):
    return {
        "findings": [finding("1"), finding("2", severity="major")]
        if findings is None
        else findings,
        "verdict": verdict,
        "receipt": receipt(mode),
        **over,
    }


def count(w, table):
    with sqlite3.connect(w.db) as db:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def events(w, kind):
    with sqlite3.connect(w.db) as db:
        return [
            (json.loads(p), actor)
            for p, actor in db.execute(
                "SELECT payload_json,actor_principal_id FROM events WHERE event_type=? "
                "ORDER BY cursor",
                (kind,),
            )
        ]


# -- submit -----------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["diff", "snapshot"])
def test_submit_code_review_record_shape(world, mode):  # noqa: F811
    r = world.submit(cr_job(mode))
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["kind"] == "code_review" and j["state"] == "queued"
    assert j["advisory"] is True and j["execution_authorized"] is False
    # Data classes are derived server-side from the recorded snapshots, never client labels.
    assert j["data_classes"] == ["private_derived"]
    assert all(item["data_class"] == "private_derived" for item in j["inputs"])
    assert j["code_review"] == {
        "mode": mode,
        "base_digest": B if mode == "diff" else None,
        "head_digest": H,
        "rubric_digest": R,
        "verdict": None,
        "attempts": 0,
        "receipt": None,
        "review_receipt_digest": None,
        "attempt_receipts": [],
        "cost_cents_total": 0,
    }
    # Other kinds keep a null subject and their ADR 0084 digest (no code_review key bound).
    other = world.submit().json()
    assert other["code_review"] is None
    assert count(world, "code_review_jobs") == 1


@pytest.mark.parametrize(
    "body",
    [
        job(kind="code_review"),  # missing subject
        job(code_review={"mode": "snapshot", "head_digest": H, "rubric_digest": R}),  # forbidden
        cr_job("diff", code_review={"mode": "diff", "head_digest": H, "rubric_digest": R}),
        cr_job(
            "snapshot",
            code_review={
                "mode": "snapshot",
                "head_digest": H,
                "base_digest": B,
                "rubric_digest": R,
            },
        ),
        cr_job(
            "diff",
            code_review={"mode": "diff", "head_digest": H, "base_digest": H, "rubric_digest": R},
        ),
        cr_job("diff", inputs=inputs(H, R)),  # base not bound to an input
        cr_job("snapshot", inputs=inputs(H)),  # rubric not bound
        cr_job("snapshot", inputs=inputs(R)),  # head not bound
        cr_job(
            "snapshot",
            code_review={"mode": "snapshot", "head_digest": H, "rubric_digest": R, "x": 1},
        ),
        cr_job("snapshot", code_review={"mode": "tree", "head_digest": H, "rubric_digest": R}),
    ],
)
def test_submit_code_review_validation_is_422(world, body):  # noqa: F811
    assert world.submit(body).status_code == 422
    assert count(world, "review_jobs") == 0 and count(world, "code_review_jobs") == 0


def test_client_data_class_is_refused(world):  # noqa: F811
    body = cr_job("snapshot")
    body["inputs"][0]["data_class"] = "public"
    assert world.submit(body).status_code == 422
    assert count(world, "review_jobs") == 0


def test_subject_classes_derived_from_custody(world):  # noqa: F811
    with sqlite3.connect(world.db) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(
            "INSERT INTO snapshot_data_classes (snapshot_id,data_class,origin,license,source,"
            "actor_principal_id,decision_class,recorded_at) VALUES "
            "(?,'public','human_declared','MIT','ref',(SELECT id FROM principals LIMIT 1),"
            "'human_decision','t')",
            (SNAPSHOTS[R],),
        )
    seen = []

    class Spy:
        def check(self, destination_id, tier, data_classes, budget_cents):
            seen.append(list(data_classes))

    world.app.state.review_policy = Spy()
    j = world.submit(cr_job("snapshot")).json()
    assert seen == [["private_derived", "public"]]
    assert j["data_classes"] == ["private_derived", "public"]
    assert {i["snapshot_id"]: i["data_class"] for i in j["inputs"]} == {
        SNAPSHOTS[H]: "private_derived",
        SNAPSHOTS[R]: "public",
    }


@pytest.mark.parametrize("role", ["head", "base"])
def test_subject_digest_not_in_custody_refused(world, role):  # noqa: F811
    """Head and base digests come from existing artifact custody; an invented one is refused."""
    digest = H if role == "head" else B
    body = cr_job()
    for item in body["inputs"]:
        if item["digest"] == digest:
            item["snapshot_id"] = "snap-invented"
    r = world.submit(body)
    assert r.status_code == 409 and r.json()["reason"] == "input_not_found"
    # A known snapshot named with a digest it does not hold is a mismatch, not a new artifact.
    other = "9" * 64
    body = cr_job()
    body["code_review"][f"{role}_digest"] = other
    for item in body["inputs"]:
        if item["digest"] == digest:
            item["digest"] = other
    r = world.submit(body)
    assert r.status_code == 409 and r.json()["reason"] == "input_digest_mismatch"
    assert count(world, "review_jobs") == 0 and count(world, "code_review_jobs") == 0
    assert events(world, "review_job.submitted") == []


def test_subject_snapshot_from_another_project_refused(world):  # noqa: F811
    second = world.client.post(
        "/projects", headers={**world.owner, "Idempotency-Key": "p2"}, json={"name": "R2"}
    ).json()["id"]
    head = "8" * 64
    _seed(world.db, second, "snap-foreign", head)
    body = cr_job("snapshot")
    body["code_review"]["head_digest"] = head
    body["inputs"] = [{"snapshot_id": "snap-foreign", "digest": head}, *inputs(R)]
    r = world.submit(body)
    assert r.status_code == 409 and r.json()["reason"] == "input_not_found"
    assert count(world, "review_jobs") == 0 and count(world, "code_review_jobs") == 0


def test_sealed_head_digest_refused(world):  # noqa: F811
    with sqlite3.connect(world.db) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(
            "INSERT INTO campaign_holdout_claims (id,org_id,project_id,partition_digest,"
            "claim_scope,campaign_id,stage_key,input_name,approval_id,spec_digest,status,opened_at)"
            " SELECT 'c1',org_id,id,?,'s','x','k','n','a','d','opened','t' FROM projects",
            (H,),
        )
    r = world.submit(cr_job("snapshot"))
    assert r.status_code == 409 and r.json()["reason"] == "input_sealed"
    assert count(world, "code_review_jobs") == 0


def test_list_kind_filter(world):  # noqa: F811
    world.submit()
    cr = world.submit(cr_job()).json()
    base = f"/projects/{world.project}/review-jobs"
    got = world.client.get(f"{base}?kind=code_review", headers=world.submitter).json()["jobs"]
    assert [x["id"] for x in got] == [cr["id"]]
    got = world.client.get(f"{base}?kind=plan_review", headers=world.submitter).json()["jobs"]
    assert len(got) == 1 and got[0]["kind"] == "plan_review"
    assert len(world.client.get(base, headers=world.submitter).json()["jobs"]) == 2
    assert world.client.get(f"{base}?kind=other", headers=world.submitter).status_code == 422


# -- ingest -----------------------------------------------------------------------------


def ingest(w, jid, body):
    return w.post(f"/review-jobs/{jid}/findings", w.reviewer, body)


def test_ingest_completes_with_verdict_and_receipts(world):  # noqa: F811
    j = world.submit(cr_job()).json()
    body = output()
    body["receipt_digest"] = canonical_digest(body["receipt"])
    r = ingest(world, j["id"], body)
    assert r.status_code == 201, r.text
    done = r.json()["job"]
    cr = done["code_review"]
    assert done["state"] == "completed" and cr["verdict"] == "MERGE-WITH-FOLLOWUPS"
    assert done["advisory"] is True and done["execution_authorized"] is False
    assert cr["receipt"] == receipt() and done["receipt_digest"] == canonical_digest(receipt())
    findings = [finding("1"), finding("2", severity="major")]
    stored = [f["finding"] for f in r.json()["findings"]]
    review_receipt = {
        "job_digest": j["digest"],
        "mode": "diff",
        "base_digest": B,
        "head_digest": H,
        "rubric_digest": R,
        "verdict": "MERGE-WITH-FOLLOWUPS",
        "findings_digest": canonical_digest({"findings": stored}),
        "model_call": receipt(),
    }
    assert [f["id"] for f in stored] == ["1", "2"] and len(stored) == len(findings)
    assert cr["review_receipt_digest"] == canonical_digest(review_receipt)
    fetched = world.client.get(f"/review-jobs/{j['id']}", headers=world.submitter).json()
    assert fetched["code_review"] == cr
    ((payload, _),) = events(world, "review_job.completed")
    assert payload["verdict"] == "MERGE-WITH-FOLLOWUPS"
    assert payload["review_receipt_digest"] == cr["review_receipt_digest"]
    # The completing attempt's receipt is recorded with its cost.
    assert cr["attempt_receipts"] == [
        {
            "attempt": 1,
            "outcome": "completed",
            "reason": None,
            "receipt": receipt(),
            "receipt_digest": canonical_digest(receipt()),
            "bound": True,
            "receipt_invalid": False,
        }
    ]
    assert cr["cost_cents_total"] == 1


def test_ingest_zero_findings_merge(world):  # noqa: F811
    j = world.submit(cr_job("snapshot")).json()
    r = ingest(world, j["id"], output(findings=[], verdict="MERGE", mode="snapshot"))
    assert r.status_code == 201 and r.json()["job"]["code_review"]["verdict"] == "MERGE"


@pytest.mark.parametrize(
    "body",
    [
        output(findings=[finding("2")]),  # not numbered from 1
        output(findings=[finding("1"), finding("3")]),
        output(findings=[finding("a")]),
        {k: v for k, v in output().items() if k != "verdict"},
        output(verdict="LGTM"),
        output(findings=[finding("1", severity="blocking")], verdict="MERGE-WITH-FOLLOWUPS"),
        output(findings=[finding("1", severity="major")], verdict="MERGE"),
        {k: v for k, v in output().items() if k != "receipt"},
        output(receipt=receipt(tier="haiku")),  # silent downshift
        output(receipt=receipt(prompt="leak")),  # unknown key
        output(receipt=receipt(model_identity_basis="verified")),
        output(receipt=receipt(tokens={"bogus": 1})),
        output(receipt=receipt(attempts=3)),
        output(receipt_digest="e" * 64),
        output(receipt=receipt(cost_basis="reported_cost", cost_cents=101)),  # budget is 100
        output(receipt=receipt(cost_basis="reported_cost", cost_cents=None)),
        output(receipt=receipt(cost_cents=101, cost=100.5)),  # pinned price over budget
        output(receipt=receipt(cost_cents=None)),
        output(receipt=receipt(cost=1.2, cost_cents=1)),  # understates ceil(cost) = 2
        output(receipt=openai_included_receipt(cost=0.5)),  # included usage is zero cost
        output(receipt=receipt(input_class="public")),  # inputs are private_derived
        output(receipt=receipt(input_class="secret")),
        output(receipt=receipt(completion="leak")),
        output(receipt=receipt(api_key_ref="ref")),
        output(receipt=receipt(seq=0)),
        output(receipt=receipt(daily_cost_cents_after=-1)),
        output(receipt=receipt(observed_at=-1.0)),
        output(receipt=receipt(prompt_sha256="not-a-digest")),
        output(receipt=receipt(limits={"x" * 65: 1})),
        output(receipt=receipt(limits={str(n): n for n in range(17)})),
        output(receipt=receipt(limits={"daily_tokens": -1})),
        output(receipt=receipt(limits={"daily_tokens": "1"})),
        output(receipt=receipt(run_id=" ")),
    ],
)
def test_code_review_ingest_refusals_store_nothing(world, body):  # noqa: F811
    j = world.submit(cr_job()).json()
    r = ingest(world, j["id"], body)
    assert r.status_code == 422 and r.json()["reason"] == "failed_output_invalid", r.text
    assert count(world, "review_findings") == 0
    after = world.client.get(f"/review-jobs/{j['id']}", headers=world.submitter).json()
    assert after["state"] == "queued" and after["code_review"]["verdict"] is None
    assert after["receipt_digest"] is None and events(world, "review_job.completed") == []


def test_reported_cost_within_budget_and_blocking_changes_required(world):  # noqa: F811
    # Reported cost is not an anthropic basis; another destination may use it.
    j = world.submit(cr_job(destination_id="openai")).json()
    body = output(
        findings=[finding("1", severity="blocking")],
        verdict="CHANGES-REQUIRED",
        receipt=receipt(
            model_configured="openai/gpt-x",
            cost_basis="reported_cost",
            cost_cents=100,
            cost=1.0,
        ),
    )
    assert ingest(world, j["id"], body).status_code == 201


def test_openai_included_receipt_completes_normalized(world):  # noqa: F811
    j = world.submit(cr_job(destination_id="openai")).json()
    sent = openai_included_receipt()
    r = ingest(world, j["id"], output(receipt=sent))
    assert r.status_code == 201, r.text
    done = r.json()["job"]
    stored = done["code_review"]["receipt"]
    assert stored == normalized(sent) and stored["run_id"] is None and stored["limits"] is None
    assert done["receipt_digest"] == canonical_digest(normalized(sent))
    assert done["receipt_digest"] != canonical_digest(sent)


def test_pinned_price_cost_cents_rounds_up_and_minimal_fields(world):  # noqa: F811
    j = world.submit(cr_job()).json()
    # Required model fields plus input_class and prompt_sha256, required for a code review.
    sent = {
        k: v
        for k, v in receipt(cost=1.2, cost_cents=2).items()
        if k in ModelCallReceipt.model_fields and ModelCallReceipt.model_fields[k].is_required()
    }
    sent["cost_cents"] = 2
    sent["input_class"] = "private_derived"
    sent["prompt_sha256"] = PROMPT
    r = ingest(world, j["id"], output(receipt=sent))
    assert r.status_code == 201, r.text
    assert r.json()["job"]["code_review"]["receipt"] == normalized(sent)


@pytest.mark.parametrize(
    "over",
    [{"observed_at": float("inf")}, {"observed_at": float("nan")}, {"limits": {"a": float("inf")}}],
)
def test_receipt_non_finite_values_refused(over):
    with pytest.raises(ValueError):
        ModelCallReceipt.model_validate(receipt(**over))


@pytest.mark.parametrize("extra", [{"verdict": "MERGE"}, {"receipt": receipt()}])
def test_other_kinds_refuse_verdict_and_receipt(world, extra):  # noqa: F811
    j = world.submit().json()
    r = ingest(world, j["id"], {"findings": [], **extra})
    assert r.status_code == 422 and r.json()["reason"] == "failed_output_invalid"
    assert count(world, "review_findings") == 0


# -- runner protocol --------------------------------------------------------------------


def test_claim_respects_bound_oldest_first_and_attempts(world):  # noqa: F811
    store = world.app.state.review_jobs
    first = world.submit(cr_job()).json()["id"]
    second = world.submit(cr_job("snapshot")).json()["id"]
    world.submit()  # other kinds are never claimed
    for bad in (0, 9, True, "2"):
        with pytest.raises(ValueError):
            store.claim_code_review("r1", bad, 60, 1000.0)
    a = store.claim_code_review("r1", 1, 60, 1000.0)
    assert a["id"] == first and a["state"] == "running"
    assert a["code_review"]["attempts"] == 1 and a["inputs"] and a["prompt_digest"]
    assert store.claim_code_review("r2", 1, 60, 1000.0) is None  # bound reached
    # Once r2 (limit 1) is no longer seen within the lease window, r1's raised limit applies.
    b = store.claim_code_review("r1", 2, 60, 1100.0)
    assert b["id"] == second
    assert store.claim_code_review("r3", 5, 60, 1100.0) is None  # host bound (2) reached
    (payload, actor), _ = events(world, "review_job.running")
    assert payload == {"job_id": first, "actor": RUNNER_ACTOR, "attempt": 1}


def test_claim_bound_counts_running_across_orgs(world):  # noqa: F811
    store = world.app.state.review_jobs
    jid = world.submit(cr_job()).json()["id"]
    world.submit(cr_job("snapshot"))
    store.claim_code_review("r1", 5, 60, 1000.0)
    # Move the running job to a different org: the host bound must still count it.
    with sqlite3.connect(world.db) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("UPDATE review_jobs SET org_id='org-elsewhere' WHERE id=?", (jid,))
    assert store.claim_code_review("r2", 1, 60, 1000.0) is None
    assert store.claim_code_review("r2", 2, 60, 1000.0) is not None


def test_lease_renew_and_owner_checks(world):  # noqa: F811
    store = world.app.state.review_jobs
    jid = world.submit(cr_job()).json()["id"]
    store.claim_code_review("r1", 2, 60, 1000.0)
    assert store.renew_code_review_lease(jid, "r1", 60, 1050.0) is True
    assert store.renew_code_review_lease(jid, "r2", 60, 1050.0) is False
    assert store.reap_expired_code_review_leases(1100.0) == []  # renewed to 1110
    for call in (
        lambda: store.complete_code_review(jid, "r2", output(), 1.0),
        lambda: store.fail_code_review(jid, "r2", "model_failed", 1.0),
        lambda: store.requeue_vanished_code_review(jid, "r2", 1.0),
        lambda: store.not_start_code_review(jid, "r2", "tier_exceeds_policy", 1.0),
    ):
        with pytest.raises(Conflict):
            call()
    with pytest.raises(ValueError):
        store.fail_code_review(jid, "r1", "free text", 1.0)
    store.fail_code_review(jid, "r1", "model_failed", 1.0)
    assert store.renew_code_review_lease(jid, "r1", 60, 1050.0) is False
    with pytest.raises(Conflict):
        store.fail_code_review(jid, "r1", "model_failed", 1.0)


def test_vanished_requeues_once_then_fails(world):  # noqa: F811
    store = world.app.state.review_jobs
    jid = world.submit(cr_job()).json()["id"]
    store.claim_code_review("r1", 2, 60, 1000.0)
    assert store.requeue_vanished_code_review(jid, "r1", 1001.0) == "queued"
    again = store.claim_code_review("r2", 2, 60, 1002.0)
    assert again["id"] == jid and again["code_review"]["attempts"] == 2
    assert store.requeue_vanished_code_review(jid, "r2", 1003.0) == "failed"
    j = world.client.get(f"/review-jobs/{jid}", headers=world.submitter).json()
    assert j["state"] == "failed" and j["reason"] == "model_child_vanished"
    assert store.claim_code_review("r3", 2, 60, 1004.0) is None
    ((retry, _),) = events(world, "review_job.retry_scheduled")
    assert retry["reason"] == "model_child_vanished" and retry["attempt"] == 1
    ((failed, _),) = events(world, "review_job.failed")
    assert failed["reason"] == "model_child_vanished" and failed["actor"] == RUNNER_ACTOR
    # Each attempt is recorded, without a receipt when the child reported none.
    assert [
        (e["attempt"], e["outcome"], e["receipt"]) for e in j["code_review"]["attempt_receipts"]
    ] == [(1, "vanished", None), (2, "vanished", None)]


def test_reap_expired_leases(world):  # noqa: F811
    store = world.app.state.review_jobs
    jid = world.submit(cr_job()).json()["id"]
    store.claim_code_review("r1", 2, 60, 1000.0)
    assert store.reap_expired_code_review_leases(1060.0) == []
    assert store.reap_expired_code_review_leases(1061.0) == [jid]
    assert world.client.get(f"/review-jobs/{jid}", headers=world.submitter).json()["state"] == (
        "queued"
    )
    store.claim_code_review("r1", 2, 60, 2000.0)
    assert store.reap_expired_code_review_leases(3000.0) == [jid]
    j = world.client.get(f"/review-jobs/{jid}", headers=world.submitter).json()
    assert j["state"] == "failed" and j["reason"] == "model_child_vanished"


def test_complete_fail_not_start_transitions(world):  # noqa: F811
    store = world.app.state.review_jobs
    a = world.submit(cr_job()).json()["id"]
    b = world.submit(cr_job()).json()["id"]
    c = world.submit(cr_job()).json()["id"]
    store.claim_code_review("r", 3, 60, 1.0)
    store.claim_code_review("r", 3, 60, 1.0)
    store.claim_code_review("r", 3, 60, 1.0)
    # invalid output: nothing stored, still running, detail flags the budget cause
    with pytest.raises(ReviewRefused) as refused:
        store.complete_code_review(a, "r", output(receipt=receipt(cost=998.2, cost_cents=999)), 2.0)
    assert refused.value.status == 422 and refused.value.reason == "failed_output_invalid"
    assert refused.value.detail == "budget_exceeded"
    assert count(world, "review_findings") == 0
    with pytest.raises(ReviewRefused):
        store.complete_code_review(a, "r", output(verdict="MERGE"), 2.0)
    status, done = store.complete_code_review(a, "r", output(), 2.0)
    assert status == 201 and done["job"]["state"] == "completed"
    assert done["job"]["code_review"]["verdict"] == "MERGE-WITH-FOLLOWUPS"
    assert [f["finding"]["id"] for f in done["findings"]] == ["1", "2"]
    ((completed, _),) = events(world, "review_job.completed")
    assert completed["actor"] == RUNNER_ACTOR and completed["verdict"]
    failed = store.fail_code_review(b, "r", "budget_exceeded", 3.0)
    assert failed["state"] == "failed" and failed["reason"] == "budget_exceeded"
    ns = store.not_start_code_review(c, "r", "policy_check_failed", 3.0)
    assert ns["state"] == "not_started" and ns["reason"] == "policy_check_failed"
    ((ev, _),) = events(world, "review_job.not_started")
    assert ev == {"job_id": c, "actor": RUNNER_ACTOR, "reason": "policy_check_failed"}
    with pytest.raises(ValueError):
        store.not_start_code_review(c, "r", "made_up", 3.0)
    with sqlite3.connect(world.db) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM code_review_jobs WHERE lease_owner IS NOT NULL"
        ).fetchone() == (0,)
        with pytest.raises(sqlite3.IntegrityError, match="permanent"):
            db.execute("DELETE FROM code_review_jobs")


# -- separation of duties ---------------------------------------------------------------


def findings_actor(w, jid):
    with sqlite3.connect(w.db) as db:
        return db.execute(
            "SELECT findings_actor_id FROM review_jobs WHERE id=?", (jid,)
        ).fetchone()[0]


def test_http_reviewer_recorded_and_cannot_resolve(world):  # noqa: F811
    jid = world.submit(cr_job()).json()["id"]
    r = ingest(world, jid, output())
    assert r.status_code == 201, r.text
    ((_, reviewer_principal),) = events(world, "review_job.completed")
    submitted = world.client.get(f"/review-jobs/{jid}", headers=world.submitter).json()
    assert reviewer_principal != submitted["submitter_principal_id"]
    assert findings_actor(world, jid) == reviewer_principal
    f = r.json()["findings"][0]
    path = f"/review-findings/{f['id']}/resolve"
    assert world.post(path, world.reviewer, resolve_body(f)).status_code == 403
    assert world.post(path, world.submitter, resolve_body(f)).status_code == 403
    resolved = world.post(path, world.resolver, resolve_body(f))
    assert resolved.status_code == 201 and resolved.json()["state"] == "accepted"


def test_runner_completion_records_no_findings_actor(world):  # noqa: F811
    human = _enroll_human(world)
    jid = world.submit(cr_job(), who=human).json()["id"]
    store = world.app.state.review_jobs
    assert store.claim_code_review("r1", 2, 60, 1000.0)["id"] == jid
    _, done = store.complete_code_review(jid, "r1", output(), 1001.0)
    assert done["job"]["state"] == "completed"
    # The system runner is not a principal: nothing is recorded as the findings supplier.
    assert findings_actor(world, jid) is None
    f = done["findings"][0]
    path = f"/review-findings/{f['id']}/resolve"
    # The submitting person still cannot resolve; a different person can.
    assert world.post(path, {**human, **AUTO}, resolve_body(f)).status_code == 403
    resolved = world.post(path, world.resolver, resolve_body(f))
    assert resolved.status_code == 201 and resolved.json()["state"] == "accepted"


def test_receipt_accepts_fractional_daily_and_estimate_cents():
    # The direct-API producer sums fractional cents (ADR 0083), so these may be floats.
    from tessera_control.review_jobs_models import ModelCallReceipt

    parsed = ModelCallReceipt.model_validate(
        receipt(daily_cost_cents_after=12.37, cost_cents_estimate_max=3.5)
    )
    assert parsed.daily_cost_cents_after == 12.37 and parsed.cost_cents_estimate_max == 3.5
    for bad in (-0.1, float("inf"), "1"):
        with pytest.raises(ValueError):
            ModelCallReceipt.model_validate(receipt(daily_cost_cents_after=bad))


# -- receipt binding (ADR 0092 review fixes) ----------------------------------------------

# Each receipt is the default bound receipt with exactly one binding rule broken.
UNBOUND = {
    "foreign_input_digest": receipt(input_digests={**bound_digests(), "snap-other": "9" * 64}),
    "missing_input_digest": receipt(input_digests={SNAPSHOTS[H]: H, SNAPSHOTS[R]: R}),
    # The right digests under made-up snapshot ids.
    "renamed_snapshot_ids": receipt(input_digests={"made-up-1": H, "made-up-2": B, "made-up-3": R}),
    "swapped_snapshot_ids": receipt(
        input_digests={SNAPSHOTS[H]: B, SNAPSHOTS[B]: H, SNAPSHOTS[R]: R}
    ),
    "missing_prompt_digest": receipt(prompt_sha256=None),
    "other_prompt_digest": receipt(prompt_sha256="5" * 64),
    "missing_input_class": receipt(input_class=None),
    "weaker_input_class": receipt(input_class="private_code_approved"),
    "public_input_class": receipt(input_class="public"),
    "provider_not_destination": receipt(model_configured="openai/claude-sonnet-4-5"),
    "no_provider_prefix": receipt(model_configured="claude-sonnet-4-5"),
    "anthropic_included_usage": receipt(
        cost_basis="included_subscription_usage", cost=0.0, cost_cents=None
    ),
    "anthropic_harness_reported": receipt(model_identity_basis="harness_reported_unverified"),
}


def attempts_of(w, jid):
    return w.client.get(f"/review-jobs/{jid}", headers=w.submitter).json()["code_review"]


@pytest.mark.parametrize("case", sorted(UNBOUND))
def test_unbound_receipt_refused_on_http_ingest(world, case):  # noqa: F811
    j = world.submit(cr_job()).json()
    r = ingest(world, j["id"], output(receipt=UNBOUND[case]))
    assert r.status_code == 422 and r.json()["reason"] == "failed_output_invalid", r.text
    assert count(world, "review_findings") == 0
    cr = attempts_of(world, j["id"])
    assert cr["verdict"] is None and cr["attempt_receipts"] == []


@pytest.mark.parametrize("case", sorted(UNBOUND))
def test_unbound_receipt_refused_on_runner_completion(world, case):  # noqa: F811
    store = world.app.state.review_jobs
    jid = world.submit(cr_job()).json()["id"]
    store.claim_code_review("r1", 2, 60, 1000.0)
    with pytest.raises(ReviewRefused) as refused:
        store.complete_code_review(jid, "r1", output(receipt=UNBOUND[case]), 1001.0)
    assert refused.value.status == 422 and refused.value.reason == "failed_output_invalid"
    assert count(world, "review_findings") == 0
    assert attempts_of(world, jid)["attempt_receipts"] == []
    # The runner then fails the job; the receipt is kept but marked unbound.
    failed = store.fail_code_review(
        jid, "r1", "failed_output_invalid", 1002.0, receipt=UNBOUND[case]
    )
    (entry,) = failed["code_review"]["attempt_receipts"]
    assert entry["receipt"] == normalized(UNBOUND[case]) and entry["bound"] is False


def test_bound_receipt_on_anthropic_snapshot_job(world):  # noqa: F811
    # A snapshot job has no base: the receipt names exactly head and rubric.
    j = world.submit(cr_job("snapshot")).json()
    assert ingest(world, j["id"], output(mode="snapshot")).status_code == 201
    k = world.submit(cr_job("snapshot")).json()
    assert ingest(world, k["id"], output(mode="diff")).status_code == 422


def test_budget_exceeded_failure_exposes_receipt(world):  # noqa: F811
    store = world.app.state.review_jobs
    jid = world.submit(cr_job()).json()["id"]
    store.claim_code_review("r1", 2, 60, 1000.0)
    spent = receipt(cost=149.3, cost_cents=150)
    with pytest.raises(ReviewRefused) as refused:
        store.complete_code_review(jid, "r1", output(receipt=spent), 1001.0)
    assert refused.value.detail == "budget_exceeded"
    failed = store.fail_code_review(jid, "r1", "budget_exceeded", 1002.0, receipt=spent)
    digest = canonical_digest(normalized(spent))
    assert failed["state"] == "failed" and failed["reason"] == "budget_exceeded"
    assert failed["receipt_digest"] == digest
    cr = attempts_of(world, jid)
    assert cr["receipt"] is None and cr["verdict"] is None  # no completion receipt invented
    assert cr["attempt_receipts"] == [
        {
            "attempt": 1,
            "outcome": "failed",
            "reason": "budget_exceeded",
            "receipt": normalized(spent),
            "receipt_digest": digest,
            "bound": True,
            "receipt_invalid": False,
        }
    ]
    assert cr["cost_cents_total"] == 150
    ((event, _),) = events(world, "review_job.failed")
    assert event["reason"] == "budget_exceeded" and event["receipt_digest"] == digest


def test_vanished_with_receipt_then_completed_records_both(world):  # noqa: F811
    store = world.app.state.review_jobs
    jid = world.submit(cr_job()).json()["id"]
    store.claim_code_review("r1", 2, 60, 1000.0)
    first = receipt(cost=2.5, cost_cents=3)
    assert store.requeue_vanished_code_review(jid, "r1", 1001.0, receipt=first) == "queued"
    ((retry, _),) = events(world, "review_job.retry_scheduled")
    assert retry["receipt_digest"] == canonical_digest(normalized(first))
    store.claim_code_review("r1", 2, 60, 1002.0)
    second = receipt(attempts=2)
    _, done = store.complete_code_review(jid, "r1", output(receipt=second), 1003.0)
    cr = done["job"]["code_review"]
    assert [(e["attempt"], e["outcome"]) for e in cr["attempt_receipts"]] == [
        (1, "vanished"),
        (2, "completed"),
    ]
    assert [e["receipt"] for e in cr["attempt_receipts"]] == [
        normalized(first),
        normalized(second),
    ]
    assert cr["cost_cents_total"] == 3 + 1
    assert cr["receipt"] == normalized(second)


def test_invalid_failure_receipt_is_flagged_not_stored(world):  # noqa: F811
    store = world.app.state.review_jobs
    jid = world.submit(cr_job()).json()["id"]
    store.claim_code_review("r1", 2, 60, 1000.0)
    failed = store.fail_code_review(
        jid, "r1", "model_failed", 1001.0, receipt=receipt(prompt="leak")
    )
    assert failed["state"] == "failed" and failed["reason"] == "model_failed"
    assert failed["receipt_digest"] is None
    assert failed["code_review"]["attempt_receipts"] == [
        {
            "attempt": 1,
            "outcome": "failed",
            "reason": "model_failed",
            "receipt": None,
            "receipt_digest": None,
            "bound": False,
            "receipt_invalid": True,
        }
    ]
    assert failed["code_review"]["cost_cents_total"] == 0
    ((event, _),) = events(world, "review_job.failed")
    assert "receipt_digest" not in event
    with sqlite3.connect(world.db) as db:
        stored = db.execute("SELECT attempt_receipts_json FROM code_review_jobs").fetchone()[0]
    assert "leak" not in stored


@pytest.mark.parametrize("reason", ["input_too_large", "input_unavailable", "prompt_not_pinned"])
def test_new_fail_reasons(world, reason):  # noqa: F811
    store = world.app.state.review_jobs
    jid = world.submit(cr_job()).json()["id"]
    store.claim_code_review("r1", 2, 60, 1000.0)
    assert store.fail_code_review(jid, "r1", reason, 1001.0)["reason"] == reason


@pytest.mark.parametrize(
    "reason", ["prompt_not_pinned", "egress_not_permitted", "destination_not_supported"]
)
def test_new_not_start_reasons(world, reason):  # noqa: F811
    store = world.app.state.review_jobs
    jid = world.submit(cr_job()).json()["id"]
    store.claim_code_review("r1", 2, 60, 1000.0)
    assert store.not_start_code_review(jid, "r1", reason, 1001.0)["reason"] == reason


# -- host-wide bound and lease validity ---------------------------------------------------


def test_host_bound_is_minimum_of_live_runners(world):  # noqa: F811
    store = world.app.state.review_jobs
    # A runner last seen long ago with limit 1 no longer counts.
    assert store.claim_code_review("r-stale", 1, 60, 100.0) is None
    jobs = [world.submit(cr_job()).json()["id"] for _ in range(3)]
    assert store.claim_code_review("r-small", 2, 60, 1000.0)["id"] == jobs[0]
    assert store.claim_code_review("r-big", 8, 60, 1000.0)["id"] == jobs[1]
    # r-big's own limit of 8 does not override r-small's 2 across the host.
    assert store.claim_code_review("r-big", 8, 60, 1001.0) is None
    with sqlite3.connect(world.db) as db:
        rows = dict(db.execute("SELECT runner_id,max_running FROM code_review_runners"))
    assert rows == {"r-stale": 1, "r-small": 2, "r-big": 8}
    # When r-small stops being seen, r-big's limit applies.
    assert store.claim_code_review("r-big", 8, 60, 1061.0)["id"] == jobs[2]


def test_renew_keeps_busy_runner_in_host_bound(world):  # noqa: F811
    store = world.app.state.review_jobs
    jobs = [world.submit(cr_job()).json()["id"] for _ in range(3)]
    store.claim_code_review("r-small", 2, 60, 1000.0)
    store.claim_code_review("r-big", 8, 60, 1000.0)
    assert store.renew_code_review_lease(jobs[0], "r-small", 60, 1050.0) is True
    assert store.claim_code_review("r-big", 8, 60, 1100.0) is None  # r-small seen at 1050


def test_expired_unreaped_lease_is_conflict(world):  # noqa: F811
    store = world.app.state.review_jobs
    jid = world.submit(cr_job()).json()["id"]
    store.claim_code_review("r1", 2, 60, 1000.0)  # lease to 1060
    assert store.code_review_inputs(jid, "r1", 1060.0)  # still valid at expiry
    late = 1061.0
    for call in (
        # Without a receipt there is nothing to record.
        lambda: store.fail_code_review(jid, "r1", "model_failed", late),
        lambda: store.code_review_inputs(jid, "r1", late),
    ):
        with pytest.raises(Conflict):
            call()
    assert store.renew_code_review_lease(jid, "r1", 60, late) is False
    cr = attempts_of(world, jid)
    assert cr["verdict"] is None and cr["attempt_receipts"] == []
    assert events(world, "review_job.late_receipt") == []
    assert store.reap_expired_code_review_leases(late) == [jid]


def job_state(w, jid):
    j = w.client.get(f"/review-jobs/{jid}", headers=w.submitter).json()
    return j["state"], j["reason"], j["revision"]


def test_late_fail_on_expired_lease_records_receipt(world):  # noqa: F811
    store = world.app.state.review_jobs
    jid = world.submit(cr_job()).json()["id"]
    store.claim_code_review("r1", 2, 60, 1000.0)  # lease to 1060
    before = job_state(world, jid)
    spent = receipt(cost=40.2, cost_cents=41)
    with pytest.raises(Conflict):
        store.fail_code_review(jid, "r1", "budget_exceeded", 1061.0, receipt=spent)
    assert job_state(world, jid) == before == ("running", None, before[2])
    cr = attempts_of(world, jid)
    digest = canonical_digest(normalized(spent))
    assert cr["verdict"] is None and cr["receipt"] is None
    assert cr["attempt_receipts"] == [
        {
            "attempt": 1,
            "outcome": "late",
            "reason": "budget_exceeded",
            "receipt": normalized(spent),
            "receipt_digest": digest,
            "bound": True,
            "receipt_invalid": False,
        }
    ]
    assert cr["cost_cents_total"] == 41
    ((ev, actor),) = events(world, "review_job.late_receipt")
    assert ev == {
        "job_id": jid,
        "actor": RUNNER_ACTOR,
        "attempt": 1,
        "reason": "budget_exceeded",
        "receipt_digest": digest,
    }
    # A second late report for the same attempt is refused with nothing stored.
    with pytest.raises(Conflict):
        store.fail_code_review(jid, "r1", "model_failed", 1062.0, receipt=receipt())
    assert len(attempts_of(world, jid)["attempt_receipts"]) == 1
    # The reaper still owns the job: it records the vanished attempt and requeues it.
    assert store.reap_expired_code_review_leases(1062.0) == [jid]
    cr = attempts_of(world, jid)
    assert [(e["attempt"], e["outcome"]) for e in cr["attempt_receipts"]] == [
        (1, "late"),
        (1, "vanished"),
    ]
    assert cr["cost_cents_total"] == 41


def test_late_complete_on_expired_lease_records_receipt_only(world):  # noqa: F811
    store = world.app.state.review_jobs
    jid = world.submit(cr_job()).json()["id"]
    store.claim_code_review("r1", 2, 60, 1000.0)
    before = job_state(world, jid)
    with pytest.raises(Conflict):
        store.complete_code_review(jid, "r1", output(), 1061.0)
    assert job_state(world, jid) == before
    assert count(world, "review_findings") == 0
    cr = attempts_of(world, jid)
    assert cr["verdict"] is None and cr["receipt"] is None
    assert cr["review_receipt_digest"] is None
    (entry,) = cr["attempt_receipts"]
    assert entry["outcome"] == "late" and entry["reason"] == "completed_after_lease"
    assert entry["receipt"] == receipt() and entry["bound"] is True
    assert cr["cost_cents_total"] == 1
    assert events(world, "review_job.completed") == []
    assert len(events(world, "review_job.late_receipt")) == 1


def test_late_receipt_after_reaper_requeued_is_recorded(world):  # noqa: F811
    store = world.app.state.review_jobs
    jid = world.submit(cr_job()).json()["id"]
    store.claim_code_review("r1", 2, 60, 1000.0)
    assert store.reap_expired_code_review_leases(1061.0) == [jid]
    again = store.claim_code_review("r2", 2, 60, 1070.0)
    assert again["id"] == jid and again["code_review"]["attempts"] == 2
    before = job_state(world, jid)
    spent = receipt(cost=20.5, cost_cents=21)
    # The runner that held attempt 1 reports its paid call after the reaper requeued it.
    with pytest.raises(Conflict):
        store.fail_code_review(jid, "r1", "model_failed", 1071.0, receipt=spent)
    assert job_state(world, jid) == before
    cr = attempts_of(world, jid)
    assert [(e["attempt"], e["outcome"], e["receipt"]) for e in cr["attempt_receipts"]] == [
        (1, "vanished", None),
        (1, "late", normalized(spent)),
    ]
    assert cr["cost_cents_total"] == 21
    ((ev, _),) = events(world, "review_job.late_receipt")
    assert ev["attempt"] == 1 and ev["receipt_digest"] == canonical_digest(normalized(spent))
    # The current lease holder is unaffected and completes attempt 2.
    status, done = store.complete_code_review(jid, "r2", output(), 1072.0)
    assert status == 201 and done["job"]["state"] == "completed"
    cr = attempts_of(world, jid)
    assert [(e["attempt"], e["outcome"]) for e in cr["attempt_receipts"]] == [
        (1, "vanished"),
        (1, "late"),
        (2, "completed"),
    ]
    assert cr["cost_cents_total"] == 22


def test_late_receipt_after_reaper_failed_is_recorded(world):  # noqa: F811
    store = world.app.state.review_jobs
    jid = world.submit(cr_job()).json()["id"]
    store.claim_code_review("r1", 2, 60, 1000.0)
    assert store.reap_expired_code_review_leases(1061.0) == [jid]
    store.claim_code_review("r2", 2, 60, 1070.0)
    assert store.reap_expired_code_review_leases(1131.0) == [jid]
    assert job_state(world, jid)[:2] == ("failed", "model_child_vanished")
    with pytest.raises(Conflict):
        store.complete_code_review(jid, "r2", output(), 1132.0)
    cr = attempts_of(world, jid)
    assert [(e["attempt"], e["outcome"]) for e in cr["attempt_receipts"]] == [
        (1, "vanished"),
        (2, "vanished"),
        (2, "late"),
    ]
    assert cr["verdict"] is None and cr["cost_cents_total"] == 1
    assert job_state(world, jid)[:2] == ("failed", "model_child_vanished")


def test_late_call_from_non_owner_stores_nothing(world):  # noqa: F811
    store = world.app.state.review_jobs
    jid = world.submit(cr_job()).json()["id"]
    store.claim_code_review("r1", 2, 60, 1000.0)
    calls = (
        lambda now: store.fail_code_review(jid, "r9", "model_failed", now, receipt=receipt()),
        lambda now: store.complete_code_review(jid, "r9", output(), now),
    )
    # Expired but unreaped, then reaped and requeued: a runner that never held it is refused.
    for now in (1061.0, 1062.0):
        if now == 1062.0:
            assert store.reap_expired_code_review_leases(now) == [jid]
        for call in calls:
            with pytest.raises(Conflict):
                call(now)
    cr = attempts_of(world, jid)
    assert [e["outcome"] for e in cr["attempt_receipts"]] == ["vanished"]
    assert cr["cost_cents_total"] == 0
    assert events(world, "review_job.late_receipt") == []
    # The runner that did hold attempt 1 is recorded; the refused calls left no trace.
    with pytest.raises(Conflict):
        store.fail_code_review(jid, "r1", "model_failed", 1063.0, receipt=receipt())
    cr = attempts_of(world, jid)
    assert [e["outcome"] for e in cr["attempt_receipts"]] == ["vanished", "late"]
    assert cr["cost_cents_total"] == 1
    ((ev, _),) = events(world, "review_job.late_receipt")
    assert ev["attempt"] == 1


# -- custody content for the runner -------------------------------------------------------


def test_code_review_inputs_returns_verified_bytes(world):  # noqa: F811
    store = world.app.state.review_jobs
    jid = world.submit(cr_job()).json()["id"]
    store.claim_code_review("r1", 2, 60, 1000.0)
    got = store.code_review_inputs(jid, "r1", 1001.0)
    assert [(i["snapshot_id"], i["digest"], i["data_class"]) for i in got] == [
        (SNAPSHOTS[d], d, "private_derived") for d in (H, B, R)
    ]
    for item in got:
        assert {f["path"]: f["content"] for f in item["files"]} == CONTENT[item["digest"]]
        assert all(isinstance(f["content"], bytes) for f in item["files"])


def test_code_review_inputs_refuses_wrong_owner_and_not_running(world):  # noqa: F811
    store = world.app.state.review_jobs
    jid = world.submit(cr_job()).json()["id"]
    with pytest.raises(Conflict):
        store.code_review_inputs(jid, "r1", 1000.0)  # queued, not leased
    store.claim_code_review("r1", 2, 60, 1000.0)
    with pytest.raises(Conflict):
        store.code_review_inputs(jid, "r2", 1001.0)


@pytest.mark.parametrize(
    "tamper",
    [
        "UPDATE snapshot_files SET content=? WHERE snapshot_id='snap-head'",
        "DELETE FROM snapshot_files WHERE snapshot_id='snap-base' AND 1=length(?)",
        "UPDATE source_snapshots SET manifest_json=replace(manifest_json,'subject','other') "
        "WHERE id='snap-rubric' AND 1=length(?)",
        "INSERT INTO snapshot_files VALUES ('snap-head','extra.py',?)",
    ],
)
def test_code_review_inputs_refuses_tampered_content(world, tamper):  # noqa: F811
    store = world.app.state.review_jobs
    jid = world.submit(cr_job()).json()["id"]
    store.claim_code_review("r1", 2, 60, 1000.0)
    with sqlite3.connect(world.db) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(tamper, (b"x",))
    with pytest.raises(Conflict):
        store.code_review_inputs(jid, "r1", 1001.0)


def test_code_review_inputs_only_from_the_jobs_project(world):  # noqa: F811
    store = world.app.state.review_jobs
    jid = world.submit(cr_job()).json()["id"]
    store.claim_code_review("r1", 2, 60, 1000.0)
    second = world.client.post(
        "/projects", headers={**world.owner, "Idempotency-Key": "p2"}, json={"name": "R2"}
    ).json()["id"]
    with sqlite3.connect(world.db) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("UPDATE source_snapshots SET project_id=? WHERE id='snap-base'", (second,))
    with pytest.raises(Conflict):
        store.code_review_inputs(jid, "r1", 1001.0)
