"""Read-only agent result summaries; no authority is opened by results:read/budget:read."""

import secrets
import sqlite3

import pytest
from test_agent_authority import admit_payload, enroll, issue, post, prepared  # noqa: F401

from tessera_control import agent_research_results as results
from tessera_control.models import ExternalAgentScope

ALL = [
    "context:read", "proposal:create", "proposal:read", "authority:request",
    "authority:consume", "observation:create", "review:request", "notebook:read",
    "provenance:append", "results:read", "budget:read",
]  # fmt: skip


def _experiment_and_variant(state):
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        return db.execute(
            "SELECT e.id,v.id FROM research_experiments e JOIN research_experiment_variants v "
            "ON v.experiment_id=e.id WHERE v.kind='baseline'"
        ).fetchone()


@pytest.fixture
def world(prepared):  # noqa: F811
    state, client, owner, agent, project, bounds, _ = prepared
    grant = issue(prepared)
    post(
        client, agent, f"/agent-authority-grants/{grant['id']}/consume", admit_payload(bounds),
        key="admit",
    )  # fmt: skip
    experiment, variant = _experiment_and_variant(state)
    attempt = post(
        client, owner, f"/experiment-variants/{variant}/attempts",
        {"expected_variant_revision": 1, "provenance_kind": "human_reported_observation",
         "human_observation": "ran by hand", "outcome": "succeeded"},
    )  # fmt: skip
    post(
        client, owner, f"/experiment-attempts/{attempt['id']}/measurements",
        {"expected_revision": attempt["revision"], "name": "seconds", "value": {"v": 3}},
    )  # fmt: skip
    reader, _ = enroll(client, owner, project, ["results:read"])
    full, _ = enroll(client, owner, project, ALL)
    return client, owner, project, experiment, reader, full, state


def _get(client, headers, project, experiment):
    return client.get(
        f"/projects/{project}/agent-results/experiments/{experiment}", headers=headers
    )


def test_agent_reads_experiment_results_with_budget_withheld(world):
    client, _, project, experiment, reader, full, _ = world
    body = _get(client, reader, project, experiment).json()
    assert body["authority_effect"] == "none"
    assert body["scientific_validity"] == "not_assessed"
    sections = body["sections"]
    assert sections["attempts"]["admitted"] and sections["attempts"]["recorded"]
    measurement = sections["measurements"]["measurements"][0]
    assert measurement["value"] == {"v": 3} and "not verified" in sections["measurements"]["label"]
    assert body["withheld"] == [{"section": "budget", "required_scope": "budget:read"}]
    assert "budget" not in sections
    assert "principal" not in str(body).lower()
    with_budget = _get(client, full, project, experiment).json()
    assert with_budget["sections"]["budget"] == {"authorized": False}
    assert with_budget["withheld"] == []


def test_owner_reads_full_summary(world):
    client, owner, project, experiment, *_ = world
    body = _get(client, owner, project, experiment).json()
    assert body["withheld"] == [] and "budget" in body["sections"]


def test_scope_destination_and_tenant_checks(world):
    client, owner, project, experiment, reader, _, _ = world
    no_scope, _ = enroll(client, owner, project, ["notebook:read"])
    assert _get(client, no_scope, project, experiment).status_code == 403
    wrong = {**reader, "X-Agent-Destination": "elsewhere"}
    assert _get(client, wrong, project, experiment).status_code == 403
    other = post(client, owner, "/projects", {"name": "other"})
    assert _get(client, owner, other["id"], experiment).status_code == 404
    assert _get(client, reader, other["id"], experiment).status_code == 404
    assert _get(client, reader, project, "missing").status_code == 404


def test_results_scopes_open_no_authority(world):
    client, _, project, experiment, _, full, state = world
    key = {"Idempotency-Key": secrets.token_hex(8)}
    digest = "a" * 64
    attempts = [
        ("post", f"/experiments/{experiment}/budget/authorization", {
            "expected_revision": 0, "ceiling_cents": 10, "expires_at": "2099-01-01T00:00:00Z",
            "evidence_digest": digest, "source_reference": "x"}),
        ("post", "/experiment-attempts/missing/measurements",
         {"expected_revision": 1, "name": "n", "value": {}}),
        ("post", "/agent-research-proposals/missing/promote",
         {"expected_revision": 1, "expected_digest": digest}),
        ("post", "/tasks/missing/approve", {"expected_revision": 1}),
        ("post", "/findings/missing/accept", {"expected_revision": 1}),
        ("post", "/result-checkpoints/missing/acceptances", {
            "expected_revision": 0, "action": "accept", "rationale": "r",
            "expected_checkpoint_digest": digest, "expected_manifest_digest": digest,
            "expected_validation_digest": digest, "expected_receipt_digest": digest,
            "expected_grant_revision": 1, "lease_id": "l", "operation_id": "a" * 32,
            "expected_epoch": 1}),
        ("get", f"/experiments/{experiment}/export", None),
        ("get", f"/experiments/{experiment}/budget", None),
    ]  # fmt: skip
    for method, path, body in attempts:
        kwargs = {"json": body} if body is not None else {}
        response = getattr(client, method)(path, headers={**full, **key}, **kwargs)
        assert response.status_code == 403, (path, response.status_code, response.text)


def test_program_summary_for_study_linking_experiment(world):
    client, owner, project, experiment, reader, full, _ = world
    study = post(client, owner, f"/projects/{project}/studies",
                 {"purpose": "p", "protocol": "q"})  # fmt: skip
    post(client, owner, f"/studies/{study['id']}/variants", {
        "name": "v", "method_digest": "a" * 64, "input_digest": "b" * 64,
        "metadata": {"experiment_id": experiment}})  # fmt: skip
    path = f"/projects/{project}/agent-results/studies/{study['id']}"
    body = client.get(path, headers=full).json()
    assert body["subject"]["kind"] == "program"
    assert body["sections"]["program_reviews"] == []
    linked = body["sections"]["experiments"][0]
    assert linked["experiment_id"] == experiment and "attempts" in linked["summary"]
    assert body["sections"]["budget"]["total_ceiling_cents"] == 0
    narrow = client.get(path, headers=reader).json()
    assert [w["section"] for w in narrow["withheld"]] == ["budget"]
    assert client.get(path, headers=owner).status_code == 200
    assert (
        client.get(f"/projects/{project}/agent-results/studies/nope", headers=reader).status_code
        == 404
    )


def test_registry_extension_and_duplicates(world):
    client, _, project, experiment, reader, _, _ = world
    section = results.ResultSection("custom", "experiment", "results:read",
                                    lambda c, a, s: {"seen": s["id"]})  # fmt: skip
    results.register_result_section(section)
    try:
        body = _get(client, reader, project, experiment).json()
        assert body["sections"]["custom"] == {"seen": experiment}
        with pytest.raises(ValueError):
            results.register_result_section(section)
    finally:
        results._REGISTRY.remove(section)
    with pytest.raises(ValueError):
        results.register_result_section(results._REGISTRY[0])


def test_scope_model_accepts_new_actions():
    assert ExternalAgentScope.model_validate({"actions": ALL, "destination": "d"})
