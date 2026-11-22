"""Code review submit from a platform git branch (ADR 0108). Synthetic only: temporary bare
repositories built with the git CLI, no network, no model call (findings are ingested directly)."""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from test_code_review_git import commit, git, write
from test_code_review_jobs import CRPolicy, receipt
from test_review_jobs import _enroll, world  # noqa: F401

from tessera_agent.code_review_child import CODE_RECHECK_PROMPT_SHA256, CODE_REVIEW_PROMPT_SHA256
from tessera_control.api import create_app
from tessera_control.code_review_git import GitRemotes
from tessera_control.code_review_submit import CODE_REVIEW_RUBRIC, focused_tests_valid
from tessera_control.snapshots import SnapshotStore
from tessera_control.store import canonical_digest, initialize_state

TEST_FILE = "tests/test_calc.py"


@pytest.fixture
def repo(tmp_path):
    """A bare remote with ``main`` and two feature branches (``feature/x``, ``feature/y``)."""
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    work = tmp_path / "work"
    git(tmp_path, "init", "-q", str(work))
    git(work, "remote", "add", "origin", str(bare))
    write(
        work,
        {
            "src/calc.py": b"def add(a, b):\n    return a - b\n",
            "src/old.py": b"old = 1\n",
            TEST_FILE: b"def test_add():\n    assert True\n",
            "tests/unit/test_more.py": b"def test_more():\n    assert True\n",
        },
    )
    base = commit(work, "base")
    git(work, "push", "-q", "origin", "main")
    git(work, "checkout", "-q", "-b", "feature/x")
    write(work, {"src/calc.py": b"def add(a, b):\n    return a + b\n", "src/new.py": b"n = 1\n"})
    (work / "src" / "old.py").unlink()
    head = commit(work, "fix add")
    git(work, "push", "-q", "origin", "feature/x")
    git(work, "checkout", "-q", "-b", "feature/y", "main")
    write(work, {"src/calc.py": b"def add(a, b):\n    return b + a\n"})
    other = commit(work, "other fix")
    git(work, "push", "-q", "origin", "feature/y")
    return {"bare": bare, "base": base, "head": head, "other": other}


@pytest.fixture
def cr(world, repo):  # noqa: F811
    world.app.state.review_policy = CRPolicy()
    world.app.state.code_review_git = GitRemotes({"demo": repo["bare"]})
    world.repo = repo
    world.cr = lambda body=None, who=None, key=None: world.post(
        f"/projects/{world.project}/code-reviews",
        who or world.submitter,
        body if body is not None else submit_body(repo),
        key,
    )
    return world


def submit_body(git_repo, /, **over):
    return {
        "repo": "demo",
        "branch": "feature/x",
        "head_sha": git_repo["head"],
        "tier": "sonnet",
        "budget_cents": 100,
        "focused_tests": [TEST_FILE],
        **over,
    }


def count(w, table):
    with sqlite3.connect(w.db) as db:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def snapshot_files(w, snapshot_id):
    with sqlite3.connect(w.db) as db:
        return dict(
            db.execute(
                "SELECT relative_path,content FROM snapshot_files WHERE snapshot_id=?",
                (snapshot_id,),
            )
        )


def events(w, kind):
    with sqlite3.connect(w.db) as db:
        return [
            json.loads(r[0])
            for r in db.execute("SELECT payload_json FROM events WHERE event_type=?", (kind,))
        ]


def job_receipt(job, **over):
    """A receipt bound to ``job`` (its inputs and its pinned prompt digest)."""
    return receipt(
        input_digests={item["snapshot_id"]: item["digest"] for item in job["inputs"]},
        prompt_sha256=job["prompt_digest"],
        **over,
    )


def recheck_finding(number, severity="minor", fid="1"):
    return {
        "id": fid,
        "severity": severity,
        "category": f"recheck:{number}",
        "claim": "still unresolved",
        "evidence": [{"source": "subject", "locator": "src/calc.py:2"}],
        "confidence": 0.5,
    }


def plain_finding(fid, severity="minor"):
    return {**recheck_finding(1, severity, fid), "category": "correctness"}


def ingest(w, job, findings, verdict):
    return w.post(
        f"/review-jobs/{job['id']}/findings",
        w.reviewer,
        {"findings": findings, "verdict": verdict, "receipt": job_receipt(job)},
    )


def completed_round1(w, verdict="CHANGES-REQUIRED"):
    job = w.cr().json()
    findings = [] if verdict == "MERGE" else [plain_finding("1", "major"), plain_finding("2")]
    r = ingest(w, job, findings, verdict)
    assert r.status_code == 201, r.text
    return job


# -- round 1 -------------------------------------------------------------------------------


def test_round1_adopts_three_snapshots_and_queues_a_diff_job(cr):
    r = cr.cr()
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["kind"] == "code_review" and job["state"] == "queued"
    assert job["egress"] == "model_runtime" and job["destination_id"] == "anthropic"
    assert job["prompt_digest"] == CODE_REVIEW_PROMPT_SHA256
    assert job["advisory"] is True and job["execution_authorized"] is False
    # Classes are derived from the custody records, never supplied by the client.
    assert job["data_classes"] == ["private_derived"]
    assert [i["data_class"] for i in job["inputs"]] == ["private_derived"] * 3
    subject = job["code_review"]
    assert subject["mode"] == "diff"
    assert subject["source"] == {
        "repo": "demo",
        "branch": "feature/x",
        "head_sha": cr.repo["head"],
        "base_branch": "main",
        "base_sha": cr.repo["base"],
        "focused_tests": [TEST_FILE],
        "round": 1,
        "previous_job_id": None,
        "recheck_findings": [],
    }
    assert subject["review_receipt"] is None
    head, base, rubric = job["inputs"]
    assert (subject["head_digest"], subject["base_digest"], subject["rubric_digest"]) == (
        head["digest"],
        base["digest"],
        rubric["digest"],
    )
    assert snapshot_files(cr, head["snapshot_id"]) == {
        "src/calc.py": b"def add(a, b):\n    return a + b\n",
        "src/new.py": b"n = 1\n",
        TEST_FILE: b"def test_add():\n    assert True\n",
    }
    assert snapshot_files(cr, base["snapshot_id"]) == {
        "src/calc.py": b"def add(a, b):\n    return a - b\n",
        "src/old.py": b"old = 1\n",
    }
    assert snapshot_files(cr, rubric["snapshot_id"]) == {
        "rubric.md": CODE_REVIEW_RUBRIC.encode(),
        "focused-tests.txt": f"{TEST_FILE}\n".encode(),
    }
    # In custody: each manifest hashes to its digest and verifies against retained bytes.
    principal = cr.app.state.review_jobs.authenticate(cr.owner["Authorization"].split()[1])
    snapshots = SnapshotStore(cr.state)
    for item in job["inputs"]:
        verified = snapshots.verify_snapshot(principal, item["snapshot_id"])
        assert verified["integrity"] == "verified" and verified["digest"] == item["digest"]
    assert count(cr, "code_review_sources") == 1
    assert len(events(cr, "workspace.snapshot_adopted")) == 3
    (submitted,) = [e for e in events(cr, "review_job.submitted") if e["job_id"] == job["id"]]
    assert submitted["source"] == subject["source"]
    # The read route shows the same record.
    got = cr.client.get(f"/review-jobs/{job['id']}", headers=cr.submitter).json()
    assert got["code_review"]["source"] == subject["source"]


def test_human_may_submit(cr):
    r = cr.cr(who=cr.owner)
    assert r.status_code == 201, r.text


@pytest.mark.parametrize(
    "tests",
    [
        ["tests"],  # a directory
        ["tests/"],
        ["tests/unit"],
        ["tests/test_*.py"],  # glob
        ["tests/test_[a].py"],
        ["tests/test_?.py"],
        ["tests/../tests/test_calc.py"],
        ["../tests/test_calc.py"],
        ["/tests/test_calc.py"],
        ["src/test_calc.py"],  # not under tests/
        ["tests/calc_test.py"],  # not test_*.py
        ["tests/test_calc.txt"],
        ["tests/test_absent.py"],  # missing in head
        ["src/calc.py"],
        [TEST_FILE, TEST_FILE],  # duplicate
        [f"tests/test_{n}.py" for n in range(21)],  # more than 20
        [],
    ],
)
def test_focused_tests_refused(cr, tests):
    r = cr.cr(submit_body(cr.repo, focused_tests=tests))
    assert r.status_code == 422, r.text
    assert r.json()["reason"] == "focused_tests_invalid"
    assert count(cr, "review_jobs") == 0 and count(cr, "source_snapshots") == 1  # the seed only


def test_focused_tests_rule_accepts_nested_tests_dirs():
    assert focused_tests_valid(["tests/unit/test_more.py", "pkg/tests/test_a.py"])
    assert not focused_tests_valid(["tests/unit/test_more.py/"])


def test_nested_focused_test_is_snapshotted(cr):
    r = cr.cr(submit_body(cr.repo, focused_tests=["tests/unit/test_more.py", TEST_FILE]))
    assert r.status_code == 201, r.text
    head = r.json()["inputs"][0]["snapshot_id"]
    assert {"tests/unit/test_more.py", TEST_FILE} <= set(snapshot_files(cr, head))


@pytest.mark.parametrize(
    ("over", "status", "reason"),
    [
        ({"head_sha": "a" * 40}, 409, "head_mismatch"),
        ({"branch": "feature/none"}, 409, "branch_not_found"),
        ({"repo": "unknown"}, 409, "repo_not_configured"),
        ({"base_branch": "develop"}, 409, "base_not_found"),
        ({"branch": "main", "head_sha": None}, 409, "no_changes"),
        ({"branch": "bad..name"}, 422, "invalid_branch"),
        ({"repo": "-bad"}, 422, "invalid_repo"),
    ],
)
def test_git_refusals_have_stable_reasons(cr, over, status, reason):
    if "head_sha" in over and over["head_sha"] is None:
        over = {**over, "head_sha": cr.repo["base"]}
    r = cr.cr(submit_body(cr.repo, **over))
    assert r.status_code == status, r.text
    assert r.json() == {"detail": reason, "reason": reason}
    assert count(cr, "review_jobs") == 0 and count(cr, "code_review_sources") == 0


def test_unconfigured_git_is_refused(cr):
    cr.app.state.code_review_git = None
    r = cr.cr()
    assert r.status_code == 409 and r.json()["reason"] == "git_remote_not_configured"
    assert count(cr, "review_jobs") == 0


def test_default_app_has_no_git_remote(tmp_path):
    state = tmp_path / "plain"
    initialize_state(state)
    assert create_app(state).state.code_review_git is None


def test_policy_refusal_leaves_nothing(cr):
    r = cr.cr(submit_body(cr.repo, destination_id="elsewhere"))
    assert r.status_code == 409 and r.json()["reason"] == "destination_not_enrolled"
    # One transaction: no snapshot, job, source or event is left behind.
    assert count(cr, "source_snapshots") == 1
    assert count(cr, "review_jobs") == 0 and count(cr, "code_review_sources") == 0
    assert events(cr, "workspace.snapshot_adopted") == []


def test_body_shape_is_422(cr):
    assert cr.cr(submit_body(cr.repo, head_sha="ABC")).status_code == 422
    assert cr.cr(submit_body(cr.repo, extra=1)).status_code == 422
    assert cr.cr(submit_body(cr.repo, tier="huge")).status_code == 422


# -- rounds --------------------------------------------------------------------------------


def test_round2_rechecks_listed_findings(cr):
    first = completed_round1(cr)
    r = cr.cr(submit_body(cr.repo, previous_job_id=first["id"], recheck_findings=[2, 1]))
    assert r.status_code == 201, r.text
    second = r.json()
    assert second["prompt_digest"] == CODE_RECHECK_PROMPT_SHA256
    source = second["code_review"]["source"]
    assert source["round"] == 2 and source["previous_job_id"] == first["id"]
    assert source["recheck_findings"] == [1, 2]
    rubric = snapshot_files(cr, second["inputs"][2]["snapshot_id"])
    assert json.loads(rubric["recheck.json"]) == [
        {"number": 1, "severity": "major", "category": "correctness", "claim": "still unresolved"},
        {"number": 2, "severity": "minor", "category": "correctness", "claim": "still unresolved"},
    ]
    assert rubric["rubric.md"] == CODE_REVIEW_RUBRIC.encode()


def test_round_limit_refuses_a_fourth_round(cr):
    previous = completed_round1(cr)
    for expected_round in (2, 3):
        r = cr.cr(submit_body(cr.repo, previous_job_id=previous["id"], recheck_findings=[1]))
        assert r.status_code == 201, r.text
        previous = r.json()
        assert previous["code_review"]["source"]["round"] == expected_round
        done = ingest(cr, previous, [recheck_finding(1, "major")], "CHANGES-REQUIRED")
        assert done.status_code == 201, done.text
    jobs = count(cr, "review_jobs")
    r = cr.cr(submit_body(cr.repo, previous_job_id=previous["id"], recheck_findings=[1]))
    assert r.status_code == 409 and r.json()["reason"] == "review_round_limit"
    assert count(cr, "review_jobs") == jobs


def test_chain_mismatch(cr):
    first = completed_round1(cr)
    other_branch = submit_body(
        cr.repo,
        branch="feature/y",
        head_sha=cr.repo["other"],
        previous_job_id=first["id"],
        recheck_findings=[1],
    )
    r = cr.cr(other_branch)
    assert r.status_code == 409 and r.json()["reason"] == "review_chain_mismatch"
    queued = cr.cr().json()  # not completed
    r = cr.cr(submit_body(cr.repo, previous_job_id=queued["id"], recheck_findings=[1]))
    assert r.status_code == 409 and r.json()["reason"] == "review_chain_mismatch"
    r = cr.cr(submit_body(cr.repo, previous_job_id="review_job_missing", recheck_findings=[1]))
    assert r.status_code == 409 and r.json()["reason"] == "review_chain_mismatch"
    # A completed job of another kind is not a code review chain.
    plain = cr.submit().json()
    cr.post(f"/review-jobs/{plain['id']}/findings", cr.reviewer, {"findings": []})
    r = cr.cr(submit_body(cr.repo, previous_job_id=plain["id"], recheck_findings=[1]))
    assert r.status_code == 409 and r.json()["reason"] == "review_chain_mismatch"


def test_previous_merge_is_refused(cr):
    first = completed_round1(cr, verdict="MERGE")
    r = cr.cr(submit_body(cr.repo, previous_job_id=first["id"], recheck_findings=[1]))
    assert r.status_code == 409 and r.json()["reason"] == "review_already_merge"


@pytest.mark.parametrize("numbers", [[], [1, 1], [3], [0], [-1]])
def test_recheck_numbers_invalid(cr, numbers):
    first = completed_round1(cr)
    r = cr.cr(submit_body(cr.repo, previous_job_id=first["id"], recheck_findings=numbers))
    assert r.status_code == 422 and r.json()["reason"] == "recheck_findings_invalid"


def test_recheck_findings_without_previous_is_invalid(cr):
    r = cr.cr(submit_body(cr.repo, recheck_findings=[1]))
    assert r.status_code == 422 and r.json()["reason"] == "recheck_findings_invalid"
    assert cr.cr(submit_body(cr.repo, recheck_findings=[])).status_code == 201


# -- re-review ingest rule and receipt -----------------------------------------------------


@pytest.mark.parametrize(
    "findings",
    [
        [plain_finding("1")],  # not a recheck category
        [recheck_finding(3)],  # not listed
        [recheck_finding(1), recheck_finding(1, fid="2")],  # twice
        [recheck_finding("01")],
        [{**recheck_finding(1), "category": "recheck:"}],
        [recheck_finding(1), plain_finding("2")],  # an extra finding
    ],
)
def test_rereview_ingest_rule(cr, findings):
    first = completed_round1(cr)
    second = cr.cr(
        submit_body(cr.repo, previous_job_id=first["id"], recheck_findings=[1, 2])
    ).json()
    r = ingest(cr, second, findings, "MERGE-WITH-FOLLOWUPS")
    assert r.status_code == 422 and r.json()["reason"] == "failed_output_invalid"
    got = cr.client.get(f"/review-jobs/{second['id']}", headers=cr.submitter).json()
    assert got["state"] == "queued" and count(cr, "review_findings") == 2


def test_rereview_completes_and_receipt_carries_source_and_findings(cr):
    first = completed_round1(cr)
    first_done = cr.client.get(f"/review-jobs/{first['id']}", headers=cr.submitter).json()
    review = first_done["code_review"]["review_receipt"]
    assert review["source"] == first_done["code_review"]["source"]
    assert [f["id"] for f in review["findings"]] == ["1", "2"]
    assert review["verdict"] == "CHANGES-REQUIRED"
    assert first_done["code_review"]["review_receipt_digest"] == canonical_digest(review)

    second = cr.cr(
        submit_body(cr.repo, previous_job_id=first["id"], recheck_findings=[1, 2])
    ).json()
    findings = [recheck_finding(2, "minor")]
    r = ingest(cr, second, findings, "MERGE-WITH-FOLLOWUPS")
    assert r.status_code == 201, r.text
    done = r.json()["job"]["code_review"]
    review = done["review_receipt"]
    assert review["findings"] == [f["finding"] for f in r.json()["findings"]]
    assert review["findings"][0]["category"] == "recheck:2"
    assert review["source"]["round"] == 2 and review["source"]["recheck_findings"] == [1, 2]
    assert review["verdict"] == "MERGE-WITH-FOLLOWUPS"
    assert done["review_receipt_digest"] == canonical_digest(review)
    # All listed findings resolved: an empty re-review may MERGE.
    third = cr.cr(submit_body(cr.repo, previous_job_id=second["id"], recheck_findings=[1])).json()
    assert third["code_review"]["source"]["round"] == 3
    assert ingest(cr, third, [], "MERGE").status_code == 201


# -- idempotency, authority, isolation -----------------------------------------------------


def test_idempotent_replay_returns_the_same_job(cr):
    first = cr.cr(key="same")
    assert first.status_code == 201, first.text
    # A replay reads no git: it succeeds even with the remote unconfigured.
    cr.app.state.code_review_git = None
    again = cr.cr(key="same")
    assert again.status_code == 201 and again.json() == first.json()
    assert count(cr, "review_jobs") == 1 and count(cr, "code_review_sources") == 1
    assert len(events(cr, "workspace.snapshot_adopted")) == 3
    cr.app.state.code_review_git = GitRemotes({"demo": cr.repo["bare"]})
    changed = cr.cr(submit_body(cr.repo, budget_cents=50), key="same")
    assert changed.status_code == 409


def test_agent_without_submit_scope_is_403(cr):
    reader = _enroll(cr.client, cr.owner, cr.project, ["review:read"])
    r = cr.cr(who=reader)
    assert r.status_code == 403
    assert count(cr, "review_jobs") == 0 and count(cr, "source_snapshots") == 1


def test_project_and_tenant_isolation(cr, tmp_path):
    first = completed_round1(cr)
    second = cr.client.post(
        "/projects", headers={**cr.owner, "Idempotency-Key": "p2"}, json={"name": "R2"}
    ).json()["id"]
    # An agent of another project cannot submit here.
    elsewhere = _enroll(cr.client, cr.owner, second, ["review:submit", "review:read"])
    assert cr.cr(who=elsewhere).status_code == 404
    # A previous job of another project does not chain.
    r = cr.post(
        f"/projects/{second}/code-reviews",
        cr.owner,
        submit_body(cr.repo, previous_job_id=first["id"], recheck_findings=[1]),
    )
    assert r.status_code == 409 and r.json()["reason"] == "review_chain_mismatch"
    # Another tenant (its own control state) cannot see the project or the job.
    other_state = tmp_path / "other-tenant"
    initialize_state(other_state)
    other = {"Authorization": (other_state / "owner.header").read_text().strip().split(": ", 1)[1]}
    other_app = create_app(other_state)
    other_app.state.review_policy = CRPolicy()
    other_app.state.code_review_git = GitRemotes({"demo": cr.repo["bare"]})
    oc = TestClient(other_app)
    r = oc.post(
        f"/projects/{cr.project}/code-reviews",
        headers={**other, "Idempotency-Key": "x"},
        json=submit_body(cr.repo),
    )
    assert r.status_code == 404
    assert oc.get(f"/review-jobs/{first['id']}", headers=other).status_code == 404


def test_sources_are_immutable(cr):
    cr.cr()
    with sqlite3.connect(cr.db) as db:
        for statement in (
            "UPDATE code_review_sources SET round=2",
            "DELETE FROM code_review_sources",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute(statement)
