"""Executable SQLite/HTTP authority checks; no provider or cluster effects."""

import json
import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_execution import _grant_payload, _owner, _snapshot_release, _worker
from test_goal_to_decision import _design

from tessera_control.api import create_app
from tessera_control.execution import ExecutionStore
from tessera_control.experiments import ExperimentStore
from tessera_control.runtime_packages import RuntimePackageStore
from tessera_control.store import Store, canonical_digest, initialize_state, migrate_state


def post(client, actor, path, payload, key=None, expected=201):
    response = client.post(
        path, headers={**actor, "Idempotency-Key": key or secrets.token_hex(12)}, json=payload
    )
    assert response.status_code == expected, response.text
    return response.json()


def enroll(client, owner, project, scopes=None):
    token = secrets.token_urlsafe(32)
    result = post(
        client,
        owner,
        "/credentials",
        {
            "token": token,
            "kind": "agent",
            "audience": "control",
            "project_id": project,
            "expires_at": (datetime.now(UTC) + timedelta(hours=3)).isoformat(),
            "external_agent": {
                "destination": "openai:personal",
                "actions": scopes
                or [
                    "authority:request",
                    "authority:consume",
                    "proposal:create",
                    "proposal:read",
                    "observation:create",
                    "review:request",
                    "notebook:read",
                    "context:read",
                ],
            },
        },
    )
    return {"Authorization": "Bearer " + token, "X-Agent-Destination": "openai:personal"}, result


@pytest.fixture
def prepared(tmp_path):
    state = tmp_path / "state"
    initialize_state(state)
    store = Store(state)
    human = _owner(state)
    owner = {"Authorization": (state / "owner.header").read_text().strip().split(": ", 1)[1]}
    project = store.create_project(human, "project", "project", {"name": "bounded"})[1]
    worker = _worker(state, human, project["id"], "worker-token")
    snapshot, _ = _snapshot_release(state, human, project["id"], worker["id"])
    task = store.create_task(
        human,
        project["id"],
        "task",
        "task",
        {
            "title": "bounded",
            "purpose": "compare",
            "budget_cents": 100,
            "max_duration_seconds": 300,
        },
    )[1]
    plan = {
        "expected_revision": task["revision"],
        "title": "bounded",
        "purpose": "compare",
        "budget_cents": 100,
        "max_duration_seconds": 300,
        "execution_spec": {
            "argv": ["opencode", "serve"],
            "input_snapshot_digest": snapshot["digest"],
            "pinned_skill_digests": [],
            "output_paths": ["result.txt"],
            "profile": "kubernetes-opencode",
            "network_enabled": False,
            "max_output_bytes": 1024,
        },
    }
    task = store.revise_task_plan(human, task["id"], "plan", canonical_digest(plan), plan)[1]
    task = store.approve_task(
        human, task["id"], "approve", "approve", task["revision"], task["current_plan_digest"]
    )[1]
    execution = ExecutionStore(state)
    execution.configure_project_execution(
        human,
        project["id"],
        "limits",
        "limits",
        {
            "expected_revision": 0,
            "currency": "USD",
            "monetary_limit_cents": 100,
            "max_concurrency": 1,
        },
    )
    payload = _grant_payload(task, worker, {"id": snapshot["id"]})
    payload["max_exposure_cents"] = 100
    grant = execution.issue_execution_grant(
        human, project["id"], "execution", canonical_digest(payload), payload
    )[1]
    # Freeze agent authority after the ordinary worker lease has advanced its epoch.
    execution.acquire_execution_lease(worker, grant["id"], "prepared-lease", "prepared-lease")
    grant = execution.get_execution_grant(human, grant["id"])
    experiments = ExperimentStore(state)
    design = _design(grant=grant)
    experiment = experiments.create_experiment(
        human, project["id"], "design", canonical_digest(design), design
    )[1]
    variant = next(v for v in experiment["design"]["variants"] if v["kind"] == "baseline")
    client = TestClient(create_app(state))
    assert client.get("/health/ready").status_code == 200
    agent, credential = enroll(client, owner, project["id"])
    bounds = {
        "subject_credential_id": credential["credential_id"],
        "destination": "openai:personal",
        "model": "model-1",
        "purpose": "bounded approved experiment",
        "actions": [
            "attempt:admit",
            "attempt:dispatch",
            "attempt:status",
            "execution:cancel",
            "authority:delegate",
        ],
        "bindings": [
            {
                "variant_id": variant["id"],
                "expected_variant_revision": variant["revision"],
                "expected_design_revision": experiment["current_design_revision"],
                "execution_grant_id": grant["id"],
                "expected_execution_grant_revision": grant["revision"],
                "expected_execution_epoch": grant["epoch"],
                "task_id": task["id"],
                "expected_task_revision": task["revision"],
                "plan_digest": task["current_plan_digest"],
                "target_digest": "a" * 64,
            }
        ],
        "budget_cents": 100,
        "max_duration_seconds": 300,
        "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        "delegation_depth": 2,
    }
    return state, client, owner, agent, project["id"], bounds, worker


def issue(p, bounds=None):
    _, client, owner, _, project, default, _ = p
    return post(
        client, owner, f"/projects/{project}/agent-authority-grants", {"bounds": bounds or default}
    )


def admit_payload(bounds):
    b = bounds["bindings"][0]
    return {
        "expected_revision": 1,
        "expected_epoch": 1,
        "model": bounds["model"],
        "command": {
            "action": "attempt:admit",
            "variant_id": b["variant_id"],
            "expected_variant_revision": b["expected_variant_revision"],
            "expected_design_revision": b["expected_design_revision"],
            "execution_grant_id": b["execution_grant_id"],
        },
    }


def test_human_issue_agent_consume_atomic_replay_and_attribution(prepared):
    state, client, owner, agent, project, bounds, _ = prepared
    request = post(
        client, agent, f"/projects/{project}/agent-authority-requests", {"bounds": bounds}
    )
    assert request["authority_effect"] == "none"
    assert post(
        client,
        agent,
        f"/projects/{project}/agent-authority-grants",
        {"bounds": bounds},
        expected=403,
    )
    grant = issue(prepared)
    path = f"/agent-authority-grants/{grant['id']}/consume"
    payload = admit_payload(bounds)
    consumed = post(client, agent, path, payload, key="admit")
    assert post(client, agent, path, payload, key="admit") == consumed
    assert (
        client.get(f"/agent-authority-grants/{grant['id']}", headers=agent).json()[
            "remaining_budget_cents"
        ]
        == 0
    )
    changed = deepcopy(payload)
    changed["model"] = "changed"
    post(client, agent, path, changed, key="admit", expected=409)
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        principal = db.execute(
            "SELECT principal_id FROM credentials WHERE id=?", (bounds["subject_credential_id"],)
        ).fetchone()[0]
        assert db.execute("SELECT COUNT(*) FROM agent_authority_consumptions").fetchone()[0] == 1
        assert (
            db.execute(
                "SELECT created_by_principal_id FROM research_experiment_admitted_attempts"
            ).fetchone()[0]
            == principal
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM agent_provenance WHERE actor_principal_id=?", (principal,)
            ).fetchone()[0]
            >= 2
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='agent_authority.consumed'"
            ).fetchone()[0]
            == 1
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM agent_provenance p JOIN events e "
                "ON json_extract(p.envelope_json,'$.correlation_id')=e.correlation_id "
                "WHERE e.event_type='agent_authority.consumed'"
            ).fetchone()[0]
            == 1
        )
    assert consumed["result"]["operational_observed_state"] == "not_observed"


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "wrong"),
        ("expected_epoch", 2),
        ("expected_revision", 2),
        ("variant", "missing"),
        ("design", 2),
    ],
)
def test_consumption_fences_and_no_partial_state(prepared, field, value):
    state, client, _, agent, _, bounds, _ = prepared
    grant = issue(prepared)
    payload = admit_payload(bounds)
    if field == "variant":
        payload["command"]["variant_id"] = value
    elif field == "design":
        payload["command"]["expected_design_revision"] = value
    else:
        payload[field] = value
    post(
        client,
        agent,
        f"/agent-authority-grants/{grant['id']}/consume",
        payload,
        expected=403 if field == "model" else 409,
    )
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM agent_authority_consumptions").fetchone()[0] == 0
        assert (
            db.execute("SELECT COUNT(*) FROM research_experiment_admitted_attempts").fetchone()[0]
            == 0
        )
        assert db.execute("SELECT reserved_cents FROM agent_authority_grants").fetchone()[0] == 0


def test_budget_expiry_destination_scope_and_human_boundaries(prepared):
    state, client, owner, agent, project, bounds, _ = prepared
    poor = deepcopy(bounds)
    poor["budget_cents"] = 0
    grant = issue(prepared, poor)
    path = f"/agent-authority-grants/{grant['id']}/consume"
    post(client, agent, path, admit_payload(poor), expected=403)
    wrong = {**agent, "X-Agent-Destination": "elsewhere"}
    post(client, wrong, path, admit_payload(poor), expected=403)
    readonly, _ = enroll(client, owner, project, ["proposal:read"])
    post(client, readonly, path, admit_payload(poor), expected=403)
    post(
        client,
        agent,
        f"/agent-authority-grants/{grant['id']}/revoke",
        {"expected_revision": 1, "expected_epoch": 1},
        expected=403,
    )
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        poor["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        db.execute(
            "UPDATE agent_authority_grants SET bounds_json=? WHERE id=?",
            (json.dumps(poor), grant["id"]),
        )
    assert (
        client.get(f"/agent-authority-grants/{grant['id']}", headers=agent).json()[
            "effective_status"
        ]
        == "fenced"
    )
    post(client, agent, path, admit_payload(poor), expected=403)


def test_subset_delegation_reserves_budget_and_ancestor_revoke(prepared):
    _, client, owner, agent, project, bounds, _ = prepared
    root = issue(prepared)
    peer, credential = enroll(client, owner, project)
    child = deepcopy(bounds)
    child.update(subject_credential_id=credential["credential_id"], delegation_depth=1)
    body = {"expected_revision": 1, "expected_epoch": 1, "bounds": child}
    path = f"/agent-authority-grants/{root['id']}/delegate"
    delegated = post(client, agent, path, body, key="delegate")
    assert post(client, agent, path, body, key="delegate") == delegated
    assert (
        client.get(f"/agent-authority-grants/{root['id']}", headers=agent).json()[
            "remaining_budget_cents"
        ]
        == 0
    )
    post(client, agent, path, body, expected=403)
    post(client, peer, f"/agent-authority-grants/{delegated['id']}/consume", admit_payload(child))
    post(
        client,
        owner,
        f"/agent-authority-grants/{root['id']}/revoke",
        {"expected_revision": 1, "expected_epoch": 1},
        expected=200,
    )
    assert (
        client.get(f"/agent-authority-grants/{delegated['id']}", headers=peer).json()[
            "effective_status"
        ]
        == "fenced"
    )
    post(
        client,
        peer,
        f"/agent-authority-grants/{delegated['id']}/consume",
        admit_payload(child),
        expected=403,
    )
    execution = client.get(
        f"/execution-grants/{bounds['bindings'][0]['execution_grant_id']}", headers=owner
    ).json()
    assert execution["status"] == "cancel_requested"


@pytest.mark.parametrize("change", ["model", "target", "actions", "depth", "expiry", "budget"])
def test_delegation_cannot_widen(prepared, change):
    _, client, owner, agent, project, bounds, _ = prepared
    limited = deepcopy(bounds)
    limited["actions"] = ["attempt:admit", "authority:delegate"]
    root = issue(prepared, limited)
    _, credential = enroll(client, owner, project)
    child = deepcopy(limited)
    child.update(subject_credential_id=credential["credential_id"], delegation_depth=1)
    if change == "model":
        child["model"] = "other"
    if change == "target":
        child["bindings"][0]["target_digest"] = "b" * 64
    if change == "actions":
        child["actions"].append("execution:cancel")
    if change == "depth":
        child["delegation_depth"] = 2
    if change == "expiry":
        child["expires_at"] = (datetime.now(UTC) + timedelta(hours=2, minutes=1)).isoformat()
    if change == "budget":
        child["budget_cents"] = 101
    post(
        client,
        agent,
        f"/agent-authority-grants/{root['id']}/delegate",
        {"expected_revision": 1, "expected_epoch": 1, "bounds": child},
        expected=403,
    )


def test_concurrent_identical_command_has_one_reservation(prepared):
    state, client, _, agent, _, bounds, _ = prepared
    grant = issue(prepared)
    path = f"/agent-authority-grants/{grant['id']}/consume"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: post(client, agent, path, admit_payload(bounds), key="concurrent"),
                range(2),
            )
        )
    assert results[0] == results[1]
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM agent_authority_consumptions").fetchone()[0] == 1
        assert db.execute("SELECT reserved_cents FROM agent_authority_grants").fetchone()[0] == 100


def test_concurrent_delegation_cannot_double_reserve_parent_budget(prepared):
    state, client, owner, agent, project, bounds, _ = prepared
    root = issue(prepared)
    _, peer = enroll(client, owner, project)
    child = deepcopy(bounds)
    child.update(subject_credential_id=peer["credential_id"], delegation_depth=1)
    body = {"expected_revision": 1, "expected_epoch": 1, "bounds": child}
    path = f"/agent-authority-grants/{root['id']}/delegate"
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(
            pool.map(
                lambda i: client.post(
                    path, headers={**agent, "Idempotency-Key": f"race-{i}"}, json=body
                ),
                range(2),
            )
        )
    assert sorted(r.status_code for r in replies) == [201, 403]
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert (
            db.execute(
                "SELECT reserved_cents FROM agent_authority_grants WHERE id=?", (root["id"],)
            ).fetchone()[0]
            == 100
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM agent_authority_grants WHERE parent_id=?", (root["id"],)
            ).fetchone()[0]
            == 1
        )


def test_dispatch_target_status_cancel_and_replay(prepared):
    state, client, _, agent, _, bounds, worker = prepared
    grant = issue(prepared)
    path = f"/agent-authority-grants/{grant['id']}/consume"
    admitted = post(client, agent, path, admit_payload(bounds))["result"]
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.row_factory = sqlite3.Row
        lease = dict(
            db.execute(
                "SELECT * FROM execution_leases WHERE grant_id=?",
                (bounds["bindings"][0]["execution_grant_id"],),
            ).fetchone()
        )
    RuntimePackageStore(state).execution_package(
        worker,
        lease["id"],
        "package",
        "package",
        lease["epoch"],
        admitted_attempt_id=admitted["id"],
        execution_adapter="kubernetes-job",
    )
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        revision = db.execute(
            "SELECT revision FROM research_experiment_admitted_attempts WHERE id=?",
            (admitted["id"],),
        ).fetchone()[0]
    dispatch = {
        "expected_revision": 1,
        "expected_epoch": 1,
        "model": "model-1",
        "command": {
            "action": "attempt:dispatch",
            "attempt_id": admitted["id"],
            "expected_revision": revision,
            "target_digest": "b" * 64,
            "profile_version": "development-minikube-opencode-v1",
        },
    }
    post(client, agent, path, dispatch, expected=403)
    dispatch["command"]["target_digest"] = "a" * 64
    result = post(client, agent, path, dispatch, key="dispatch")
    assert post(client, agent, path, dispatch, key="dispatch") == result
    status = {
        "expected_revision": 1,
        "expected_epoch": 1,
        "model": "model-1",
        "command": {"action": "attempt:status", "attempt_id": admitted["id"]},
    }
    assert post(client, agent, path, status, expected=200)["result"]["id"] == admitted["id"]
    cancel = {
        "expected_revision": 1,
        "expected_epoch": 1,
        "model": "model-1",
        "command": {
            "action": "execution:cancel",
            "execution_grant_id": bounds["bindings"][0]["execution_grant_id"],
            "expected_revision": 1,
        },
    }
    cancelled = post(client, agent, path, cancel, key="cancel", expected=200)
    assert post(client, agent, path, cancel, key="cancel", expected=200) == cancelled
    assert cancelled["result"]["status"] == "cancel_requested"


def test_workflow_proposals_observations_review_notebook_and_exports(prepared):
    state, client, owner, agent, project, bounds, _ = prepared
    provenance = {"client": "mcp", "model": "model-1", "session_id": "session-1"}
    proposal = post(
        client,
        agent,
        f"/projects/{project}/agent-workflow-proposals",
        {
            "proposal": {
                "kind": "study",
                "study": {"purpose": "compare", "protocol": "bounded", "mode": "exploratory"},
            },
            "provenance": provenance,
        },
        key="research",
    )
    assert proposal["authority_effect"] == "none" and proposal["revision"] == 1
    promotion = {"expected_revision": 1, "expected_digest": proposal["digest"]}
    post(
        client,
        agent,
        f"/agent-research-proposals/{proposal['id']}/promote",
        promotion,
        expected=403,
    )
    study = post(client, owner, f"/agent-research-proposals/{proposal['id']}/promote", promotion)[
        "resource"
    ]
    grant = issue(prepared)
    admitted = post(
        client, agent, f"/agent-authority-grants/{grant['id']}/consume", admit_payload(bounds)
    )["result"]
    observation = {
        "attempt_id": admitted["id"],
        "expected_attempt_revision": 1,
        "summary": "not yet executed",
        "artifact_ids": [],
        "provenance": provenance,
    }
    observed = post(client, agent, f"/projects/{project}/agent-observations", observation)
    assert observed["provenance_kind"] == "external_agent_caller_attested"
    post(
        client,
        agent,
        f"/projects/{project}/agent-observations",
        {**observation, "artifact_ids": ["undisclosed"]},
        expected=403,
    )
    post(
        client,
        agent,
        f"/projects/{project}/agent-review-requests",
        {
            "subject_type": "observation",
            "subject_id": observed["id"],
            "expected_revision": 1,
            "reason": "review independently",
        },
    )
    notebook = client.get(f"/projects/{project}/agent-notebook?offset=0&limit=20", headers=agent)
    assert notebook.status_code == 200, notebook.text
    assert "not yet executed" not in notebook.text and "review independently" not in notebook.text
    exported = client.get(f"/projects/{project}/agent-notebook/export", headers=agent)
    assert exported.status_code == 200, exported.text
    assert len(exported.content) <= 2097152
    human_export = client.get(f"/studies/{study['id']}/export", headers=owner)
    assert human_export.status_code == 200, human_export.text
    assert "agent_notebook" in human_export.json()
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM operator_audit_events WHERE decision='denied'"
            ).fetchone()[0]
            >= 1
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM agent_provenance "
                "WHERE json_extract(envelope_json,'$.outcome')='denied'"
            ).fetchone()[0]
            >= 1
        )


RUNTIME_V55_TABLES = (
    "runtime_native_entries",
    "runtime_native_settlements",
    "runtime_native_claims",
    "runtime_native_root_fences",
    "runtime_native_operations",
    "runtime_authority_grants",
    "runtime_binding_revocations",
    "runtime_financial_assertions",
    "runtime_enrollments",
    "runtime_policy_profiles",
)


def strip_runtime_v55(db):
    """Remove only empty v55 additions from disposable historical fixtures."""
    immutable = (
        "runtime_policy_profiles",
        "runtime_enrollments",
        "runtime_financial_assertions",
        "runtime_binding_revocations",
        "runtime_native_claims",
        "runtime_native_settlements",
        "runtime_native_entries",
    )
    for table in RUNTIME_V55_TABLES:
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    for table in immutable:
        for action in ("update", "delete"):
            db.execute(f"DROP TRIGGER {table}_no_{action}")
    for table in RUNTIME_V55_TABLES:
        db.execute(f"DROP TABLE {table}")
        assert (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE name=? OR tbl_name=?", (table, table)
            ).fetchone()
            is None
        )


def assert_empty_runtime_v55(db):
    for table in RUNTIME_V55_TABLES:
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_upgrade_v50_does_not_infer_grants(tmp_path):
    state = tmp_path / "state"
    initialize_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        strip_runtime_v55(db)
        for table in (
            "agent_authority_consumptions",
            "agent_authority_grants",
            "agent_authority_requests",
            "agent_provenance",
            "agent_research_promotions",
            "agent_research_records",
        ):
            db.execute(f"DROP TABLE {table}")
        db.execute("UPDATE schema_meta SET schema_version=50")
    assert migrate_state(state) == 56
    assert migrate_state(state) == 56
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM agent_authority_grants").fetchone()[0] == 0
        assert_empty_runtime_v55(db)
    assert TestClient(create_app(state)).get("/health/ready").status_code == 200


@pytest.mark.parametrize(
    "pin", ["digest", "task_revision", "execution_revision", "epoch", "duration", "lifetime"]
)
def test_human_grant_cannot_pin_stale_or_overlong_execution(prepared, pin):
    _, client, owner, _, project, bounds, _ = prepared
    changed = deepcopy(bounds)
    binding = changed["bindings"][0]
    if pin == "digest":
        binding["plan_digest"] = "f" * 64
    elif pin == "task_revision":
        binding["expected_task_revision"] += 1
    elif pin == "execution_revision":
        binding["expected_execution_grant_revision"] += 1
    elif pin == "epoch":
        binding["expected_execution_epoch"] += 1
    elif pin == "duration":
        changed["max_duration_seconds"] = 100
    else:
        changed["expires_at"] = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
    post(
        client,
        owner,
        f"/projects/{project}/agent-authority-grants",
        {"bounds": changed},
        expected=403 if pin in {"duration", "lifetime"} else 409,
    )


def test_wrong_project_credential_and_replay_after_revocation(prepared):
    state, client, owner, agent, project, bounds, _ = prepared
    other = post(client, owner, "/projects", {"name": "other"})
    other_agent, _ = enroll(client, owner, other["id"])
    grant = issue(prepared)
    grant_path = f"/agent-authority-grants/{grant['id']}"
    assert client.get(grant_path, headers=other_agent).status_code == 404
    post(client, other_agent, grant_path + "/consume", admit_payload(bounds), expected=404)
    path = grant_path + "/consume"
    payload = admit_payload(bounds)
    post(client, agent, path, payload, key="once")
    post(
        client,
        owner,
        grant_path + "/revoke",
        {"expected_revision": 1, "expected_epoch": 1},
        expected=200,
    )
    post(client, agent, path, payload, key="once", expected=409)
    assert client.get(f"/projects/{other['id']}/agent-notebook", headers=agent).status_code == 404
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM agent_authority_consumptions").fetchone()[0] == 1
        assert (
            db.execute(
                "SELECT COUNT(*) FROM agent_provenance "
                "WHERE json_extract(envelope_json,'$.outcome')='failed'"
            ).fetchone()[0]
            >= 1
        )


def test_project_experiment_proposal_promotion_replay_and_stale_review(prepared):
    _, client, owner, agent, project, _, _ = prepared
    provenance = {"client": "mcp", "model": "model-1", "session_id": "session-1"}
    path = f"/projects/{project}/agent-workflow-proposals"
    proposed = post(
        client,
        agent,
        path,
        {
            "proposal": {
                "kind": "project",
                "name": "new project",
                "purpose": "request separate research",
            },
            "provenance": provenance,
        },
        key="project-proposal",
    )
    study = post(
        client,
        owner,
        f"/projects/{project}/studies",
        {"purpose": "compare", "protocol": "protocol", "mode": "exploratory"},
    )
    design = _design()
    experiment_proposal = post(
        client,
        agent,
        path,
        {
            "proposal": {"kind": "experiment", "study_id": study["id"], "experiment": design},
            "provenance": provenance,
        },
        key="experiment-proposal",
    )
    assert client.get(path, headers=agent).status_code == 200
    assert len(client.get(path, headers=agent).json()["proposals"]) == 2
    for record in (proposed, experiment_proposal):
        promote_path = f"/agent-research-proposals/{record['id']}/promote"
        body = {"expected_revision": 1, "expected_digest": record["digest"]}
        adopted = post(client, owner, promote_path, body, key="promote-" + record["id"])
        assert post(client, owner, promote_path, body, key="promote-" + record["id"]) == adopted
        assert adopted["execution_authorized"] is False
    review = {
        "subject_type": "proposal",
        "subject_id": proposed["id"],
        "expected_revision": 2,
        "reason": "review",
    }
    post(client, agent, f"/projects/{project}/agent-review-requests", review, expected=409)
    peer, _ = enroll(client, owner, project)
    review["expected_revision"] = 1
    post(client, peer, f"/projects/{project}/agent-review-requests", review, expected=404)
