"""Isolated API-only research reviews. No models, providers or execution effects."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tessera_control.api import create_app
from tessera_control.program_models import OUTCOME_CONDITIONS
from tessera_control.store import Store, initialize_state


class ProgramJourney:
    def __init__(self, tmp_path, execution=None):
        self.state = execution.state if execution else tmp_path / "state"
        if execution is None:
            initialize_state(self.state)
        self.client = TestClient(create_app(self.state))
        self.owner = (self.state / "owner.header").read_text().strip().split(": ", 1)[1]
        self.actor = Store(self.state).authenticate(self.owner.removeprefix("Bearer "))
        self.project = (
            execution.project
            if execution
            else self.post("/projects", {"name": "Synthetic MixFM requirements fixture"})
        )
        self.study = self.post(
            f"/projects/{self.project['id']}/studies",
            {
                "purpose": "Outcome review, not a scientific experiment",
                "protocol": "Synthetic fixture",
            },
        )
        self.source = self.post(
            f"/studies/{self.study['id']}/sources",
            {
                "uri": "https://example.test/synthetic",
                "quoted_passage": "Fixture source only",
                "content_digest": "a" * 64,
                "locator": "fixture:1",
            },
        )
        self.variant = self.post(
            f"/studies/{self.study['id']}/variants",
            {
                "name": "Synthetic registered condition",
                "method_digest": "b" * 64,
                "input_digest": "c" * 64,
                "metadata": {
                    "synthetic": True,
                    **({"experiment_id": execution.experiment["id"]} if execution else {}),
                },
            },
        )
        self.agent = "Bearer " + "synthetic_agent_" + "x" * 48
        self.post(
            "/credentials",
            {
                "token": self.agent.removeprefix("Bearer "),
                "kind": "agent",
                "audience": "control",
                "project_id": self.project["id"],
                "expires_at": self.expiry(),
            },
        )

    @staticmethod
    def expiry():
        return (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    def post(self, path, body, *, status=None, token=None, key=None):
        response = self.client.post(
            path,
            json=body,
            headers={
                "Authorization": token or self.owner,
                "Idempotency-Key": key or uuid4().hex,
            },
        )
        if status is None:
            assert response.status_code in (200, 201), response.text
        else:
            assert response.status_code == status, response.text
        return response.json()

    def context(self, token=None):
        response = self.client.get(
            f"/interaction/studies/{self.study['id']}/program-context",
            headers={"Authorization": token or self.owner},
        )
        assert response.status_code == 200, response.text
        return response.json()

    def scope(self, action="plan", outcome="outcome_0", **changes):
        context = self.context()
        prior = [
            r
            for r in context["reviews"]
            if r["scope"]["outcome"] == outcome and r["scope"]["action"] == action
        ]
        scope = {
            "type": "research_program",
            "version": 1,
            "study_id": self.study["id"],
            "outcome": outcome,
            "action": action,
            "basis_digest": context["basis_digest"],
            "supersedes_id": prior[-1]["id"] if prior else None,
            "reason": "Explicit fixture proposal",
        }
        if action == "plan":
            scope.update(
                brief={
                    "objective": "Test specialist transfer",
                    "hypothesis": "Transfer may help OOD",
                    "success_gate": "Prespecified OOD gain with retained capability",
                    "failure_gate": "No gain over baseline",
                    "stop_gate": "Stop at declared resource cap",
                    "target_domain": "Molecules",
                    "provenance_policy": "Record rights, hashes and uncertainty",
                    "data_source_ids": [self.source["id"]],
                    "compute": {
                        "budget_cents": 0,
                        "max_wall_seconds": 60,
                        "max_gpu_hours": 0.0,
                        "target": "salad_gpu_future",
                        "resources": "Unimplemented target; planning only",
                    },
                    "dataset_recommendation": "Fixture only, not a model recommendation",
                    "representation_recommendation": "Compare representations with fixed data",
                    "split_recommendation": "Prespecify isolated holdouts",
                    "metric_recommendation": "Record uncertainty and retention alongside accuracy",
                    "uncertainty": "No scientific result exists",
                    "missing_prerequisites": [],
                },
                conditions={
                    name: {
                        "disposition": "selected" if index == 0 else "deferred",
                        "rationale": "Bound this first comparison; deferred is not tested",
                        "variant_ids": [self.variant["id"]] if index == 0 else [],
                    }
                    for index, name in enumerate(OUTCOME_CONDITIONS[outcome])
                },
            )
        if action == "source_use":
            scope.update(
                source_ids=[self.source["id"]], intended_use="Fixture-only review; no transfer"
            )
        if action == "go_no_go":
            scope.update(recommendation="no_go", finding_ids=[])
        scope.update(changes)
        return scope

    def propose(self, scope=None, **kwargs):
        scope = scope or self.scope()
        return self.post(
            "/interaction/decisions",
            {
                "project_id": self.project["id"],
                "kind": "access" if scope["action"] == "source_use" else "direction",
                "scope": scope,
                "expires_at": self.expiry(),
                "reviewer_principal_id": self.actor["id"],
            },
            **kwargs,
        )

    def resolve(self, proposal, decision="approve", **kwargs):
        return self.post(
            "/interaction/decisions/resolve",
            {
                "decision_id": proposal["id"],
                "expected_revision": proposal["revision"],
                "decision": decision,
                "rationale": "Human fixture review; no scientific validity claim",
            },
            **kwargs,
        )

    def assess(self):
        return self.post(
            f"/sources/{self.source['id']}/assess",
            {
                "expected_revision": 1,
                "verdict": "supported",
                "reason": "Fixture support only",
            },
        )

    def finding(self, completed=True):
        if completed:
            self.post(
                f"/variants/{self.variant['id']}/record",
                {
                    "expected_revision": 1,
                    "status": "completed",
                    "outcome": {"fixture": True},
                },
            )
        finding = self.post(
            f"/studies/{self.study['id']}/findings",
            {
                "statement": "Synthetic human interpretation only",
                "source_ids": [self.source["id"]],
                "variant_ids": [self.variant["id"]],
            },
        )
        return self.post(f"/findings/{finding['id']}/accept", {"expected_revision": 1})


@pytest.fixture
def journey(tmp_path):
    return ProgramJourney(tmp_path)


@pytest.mark.parametrize("outcome", OUTCOME_CONDITIONS)
def test_agent_proposal_human_version_review_and_restart(journey, outcome):
    j = journey
    assert j.context(j.agent)["execution_granted"] is False
    scope = j.scope(outcome=outcome)
    proposal = j.propose(scope, token=j.agent, key="proposal")
    assert proposal["status"] == "pending"
    j.resolve(proposal, token=j.agent, status=403)
    assert j.resolve(proposal)["status"] == "approved"
    replacement_scope = j.scope(outcome=outcome)
    replacement_scope["brief"]["hypothesis"] = "Narrower human-edited hypothesis"
    replacement = j.propose(replacement_scope)
    # Merely suggesting a replacement cannot reverse the approved review.
    assert next(r for r in j.context()["reviews"] if r["id"] == proposal["id"])[
        "is_effective_review"
    ]
    j.resolve(replacement)
    context = j.context()
    assert [r["id"] for r in context["reviews"] if r["is_effective_review"]] == [replacement["id"]]
    assert context["salad_adapter"]["supported"] is False
    j.client = TestClient(create_app(j.state))
    assert j.context() == context
    assert context["basis"]["variants"][0]["outcome_json"] is None


def test_stale_basis_lineage_revision_and_agent_cannot_approve_sources(journey):
    j = journey
    first = j.propose()
    second = j.propose(j.scope())
    j.resolve(first, status=409)
    j.resolve({**second, "revision": 9}, status=409)
    j.assess()
    retained = next(r for r in j.context()["reviews"] if r["id"] == first["id"])
    assert retained["basis_snapshot"]["sources"][0]["verdict"] == "unverified"
    assert j.context()["basis"]["sources"][0]["verdict"] == "supported"
    j.resolve(second, status=409)  # source revision changed after the panel was opened
    fresh = j.propose(j.scope())
    j.resolve(fresh, "reject")
    sources = j.propose(j.scope("source_use"), token=j.agent)
    j.resolve(sources, token=j.agent, status=403)
    assert j.resolve(sources)["status"] == "approved"


def test_missing_source_support_and_incomplete_condition_accounting(journey):
    j = journey
    unsupported = j.propose(j.scope("source_use"))
    assert "supported" in j.resolve(unsupported, status=409)["detail"]
    missing_condition = j.scope()
    del missing_condition["conditions"]["scaffold_ood"]
    j.propose(missing_condition, status=422)
    missing_source = j.scope()
    missing_source["brief"]["data_source_ids"] = ["source_missing"]
    j.propose(missing_source, status=404)
    extra = j.post(
        f"/studies/{j.study['id']}/variants",
        {
            "name": "Unaccounted",
            "method_digest": "a" * 64,
            "input_digest": "b" * 64,
            "metadata": {},
        },
    )
    assert extra["status"] == "registered"
    assert "every registered" in j.propose(j.scope(), status=409)["detail"]


def test_go_no_go_reversal_retraction_and_no_implicit_execution(journey):
    j = journey
    j.assess()
    finding = j.finding()
    j.resolve(j.propose())
    j.resolve(j.propose(j.scope("source_use")))
    go = j.propose(j.scope("go_no_go", recommendation="go", finding_ids=[finding["id"]]))
    j.resolve(go)
    no_go = j.propose(j.scope("go_no_go"), token=j.agent)
    assert next(r for r in j.context()["reviews"] if r["id"] == go["id"])["is_effective_review"]
    j.resolve(no_go)
    assert not next(r for r in j.context()["reviews"] if r["id"] == go["id"])["is_effective_review"]
    j.post(
        f"/findings/{finding['id']}/retract",
        {
            "expected_revision": finding["revision"],
            "reason": "Fixture interpretation withdrawn",
        },
    )
    j.resolve(j.propose(j.scope("source_use")))
    retracted_go = j.propose(j.scope("go_no_go", recommendation="go", finding_ids=[finding["id"]]))
    assert "accepted findings" in j.resolve(retracted_go, status=409)["detail"]
    assert all(
        not r["is_effective_review"]
        for r in j.context()["reviews"]
        if r["scope"]["action"] == "go_no_go"
    )
    assert j.context()["execution_granted"] is False


def test_go_rejects_unaccounted_variants_but_no_go_can_stop_early(journey):
    j = journey
    j.assess()
    finding = j.finding(completed=False)
    j.resolve(j.propose())
    j.resolve(j.propose(j.scope("source_use")))
    go = j.propose(j.scope("go_no_go", recommendation="go", finding_ids=[finding["id"]]))
    assert "variant accounting" in j.resolve(go, status=409)["detail"]
    assert j.resolve(j.propose(j.scope("go_no_go")))["status"] == "approved"


def test_cross_project_reference_and_context_denied(journey):
    j = journey
    foreign = j.post("/projects", {"name": "Other project"})
    study = j.post(f"/projects/{foreign['id']}/studies", {"purpose": "Other", "protocol": "Other"})
    path = f"/interaction/studies/{study['id']}/program-context"
    assert j.client.get(path, headers={"Authorization": j.agent}).status_code == 404
    response = j.client.get(path, headers={"Authorization": j.owner}).json()
    scope = j.scope("go_no_go", study_id=study["id"], basis_digest=response["basis_digest"])
    j.propose(scope, status=404)


def test_missing_prerequisite_and_rationale_are_not_silently_approved(journey):
    j = journey
    j.assess()
    finding = j.finding()
    scope = j.scope()
    scope["brief"]["missing_prerequisites"] = ["No usable training dataset"]
    plan = j.propose(scope)
    j.post(
        "/interaction/decisions/resolve",
        {
            "decision_id": plan["id"],
            "expected_revision": 1,
            "decision": "approve",
        },
        status=409,
    )
    j.resolve(plan)  # reviewing a blocked plan is allowed and remains non-executable
    j.resolve(j.propose(j.scope("source_use")))
    go = j.propose(j.scope("go_no_go", recommendation="go", finding_ids=[finding["id"]]))
    assert "prerequisites" in j.resolve(go, status=409)["detail"]


def test_pending_dedup_and_replay_do_not_duplicate_history(journey):
    j = journey
    scope = j.scope()
    proposal = j.propose(scope)
    assert j.propose(deepcopy(scope)) == proposal
    approved = j.resolve(proposal, key="resolve")
    assert j.resolve(proposal, key="resolve") == approved
    assert len(j.context()["reviews"]) == 1


@pytest.mark.parametrize("outcome", ["failed", "unknown"])
def test_linked_execution_failure_retained_unknown_blocks_go(
    tmp_path, monkeypatch, capsys, outcome
):
    from test_local_api_workflow import Journey, complete

    execution = Journey(tmp_path, monkeypatch, capsys)
    for kind, result in (("baseline", "succeeded"), ("intervention", outcome)):
        grant, _, attempt_id, _, fake = execution.admit(kind)
        execution.run(fake)
        if result != "unknown":
            complete(fake, result)
            execution.run(fake)
        else:
            execution.call(
                "POST",
                f"/execution-grants/{grant['id']}/revoke",
                {
                    "expected_revision": grant["revision"],
                },
            )

            def unavailable_delete(_name, _uid):
                raise ConnectionError("Synthetic unknown termination")

            fake.delete = unavailable_delete
            execution.run(fake)
        execution.resolve(attempt_id, result)
    j = ProgramJourney(tmp_path, execution=execution)
    j.assess()
    finding = j.finding()
    j.resolve(j.propose())
    j.resolve(j.propose(j.scope("source_use")))
    go = j.propose(j.scope("go_no_go", recommendation="go", finding_ids=[finding["id"]]))
    if outcome == "unknown":
        assert "unknown linked execution" in j.resolve(go, status=409)["detail"]
        j.resolve(j.propose(j.scope("go_no_go")))
    else:
        j.resolve(go)  # A failed process does not prevent an explicit human direction.
    context = j.context()
    assert {a["outcome"] for a in context["basis"]["executions"][0]["admissions"]} == {
        "succeeded",
        outcome,
    }
    assert context["scientific_validity"] == "not_assessed"
