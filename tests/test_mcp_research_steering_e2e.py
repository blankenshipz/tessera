"""Steering the on-platform research agent loop through tessera_mcp tools (ADR 0084, 0085, 0089).

A real control app (in process, temp state), the real standing loop, auto-job planner and
reasoning supervisor, with the model call faked at the harness: no network, no provider. Every
client step is a tool call; the acts the design keeps with a person or the operator are marked
``HUMAN`` / ``OPERATOR``.

What is exercised: the loop submits its own research jobs as an enrolled agent on a plan being
admitted and advanced (idempotently per trigger), a client lists and reads them, the kill switch
pauses the loop and a pause holds everything, the loop's fake model call writes a real
metadata-only model-call receipt whose digest is stored on the job, a reviewer identity ingests
findings, the client lists and reads them, and a person accepts one and rejects another.

Known gaps this test pins rather than hides are listed at the bottom of the file.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("mcp")

from campaign_fixture import Env  # noqa: E402
from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402
from research_steering_fixtures import (  # noqa: E402
    API_KEY,
    CREDENTIAL,
    JOB_DESTINATION,
    PROMPT,
    ReviewPolicy,
    enrol_person,
    fake_model_call,
    second_root_agent,
    seed_private_snapshot,
)

from tessera_agent.auto_research_jobs import (  # noqa: E402
    DEFAULT_JOB_CLASSES,
    AutoJobLedger,
    AutoJobPlanner,
    AutoJobPolicy,
    PlanTrigger,
    ReviewSubmitRefused,
)
from tessera_agent.standing_loop import (  # noqa: E402
    KillSwitch,
    LoopPolicy,
    SqliteCallLedger,
    StandingLoop,
)
from tessera_control.api import create_app  # noqa: E402
from tessera_control.store import canonical_digest  # noqa: E402
from tessera_mcp.client import ControlClient  # noqa: E402
from tessera_mcp.config import Config  # noqa: E402
from tessera_mcp.server import create_server  # noqa: E402

PROMPT_DIGEST = hashlib.sha256(b"pinned prompt").hexdigest()
DESTINATION = "planner.local"  # the agents' enrolled destination (ADR 0047)


def _tool(server, name, **arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    return result[1] if isinstance(result, tuple) else result


class Rig:
    """The control app, three enrolled agents, a person, and the loop with a fake model."""

    def __init__(self, tmp_path) -> None:
        self.tmp = tmp_path
        env = self.env = Env(tmp_path)
        self.app = create_app(env.state)
        self.app.state.review_policy = ReviewPolicy()
        self.http = TestClient(self.app)
        self.client_http = {}
        for name, actions in {
            "loop": ["review:submit", "review:read"],
            "steerer": ["review:read", "loop:pause"],
            "reviewer": ["review:findings", "review:read"],
        }.items():
            if name == "reviewer":
                # Another person (enrolment root) than the loop that submitted the jobs.
                second_root_agent(env, f"synthetic-{name}-token-" + "x" * 30, actions, DESTINATION)
            else:
                env.agent(actions, name)
        owner = {
            "Authorization": (env.state / "owner.header").read_text().split(": ", 1)[1].strip()
        }
        enrol_person(self.http, owner, "synthetic-human-token-" + "x" * 30)
        self.loop_server = self._server("loop", review_tools=True)
        self.marker = tmp_path / "loop-kill"  # OPERATOR: the kill-switch marker file
        self.ledger_path = tmp_path / "loop-calls.sqlite"
        self.steer = self._server(
            "steerer",
            review_tools=True,
            agent_loop_marker=self.marker,
            agent_loop_ledger=self.ledger_path,
        )
        self.decider = self._server("human", review_decision_tool=True)
        self.events: list[dict] = []
        self.clock = [1000.0]
        self.model_calls: list[dict] = []
        self.model_dirs = 0
        self.planner = AutoJobPlanner(
            AutoJobPolicy(
                project_id=env.project["id"],
                classes=DEFAULT_JOB_CLASSES,
                tier="sonnet",
                per_job_cap_cents=100,
                max_jobs_total=8,
                max_total_budget_cents=800,
                destination_id=JOB_DESTINATION,
                prompt_digests=dict.fromkeys(DEFAULT_JOB_CLASSES, PROMPT_DIGEST),
            ),
            AutoJobLedger(tmp_path / "auto-jobs.sqlite"),
            _McpSubmitter(self.loop_server),
        )
        self.loop = StandingLoop(
            LoopPolicy(
                policy_digest="sha256:" + "c" * 64,
                identity_ref=CREDENTIAL,
                model="anthropic/claude-sonnet-4-5",
                tier="sonnet",
                max_calls_per_hour=20,
                max_consecutive_failures=2,
                base_backoff_seconds=10,
                max_backoff_seconds=60,
            ),
            KillSwitch(self.marker),
            self._observe,
            self._start_call,
            self.events.append,
            lambda: self.clock[0],
            environ={},  # no API-key variable (ADR 0082, ADR 0085)
            ledger=SqliteCallLedger(self.ledger_path),
        )

    # -- MCP clients -----------------------------------------------------------------------
    def _server(self, name, **flags):
        reference = self.tmp / f"{name}.token"
        reference.write_text(f"synthetic-{name}-token-" + "x" * 30)
        reference.chmod(0o600)
        client = ControlClient(
            Config("http://127.0.0.1:8765", reference, self.env.project["id"], DESTINATION),
            httpx.ASGITransport(app=self.app),
        )
        return create_server(client, **flags)

    def headers(self, name):
        return {
            "Authorization": f"Bearer synthetic-{name}-token-" + "x" * 30,
            "X-Agent-Destination": DESTINATION,
        }

    # -- the loop's two seams --------------------------------------------------------------
    def _observe(self):
        """Work items: queued jobs, as the client's own list tool shows them."""
        listing = _tool(self.loop_server, "tessera_list_review_jobs", limit=100)
        return [
            {"id": job["id"], "kind": job["kind"], "tier": "sonnet"}
            for job in listing["jobs"]
            if job["state"] == "queued"
        ]

    def _start_call(self, item, *, model, tier, identity_ref, policy_digest):
        """The fake model call: the real supervisor over a fake harness writes the receipt."""
        self.model_dirs += 1
        directory = self.tmp / f"model-{self.model_dirs}"
        directory.mkdir()
        receipt = fake_model_call(directory)
        self.model_calls.append(
            {"item": item["id"], "model": model, "tier": tier, "receipt": receipt}
        )


class _McpSubmitter:
    """The loop's ResearchJobSubmitter: its own enrolled agent credential, over the MCP tool."""

    def __init__(self, server) -> None:
        self.server = server

    def submit(self, project_id, key, payload):
        try:
            return _tool(
                self.server, "tessera_submit_review_job", payload=payload, idempotency_key=key
            )
        except ToolError as error:
            if "HTTP 409" in str(error):  # the tool never relays a denial body (client.py)
                raise ReviewSubmitRefused("refused") from None
            raise


def _trigger(rig, trigger_id, event="admitted"):
    env = rig.env
    return PlanTrigger(
        trigger_id=trigger_id,
        event=event,
        subject_type="plan",
        subject_id="plan-1",
        subject_digest="d" * 64,
        inputs=((env.snapshot_id, env.snapshot_digest, "public"),),
    )


def _findings(job):
    return [
        {
            "id": f"{job['kind']}-1",
            "severity": "major",
            "category": "design",
            "claim": f"synthetic {job['kind']} concern",
            "evidence": [{"source": "subject", "digest": "a" * 64}],
            "confidence": 0.6,
        },
        {
            "id": f"{job['kind']}-2",
            "severity": "info",
            "category": "style",
            "claim": f"synthetic {job['kind']} remark",
            "evidence": [],
            "confidence": 0.3,
        },
    ]


def test_the_research_loop_is_steered_over_tessera_mcp(tmp_path):
    rig = Rig(tmp_path)
    env = rig.env
    project = env.project["id"]

    # -- a plan is admitted: the loop creates its research jobs, once per trigger ------------
    first = rig.planner.on_trigger(_trigger(rig, "trigger-admit"))
    assert {r["status"] for r in first} == {"created"}, first
    again = rig.planner.on_trigger(_trigger(rig, "trigger-admit"))  # a replay creates nothing new
    assert {r["status"] for r in again} == {"existing"}
    listing = _tool(rig.steer, "tessera_list_review_jobs", limit=100)
    kinds = sorted(job["kind"] for job in listing["jobs"])
    assert kinds == sorted(DEFAULT_JOB_CLASSES)
    assert {job["state"] for job in listing["jobs"]} == {"queued"}
    # Egress invariants (ADR 0047, 0084): every job names the one enrolled model destination and
    # an egress of none or model_runtime; control derived each input's class itself.
    assert {job["destination_id"] for job in listing["jobs"]} == {JOB_DESTINATION}
    assert {job["egress"] for job in listing["jobs"]} <= {"none", "model_runtime"}
    assert all(job["data_classes"] for job in listing["jobs"])
    one = _tool(rig.steer, "tessera_read_review_job", job_id=listing["jobs"][0]["id"])
    assert one["advisory"] is True and one["execution_authorized"] is False
    assert one["receipt_digest"] is None  # nothing has run: no receipt is claimed
    # The jobs were submitted by the loop's own identity, not the steering client's.
    submitters = {job["submitter_principal_id"] for job in listing["jobs"]}
    assert len(submitters) == 1

    # -- the plan advances: a new trigger creates a new set; the cap bounds the total ----------
    advanced = rig.planner.on_trigger(_trigger(rig, "trigger-advance", event="advanced"))
    assert {r["status"] for r in advanced} == {"created"}
    assert len(_tool(rig.steer, "tessera_list_review_jobs", limit=100)["jobs"]) == 8
    capped = rig.planner.on_trigger(_trigger(rig, "trigger-third", event="advanced"))
    assert {r["reason"] for r in capped} == {"cap_exhausted"}  # a recorded skip, never topped up
    assert len(_tool(rig.steer, "tessera_list_review_jobs", limit=100)["jobs"]) == 8

    # -- pause the loop from the client: the kill switch holds every new model call ----------
    status = _tool(rig.steer, "tessera_agent_loop_status")
    assert status["engaged"] is False and status["halted"] is False
    # A credential without `loop:pause` is refused by the platform and creates no marker.
    other_marker = tmp_path / "other-kill"
    unscoped = rig._server("reviewer", agent_loop_marker=other_marker)
    with pytest.raises(ToolError, match="HTTP 403"):
        _tool(unscoped, "tessera_pause_agent_loop", reason="no scope")
    assert not other_marker.exists()
    paused = _tool(rig.steer, "tessera_pause_agent_loop", reason="client pauses before review")
    assert paused["engaged"] is True
    # The pause is a recorded platform act, not only a marker file: one control event names the
    # calling principal and credential, with the reason's digest (ADR 0089).
    record = paused["recorded"]
    assert record["event"] == "agent_loop.paused" and record["cursor"]
    (event,) = rig.env.query("SELECT payload_json FROM events WHERE event_type='agent_loop.paused'")
    payload = json.loads(event["payload_json"])
    assert payload["reason_sha256"] == hashlib.sha256(b"client pauses before review").hexdigest()
    assert (
        payload["principal_id"] == record["principal_id"] and payload["credential_kind"] == "agent"
    )
    listed = rig.http.get(
        f"/projects/{project}/agent-loop/pauses", headers=rig.headers("steerer")
    ).json()
    assert [p["cursor"] for p in listed["pauses"]] == [record["cursor"]]
    assert _tool(rig.steer, "tessera_agent_loop_status")["reason"] == "marker_present"
    held = rig.loop.tick()
    assert held["skipped"] == "kill_switch" and not rig.model_calls
    assert {
        j["state"] for j in _tool(rig.steer, "tessera_list_review_jobs", limit=100)["jobs"]
    } == {"queued"}
    # There is deliberately no resume tool: a pause is lifted by a person (ADR 0089).
    assert not {t.name for t in asyncio.run(rig.steer.list_tools())} & {
        "tessera_resume_agent_loop",
        "tessera_unpause_agent_loop",
    }
    rig.marker.unlink()  # OPERATOR lifts the pause
    assert _tool(rig.steer, "tessera_agent_loop_status")["engaged"] is False

    # -- the loop runs one job at a time; the fake model call writes a real receipt -----------
    ran = []
    for _ in range(3):
        tick = rig.loop.tick()
        assert tick["started"], (tick, rig.events[-4:])
        ran.append(tick["started"])
    assert len(set(ran)) == 3 and len(rig.model_calls) == 3
    for call in rig.model_calls:
        receipt = call["receipt"]
        assert receipt["model_configured"] == "anthropic/claude-sonnet-4-5"
        assert receipt["tier"] == "sonnet" and receipt["tokens"]
        # Metadata only: neither the prompt text nor the credential reference is in the receipt.
        assert PROMPT not in json.dumps(receipt) and API_KEY not in json.dumps(receipt)
    names = [e["event"] for e in rig.events]
    assert names.count("call_started") == 3 and names.count("call_finished") == 3
    assert names.count("kill_switch") == 1  # the pause was journaled once, not once per tick
    assert not any("prompt" in json.dumps(e) for e in rig.events)  # ids and digests only

    # -- the reviewer identity ingests findings with the receipt digest ------------------------
    ingested = {}
    for call in rig.model_calls:
        job = _tool(rig.steer, "tessera_read_review_job", job_id=call["item"])
        receipt_digest = canonical_digest(call["receipt"])
        response = rig.http.post(
            f"/review-jobs/{job['id']}/findings",
            headers={**rig.headers("reviewer"), "Idempotency-Key": f"ingest-{job['id']}"},
            json={
                "findings": _findings(job),
                "review_md_digest": hashlib.sha256(job["id"].encode()).hexdigest(),
                "receipt_digest": receipt_digest,
            },
        )
        assert response.status_code == 201, response.text
        ingested[job["id"]] = receipt_digest
    # The loop (the submitter) cannot ingest findings for its own job.
    victim = next(iter(ingested))
    refused = rig.http.post(
        f"/review-jobs/{victim}/findings",
        headers={**rig.headers("loop"), "Idempotency-Key": "self-ingest"},
        json={"findings": [], "receipt_digest": None},
    )
    assert refused.status_code == 403

    # -- the client lists and reads: jobs completed with receipts, findings open ---------------
    done = [
        j
        for j in _tool(rig.steer, "tessera_list_review_jobs", limit=100)["jobs"]
        if j["state"] == "completed"
    ]
    assert {j["id"] for j in done} == set(ingested)
    for job in done:
        assert job["receipt_digest"] == ingested[job["id"]]
    open_findings = _tool(rig.steer, "tessera_list_review_findings", state="open", limit=100)
    assert len(open_findings["findings"]) == 6
    one_finding = _tool(
        rig.steer, "tessera_read_review_finding", finding_id=open_findings["findings"][0]["id"]
    )
    assert one_finding["untrusted_text"] is True and one_finding["advisory"] is True

    # -- accept and reject are a person's: the agent tools cannot, the decision server can ------
    assert "tessera_resolve_review_finding" not in {
        t.name for t in asyncio.run(rig.steer.list_tools())
    }
    probe = open_findings["findings"][0]
    for who in ("steerer", "reviewer", "loop"):  # no agent credential may resolve
        denied = rig.http.post(
            f"/review-findings/{probe['id']}/resolve",
            headers={**rig.headers(who), "Idempotency-Key": f"deny-{who}"},
            json={
                "decision": "accept",
                "rationale": "agent tries",
                "expected_revision": probe["revision"],
                "expected_digest": probe["digest"],
            },
        )
        assert denied.status_code == 403, who
    by_id = {f["id"]: f for f in open_findings["findings"]}
    accept, reject = list(by_id.values())[:2]
    for finding, decision in ((accept, "accept"), (reject, "reject")):
        resolved = _tool(
            rig.decider,
            "tessera_resolve_review_finding",
            finding_id=finding["id"],
            decision=decision,
            rationale=f"HUMAN: {decision} after reading the evidence",
            expected_revision=finding["revision"],
            expected_digest=finding["digest"],
        )
        assert resolved["state"] in {"accepted", "rejected"}
    states = {
        f["id"]: f["state"]
        for f in _tool(rig.steer, "tessera_list_review_findings", limit=100)["findings"]
    }
    assert states[accept["id"]] == "accepted" and states[reject["id"]] == "rejected"
    assert sum(1 for s in states.values() if s == "open") == 4  # the rest stay proposals
    assert project  # the whole flow stayed inside one project


def test_an_uncertain_model_call_is_not_retried_and_no_receipt_is_claimed(tmp_path):
    """A start with no confirmed finish may have been accepted and paid: the loop never repeats it,
    the job stays queued with no receipt, and the client sees exactly that."""
    rig = Rig(tmp_path)
    rig.planner.on_trigger(_trigger(rig, "trigger-fault"))

    def broken(item, **_):
        raise RuntimeError("connection reset after send")  # outcome unknown

    rig.loop.start_call = broken
    first = rig.loop.tick()
    assert len(first["uncertain"]) == 1
    uncertain = first["uncertain"][0]
    assert first["started"] == uncertain  # the one attempt that may have been accepted
    rig.loop.tick()
    rig.loop.tick()
    assert rig.loop.tick()["skipped"] in {"uncertain_outcome", "halted"}
    seen = _tool(rig.steer, "tessera_agent_loop_status")
    assert seen["halted"] is True and uncertain in seen["uncertain_item_ids"]
    jobs = {j["id"]: j for j in _tool(rig.steer, "tessera_list_review_jobs", limit=100)["jobs"]}
    assert jobs[uncertain]["state"] == "queued" and jobs[uncertain]["receipt_digest"] is None
    assert [e for e in rig.events if e["event"] == "uncertain_outcome"]
    assert not rig.model_calls  # nothing is recorded as having run
    # OPERATOR: only an operator's resolution clears it; no tool does.
    rig.loop.resolve_uncertain(uncertain, "not_started")


def _planner(rig, **policy):
    base = dict(
        project_id=rig.env.project["id"],
        classes=("plan_review",),
        tier="sonnet",
        per_job_cap_cents=100,
        max_jobs_total=8,
        max_total_budget_cents=800,
        destination_id=JOB_DESTINATION,
        prompt_digests=dict.fromkeys(DEFAULT_JOB_CLASSES, PROMPT_DIGEST),
    )
    return AutoJobPlanner(
        AutoJobPolicy(**{**base, **policy}),
        AutoJobLedger(rig.tmp / f"auto-{len(policy)}-{id(policy)}.sqlite"),
        _McpSubmitter(rig.loop_server),
    )


def test_the_loop_cannot_route_a_job_to_an_unregistered_destination_or_mislabel_a_class(tmp_path):
    """Control, not the loop, decides what may leave: an unenrolled destination is refused, and a
    trigger that labels a snapshot `public` does not change the class control derives for it."""
    rig = Rig(tmp_path)
    trigger = _trigger(rig, "trigger-egress")  # labels its input `public`
    # 1. An unregistered destination creates nothing and is a recorded, final skip.
    refused = _planner(rig, destination_id="unenrolled-destination").on_trigger(trigger)
    assert [r["status"] for r in refused] == ["skipped"], refused
    assert not _tool(rig.steer, "tessera_list_review_jobs", limit=100)["jobs"]
    # 2. The derived class wins. A private snapshot the trigger labels `public` is still
    # `private_derived` to control, so the cheap tier refuses it and nothing is created.
    private_id, private_digest = "snapshot-private", "9" * 64
    seed_private_snapshot(
        rig.env.state / "tessera.sqlite3", rig.env.project["id"], private_id, private_digest
    )
    mislabelled = PlanTrigger(
        trigger_id="trigger-mislabel",
        event="admitted",
        subject_type="plan",
        subject_id="plan-1",
        subject_digest="d" * 64,
        inputs=((private_id, private_digest, "public"),),
    )
    cheap = _planner(rig, tier="haiku").on_trigger(mislabelled)
    assert [r["status"] for r in cheap] == ["skipped"], cheap
    assert not _tool(rig.steer, "tessera_list_review_jobs", limit=100)["jobs"]
    # 3. On an allowed tier the job is created, and the record shows the class control derived
    # (not the trigger's label); egress stays inside the closed set, to the enrolled destination.
    allowed = _planner(rig, tier="sonnet").on_trigger(mislabelled)
    assert [r["status"] for r in allowed] == ["created"], allowed
    (job,) = _tool(rig.steer, "tessera_list_review_jobs", limit=100)["jobs"]
    assert job["data_classes"] == ["private_derived"]
    assert job["destination_id"] == JOB_DESTINATION and job["egress"] in {"none", "model_runtime"}
    assert job["execution_authorized"] is False


# Known gaps (pinned or worked around above, not hidden):
# - The loop's work source (queued jobs), its model call and its submitter are written here; no
#   code in the tree connects the loop to the control API.
# - Nothing runs a review job: the runner seams have no route and no code turns model output into
#   findings, so the test supplies the findings; ingest has no MCP tool (a reviewer posts over
#   HTTP) and the stored receipt digest is not verified against the model-call receipt.
# - The kill switch can be engaged from a client but not lifted (a person's act, ADR 0089).
# - The model destination is Anthropic called directly by API-key reference (ADR 0082); the
#   transport is faked, the supervisor, store and adapter are real.
# - A refused submit shows only "HTTP 409": the tool never relays a denial body.
