"""Research review runner: research kinds only, model-spend caps, findings and receipts, replay.

Synthetic only: a fake model client stands in for the model; no network and no paid call. The
control store, the review-job seams and the model-spend ledger are the real ones.
"""

import hashlib
import json
import secrets
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_review_jobs import AUTO, _enroll, enrol_second_root

from tessera_control import research_review_runner as rrr
from tessera_control.api import create_app
from tessera_control.code_review_runner import RunnerRefused
from tessera_control.model_spend import ModelSpendStore
from tessera_control.research_review_runner import (
    RESEARCH_KINDS,
    RUNNER_SCOPES,
    ModelCallNotSent,
    ResearchAnswer,
    ResearchJournal,
    ResearchReviewRunner,
    runner_principal,
)
from tessera_control.store import canonical_digest, initialize_state

PROMPT = b"Critique this research plan for confounds and missing controls.\n"
PROMPT_DIGEST = hashlib.sha256(PROMPT).hexdigest()
DESTINATION = "runner-destination"
PLAN_FILES = {"plan.md": b"# Plan\nTrain a small model on a public set and compare.\n"}
RUBRIC_FILES = {"rubric.md": b"Check correctness.\n"}
ANSWER = json.dumps(
    {
        "findings": [
            {
                "severity": "major",
                "category": "controls",
                "claim": "No baseline run is planned.",
                "evidence": [{"source": "subject", "locator": "plan.md"}],
                "confidence": 0.7,
                "ignored": "dropped by the runner",
            },
            {
                "severity": "info",
                "category": "scope",
                "claim": "The dataset is public.",
                "evidence": [],
                "confidence": 0.4,
            },
        ]
    }
)


def manifest(files):
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
        "total_bytes": sum(len(c) for c in files.values()),
    }


def manifest_digest(files):
    text = json.dumps(manifest(files), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


PLAN, RUBRIC = manifest_digest(PLAN_FILES), manifest_digest(RUBRIC_FILES)


def seed(db, project, snapshot_id, files):
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO source_snapshots (id,project_id,digest,manifest_json,created_at,"
            "actor_principal_id) VALUES (?,?,?,?,'t',(SELECT id FROM principals LIMIT 1))",
            (snapshot_id, project, manifest_digest(files), json.dumps(manifest(files))),
        )
        conn.executemany(
            "INSERT INTO snapshot_files (snapshot_id,relative_path,content) VALUES (?,?,?)",
            [(snapshot_id, path, content) for path, content in files.items()],
        )


class Admit:
    def check(self, destination_id, tier, data_classes, budget_cents):
        return None if destination_id == "anthropic" else "destination_not_enrolled"


class FakeClient:
    provider = "anthropic"
    model = "configured-model"

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def call(self, request):
        self.calls.append(request)
        outcome = self.outcomes.pop(0) if self.outcomes else ResearchAnswer(ANSWER, 3.5)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class Crash(BaseException):
    """Stands in for the process dying: nothing in the runner catches it."""


@pytest.fixture
def world(tmp_path):
    state = tmp_path / "state"
    initialize_state(state)
    owner = {"Authorization": (state / "owner.header").read_text().strip().split(": ", 1)[1]}
    app = create_app(state)
    app.state.review_policy = Admit()
    client = TestClient(app)
    project = client.post(
        "/projects", headers={**owner, "Idempotency-Key": "p"}, json={"name": "R"}
    ).json()["id"]
    db = state / "tessera.sqlite3"
    seed(db, project, "snap-plan", PLAN_FILES)
    seed(db, project, "snap-rubric", RUBRIC_FILES)
    submitter = _enroll(client, owner, project, ["review:submit", "review:read"])
    root = enrol_second_root(db, owner, client)

    class W:
        pass

    w = W()
    w.state, w.app, w.client, w.owner, w.project, w.db, w.tmp = (
        state,
        app,
        client,
        owner,
        project,
        db,
        tmp_path,
    )
    w.submitter, w.root = submitter, root
    runner_headers = _enroll(client, root, project, sorted(RUNNER_SCOPES), DESTINATION)
    w.token = runner_headers["Authorization"].removeprefix("Bearer ")
    w.principal = runner_principal(app.state.review_jobs, w.token, DESTINATION)
    w.spend = ModelSpendStore(state)

    def limits(per_call=50, daily=100, provider="anthropic"):
        r = client.put(
            "/model-spend/limits",
            headers={**owner, **AUTO, "Idempotency-Key": secrets.token_hex(8)},
            json={
                "project_id": project,
                "provider_id": provider,
                "per_call_cents": per_call,
                "daily_cents": daily,
            },
        )
        assert r.status_code == 200, r.text
        return r

    def submit(**over):
        body = {
            "kind": "plan_review",
            "title": "review the plan",
            "inputs": [{"snapshot_id": "snap-plan", "digest": PLAN}],
            "prompt_digest": PROMPT_DIGEST,
            "destination_id": "anthropic",
            "tier": "sonnet",
            "budget_cents": 20,
            "egress": "model_runtime",
            **over,
        }
        r = client.post(
            f"/projects/{project}/review-jobs",
            headers={**submitter, "Idempotency-Key": secrets.token_hex(8)},
            json=body,
        )
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def get(job_id):
        return client.get(f"/review-jobs/{job_id}", headers=submitter).json()

    def runner(model, journal_name="journal.jsonl", spend=None, **over):
        journal = ResearchJournal(tmp_path / journal_name)
        w.journals.append(journal)
        return ResearchReviewRunner(
            app.state.review_jobs,
            spend or w.spend,
            w.principal,
            model,
            over.pop("policy", Admit()),
            {PROMPT_DIGEST: PROMPT.decode()},
            journal,
            project_id=project,
            receipts_dir=tmp_path / "receipts",
            **over,
        )

    def ledger():
        with sqlite3.connect(db) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute("SELECT * FROM model_spend_calls")]

    w.limits, w.submit, w.get, w.runner, w.ledger, w.journals = (
        limits,
        submit,
        get,
        runner,
        ledger,
        [],
    )
    yield w
    for journal in w.journals:
        journal.close()


def code_review_body():
    return {
        "kind": "code_review",
        "inputs": [
            {"snapshot_id": "snap-plan", "digest": PLAN},
            {"snapshot_id": "snap-rubric", "digest": RUBRIC},
        ],
        "code_review": {"mode": "snapshot", "head_digest": PLAN, "rubric_digest": RUBRIC},
    }


def findings_of(w, job_id):
    return w.client.get(
        f"/projects/{w.project}/review-findings?job_id={job_id}", headers=w.submitter
    ).json()["findings"]


def test_claims_only_research_kinds_and_records_findings_and_receipt(world):
    world.limits()
    code_review = world.submit(**code_review_body())
    plan = world.submit()
    scout = world.submit(kind="dataset_scout", title="find data")
    model = FakeClient()
    runner = world.runner(model, max_jobs_per_tick=4)
    assert runner.run_once() == 2
    assert world.get(code_review)["state"] == "queued"  # never claimed
    assert sorted(call.kind for call in model.calls) == ["dataset_scout", "plan_review"]
    # The pinned prompt and the verified custody bytes reach the model, with the model config.
    request = model.calls[0]
    assert PROMPT.decode().strip() in request.message
    assert PLAN_FILES["plan.md"].decode() in request.message
    assert request.model == "configured-model" and request.max_cost_cents == 20
    for job_id in (plan, scout):
        job = world.get(job_id)
        assert job["state"] == "completed", job
        receipt_path = world.tmp / "receipts" / f"{job_id}.json"
        receipt = json.loads(receipt_path.read_text())
        assert receipt_path.stat().st_mode & 0o077 == 0
        assert job["receipt_digest"] == canonical_digest(receipt)
        assert receipt["job_id"] == job_id and receipt["kind"] == job["kind"]
        assert receipt["destination_id"] == "anthropic" and receipt["tier"] == "sonnet"
        assert receipt["data_classes"] == job["data_classes"] == ["private_derived"]
        assert receipt["prompt_digest"] == PROMPT_DIGEST
        assert receipt["model_configured"] == "anthropic/configured-model"
        assert receipt["outcome"] == "completed" and receipt["findings_count"] == 2
        assert receipt["spend"]["actual_cents"] == 3.5
        assert receipt["spend"]["reserved_cents"] == 20
        assert receipt["claimed_at"] <= receipt["model_started_at"] <= receipt["model_finished_at"]
        found = findings_of(world, job_id)
        assert [f["finding"]["id"] for f in found] == ["1", "2"]
        assert "ignored" not in found[0]["finding"]
        assert all(f["advisory"] and f["untrusted_text"] for f in found)
    # One reservation per job, settled at the actual cost and bound to the receipt digest.
    rows = world.ledger()
    assert sorted(r["run_id"] for r in rows) == sorted([plan, scout])
    for row in rows:
        assert row["status"] == "settled"
        assert row["reserved_micro"] == 20_000_000 and row["settled_micro"] == 3_500_000
        assert row["receipt_sha256"] == world.get(row["run_id"])["receipt_digest"]
        assert row["model"] == "anthropic/configured-model" and row["seq"] == 1
    # Nothing more to do: a second tick claims nothing and spends nothing.
    assert runner.run_once() == 0
    assert len(model.calls) == 2 and len(world.ledger()) == 2


def test_research_kinds_never_include_code_review():
    assert "code_review" not in RESEARCH_KINDS
    assert set(RESEARCH_KINDS) == {
        "plan_review",
        "dataset_scout",
        "technique_scout",
        "pre_gate_review",
    }


def test_never_claims_code_review_even_alone(world):
    world.limits()
    code_review = world.submit(**code_review_body())
    model = FakeClient()
    runner = world.runner(model, max_jobs_per_tick=4)
    assert runner.run_once() == 0
    assert runner._start(world.get(code_review)) is False
    assert world.get(code_review)["state"] == "queued" and model.calls == []
    assert world.ledger() == []


def test_per_call_cap_refuses_before_any_call(world):
    world.limits(per_call=10, daily=100)
    job_id = world.submit(budget_cents=20)
    model = FakeClient()
    world.runner(model).run_once()
    job = world.get(job_id)
    assert job["state"] == "failed" and job["reason"] == "model_spend_cap_exceeded"
    assert model.calls == [] and world.ledger() == []


def test_daily_cap_counts_settled_spend(world):
    world.limits(per_call=50, daily=60)
    first, second = world.submit(budget_cents=50), world.submit(budget_cents=50)
    model = FakeClient(ResearchAnswer(ANSWER, 40.0))
    runner = world.runner(model, max_jobs_per_tick=2)
    runner.run_once()
    states = sorted((world.get(j)["state"], world.get(j)["reason"]) for j in (first, second))
    # 40 settled + 50 worst case > 60: the later one is refused, fail closed, never called.
    assert states == [("completed", None), ("failed", "model_spend_cap_exceeded")]
    assert len(model.calls) == 1


def test_unconfigured_ledger_refuses(world):
    job_id = world.submit()
    model = FakeClient()
    world.runner(model).run_once()
    job = world.get(job_id)
    assert job["state"] == "failed" and job["reason"] == "model_spend_not_configured"
    assert model.calls == []


def test_actual_above_budget_is_settled_truthfully_and_fails(world):
    world.limits()
    job_id = world.submit(budget_cents=20)
    world.runner(FakeClient(ResearchAnswer(ANSWER, 25.2))).run_once()
    job = world.get(job_id)
    assert job["state"] == "failed" and job["reason"] == "budget_exceeded"
    (row,) = world.ledger()
    assert row["status"] == "settled" and row["settled_micro"] == 25_200_000
    assert row["receipt_sha256"] == job["receipt_digest"]
    assert findings_of(world, job_id) == []


def test_not_sent_releases_the_reservation(world):
    world.limits()
    job_id = world.submit()
    world.runner(FakeClient(ModelCallNotSent("budget_exceeded"))).run_once()
    job = world.get(job_id)
    assert job["state"] == "failed" and job["reason"] == "budget_exceeded"
    (row,) = world.ledger()
    assert row["status"] == "released"


def test_invalid_output_is_paid_for_and_fails(world):
    world.limits()
    job_id = world.submit()
    world.runner(FakeClient(ResearchAnswer("not json", 2.0))).run_once()
    job = world.get(job_id)
    assert job["state"] == "failed" and job["reason"] == "failed_output_invalid"
    (row,) = world.ledger()
    assert row["status"] == "settled" and row["settled_micro"] == 2_000_000


def test_launch_checks_mark_not_started(world):
    world.limits()
    unpinned = world.submit(prompt_digest="c" * 64)
    no_egress = world.submit(egress="none")
    model = FakeClient()
    world.runner(model, max_jobs_per_tick=4).run_once()
    assert world.get(unpinned)["state"] == "not_started"
    assert world.get(unpinned)["reason"] == "prompt_not_pinned"
    assert world.get(no_egress)["reason"] == "egress_not_permitted"
    assert model.calls == [] and world.ledger() == []


class CrashOnce:
    """Wraps the ledger; raises ``Crash`` on the first call of ``method``."""

    def __init__(self, inner, method):
        self.inner, self.method, self.crashed = inner, method, False

    def __getattr__(self, name):
        attribute = getattr(self.inner, name)
        if name != self.method or self.crashed:
            return attribute

        def crash(*args, **kwargs):
            self.crashed = True
            raise Crash()

        return crash


def test_crash_after_the_model_answered_replays_without_a_second_call(world):
    world.limits()
    job_id = world.submit()
    first_model = FakeClient()
    first = world.runner(first_model, spend=CrashOnce(world.spend, "settle"))
    with pytest.raises(Crash):
        first.run_once()
    assert world.get(job_id)["state"] == "running"
    first.journal.close()
    second_model = FakeClient()
    second = world.runner(second_model)
    assert second.runner_id == first.runner_id  # persistent identity from the journal
    second.run_once()
    assert len(first_model.calls) == 1 and second_model.calls == []
    job = world.get(job_id)
    assert job["state"] == "completed"
    (row,) = world.ledger()  # one reservation, settled once at the actual cost
    assert row["status"] == "settled" and row["settled_micro"] == 3_500_000
    assert len(findings_of(world, job_id)) == 2


def test_crash_after_reserve_replays_the_same_reservation(world):
    world.limits()
    job_id = world.submit()
    first = world.runner(FakeClient(Crash()))
    # Crash inside the call: the call may have been sent, so a replay never calls again.
    with pytest.raises(Crash):
        first.run_once()
    first.journal.close()
    second_model = FakeClient()
    world.runner(second_model).run_once()
    assert second_model.calls == []
    job = world.get(job_id)
    assert job["state"] == "failed" and job["reason"] == "model_call_uncertain"
    (row,) = world.ledger()
    assert row["status"] == "reserved"  # left open for a person to resolve (ADR 0090)


def test_crash_before_the_call_resumes_with_one_reservation(world):
    world.limits()
    job_id = world.submit()
    # The process dies while reserving: no call was started, so a replay may call once.
    first = world.runner(FakeClient(), spend=CrashOnce(world.spend, "reserve"))
    with pytest.raises(Crash):
        first.run_once()
    first.journal.close()
    model = FakeClient()
    world.runner(model).run_once()
    assert len(model.calls) == 1
    assert world.get(job_id)["state"] == "completed"
    assert len(world.ledger()) == 1


def test_a_second_runner_does_not_take_a_claimed_job(world):
    world.limits()
    job_id = world.submit()
    first = world.runner(FakeClient(), spend=CrashOnce(world.spend, "reserve"))
    with pytest.raises(Crash):
        first.run_once()
    other_model = FakeClient()
    other = world.runner(other_model, journal_name="other.jsonl")
    assert other.runner_id != first.runner_id
    assert other.run_once() == 0  # the running job is not queued; nothing to claim
    # Even when pointed at it directly, another runner's claim key conflicts.
    other.journal.write({"event": "claiming", "job_id": job_id, "kind": "plan_review"})
    other._run_claimed(job_id)
    assert other.journal.job(job_id)["done"] == "claim_lost"
    assert other_model.calls == [] and world.ledger() == []


def test_runner_credential_needs_exactly_the_runner_scopes(world):
    broad = _enroll(
        world.client,
        world.root,
        world.project,
        [*sorted(RUNNER_SCOPES), "review:submit"],
        DESTINATION,
    )
    narrow = _enroll(world.client, world.root, world.project, ["review:read"], DESTINATION)
    store = world.app.state.review_jobs
    with pytest.raises(RunnerRefused, match="agent_scope_too_broad"):
        runner_principal(store, broad["Authorization"].removeprefix("Bearer "), DESTINATION)
    with pytest.raises(RunnerRefused, match="agent_scope_missing"):
        runner_principal(store, narrow["Authorization"].removeprefix("Bearer "), DESTINATION)
    with pytest.raises(RunnerRefused, match="agent_credential_invalid"):
        runner_principal(store, "not-a-token", DESTINATION)
    with pytest.raises(RunnerRefused, match="agent_credential_invalid"):
        runner_principal(store, world.token, "another-destination")


def test_kill_switch_pauses_claims(world):
    world.limits()
    job_id = world.submit()
    switch = world.tmp / "kill"
    switch.touch()
    model = FakeClient()
    runner = world.runner(model, kill_switch=switch)
    assert runner.run_once() == 0
    assert world.get(job_id)["state"] == "queued"
    switch.unlink()
    runner.run_once()
    assert world.get(job_id)["state"] == "completed"


def test_journal_is_private_and_exclusive(tmp_path):
    with ResearchJournal(tmp_path / "j.jsonl") as journal:
        assert (tmp_path / "j.jsonl").stat().st_mode & 0o777 == 0o600
        with pytest.raises(rrr.JournalInUse):
            ResearchJournal(tmp_path / "j.jsonl")
        runner_id = journal.runner_id
    with ResearchJournal(tmp_path / "j.jsonl") as again:
        assert again.runner_id == runner_id


def test_load_prompts_pins_by_content_digest(tmp_path):
    (tmp_path / "plan.txt").write_bytes(PROMPT)
    assert rrr.load_prompts(tmp_path) == {PROMPT_DIGEST: PROMPT.decode()}
    (tmp_path / "link").symlink_to(tmp_path / "plan.txt")
    with pytest.raises(RunnerRefused, match="prompt_invalid"):
        rrr.load_prompts(tmp_path)


def test_main_refuses_without_policy_or_with_an_api_key(tmp_path):
    args = [
        "--state-dir",
        str(tmp_path),
        "--project-id",
        "p",
        "--journal",
        str(tmp_path / "j"),
        "--receipts-dir",
        str(tmp_path / "r"),
        "--prompts-dir",
        str(tmp_path),
        "--model-profile",
        str(tmp_path / "profile.json"),
    ]
    assert rrr.main(args, environment={}) == 2
    assert rrr.main([*args, "--policy", "x:y"], environment={"ANTHROPIC_API_KEY": "x"}) == 2
    assert not (tmp_path / "j").exists()


class _Blocked(Exception):
    pass


def test_anthropic_client_maps_outcomes(monkeypatch, tmp_path):
    import tessera_agent.anthropic_messages as messages
    import tessera_agent.remote_reasoning_runtime as runtime

    behaviour = {}

    class FakeReasoning:
        def __init__(self, profile, *, credentials_dir, transport, budget_check, output_sink):
            self.budget_check, self.sink = budget_check, output_sink

        def prompt(self, row, text):
            mode = behaviour["mode"]
            if mode == "over_budget":
                assert self.budget_check(1000, 99.0) is False
                error = runtime.RuntimeBlocked("model_call_cap_exceeded")
                error.request_sent = False
                raise error
            if mode == "uncertain":
                error = runtime.RuntimeBlocked("transport_failed")
                error.request_sent = None
                raise error
            self.sink(row, ANSWER)

        def status(self, row):
            return {
                "usage": {"input": 10, "output": 5, "cache_read": 0, "cache_write": 0},
                "tokens": 15,
                "cost": 1.25,
                "reported_model": "configured-model-20990101",
            }

    monkeypatch.setattr(messages, "AnthropicMessagesReasoning", FakeReasoning)
    client = rrr.AnthropicResearchClient({"model": {"id": "configured-model"}}, tmp_path)
    request = rrr.ResearchCall("job", "plan_review", "sonnet", "anthropic", "m", "d", "x", 20, ())
    behaviour["mode"] = "ok"
    answer = client.call(request)
    assert answer.text == ANSWER and answer.cost_cents == 1.25
    assert answer.model_reported == "configured-model-20990101" and answer.tokens["total"] == 15
    behaviour["mode"] = "over_budget"
    with pytest.raises(ModelCallNotSent) as not_sent:
        client.call(request)
    assert not_sent.value.code == "budget_exceeded"
    behaviour["mode"] = "uncertain"
    with pytest.raises(runtime.RuntimeBlocked):
        client.call(request)


def test_module_has_no_hard_coded_model_ids():
    text = Path(rrr.__file__).read_text(encoding="utf-8")
    assert "claude-" not in text and "sonnet-4" not in text


def test_systemd_unit_is_admitted_manual_and_matches_the_cli():
    import configparser
    import shlex

    root = Path(__file__).resolve().parents[1]
    path = root / "deploy" / "systemd" / "tessera-research-review-runner.service"
    unit = configparser.ConfigParser(strict=False, interpolation=None)
    unit.optionxform = str
    unit.read(path, encoding="utf-8")
    assert "Install" not in unit.sections() and "PartOf" not in unit["Unit"]
    assert unit["Unit"]["ConditionPathExists"] == "/etc/tessera/research-review-runner.admitted"
    target = (root / "deploy" / "systemd" / "tessera.target").read_text(encoding="utf-8")
    assert "research-review-runner" not in target
    text = path.read_text(encoding="utf-8")
    assert "LoadCredential=agent-token:" in text and "LoadCredential=anthropic-api-key:" in text
    words = shlex.split(unit["Service"]["ExecStart"])
    assert words[1:3] == ["-m", "tessera_control.research_review_runner"]
    flags = {action.option_strings[0] for action in rrr.build_parser()._actions[1:]}
    assert {w for w in words if w.startswith("--")} <= flags
    assert words[words.index("--kill-switch") + 1] == "/etc/tessera/research-review-runner.kill"
    for key in ("NoNewPrivileges", "PrivateTmp"):
        assert unit["Service"][key] == "yes"
    assert unit["Service"]["ProtectSystem"] == "strict"


def test_a_tier_start_refusal_marks_the_job_not_started(world, monkeypatch):
    """ADR 0168: a job whose tier grant no longer holds ends not_started; it is never left
    queued for mark_running to refuse on every tick, and nothing is reserved or called."""
    import tessera_control.review_tiers as tiers

    world.limits()
    job = world.submit()
    monkeypatch.setattr(
        tiers, "tier_start_refusal", lambda connection, row: "tier_grant_not_active"
    )
    model = FakeClient()
    world.runner(model).run_once()
    assert world.get(job)["state"] == "not_started"
    assert world.get(job)["reason"] == "tier_grant_not_active"
    assert model.calls == [] and world.ledger() == []


def test_a_grant_of_another_credential_is_refused_at_launch(world, monkeypatch):
    """Only a grant's subject may spend it, so a job approved on a grant issued to another
    credential cannot be paid for by this runner: it ends not_started before any reservation."""
    world.limits()
    job = world.submit()
    runner = world.runner(FakeClient())
    record = dict(runner.reviews.get_job(runner.principal, job))
    record["approval"] = {"grant_id": "g", "tier_grant": {"subject_credential_id": "other"}}
    assert runner._tier_refusal(record) == "tier_grant_scope_mismatch"
    record["approval"]["tier_grant"]["subject_credential_id"] = runner.principal["credential_id"]
    assert runner._tier_refusal(record) is None
