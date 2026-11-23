"""Campaign delegation hook: the security-critical authority path (ADR 0038)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from campaign_fixture import Env
from fastapi.testclient import TestClient

import tessera_control.campaign_delegation as delegation
from tessera_control.api import create_app
from tessera_control.approval_trust import CAMPAIGN_DELEGATION_KEY
from tessera_control.campaign_delegation import (
    REDUCE_ONLY_STEPS,
    STEP_TABLE,
    approver_is_live,
    write_intent,
)
from tessera_control.snapshot_releases import SnapshotReleaseStore
from tessera_control.store import Conflict, Forbidden, Store, StoreError, canonical_digest

SRC = Path(__file__).parent.parent / "src" / "tessera_control"


def _task_count(env: Env) -> int:
    return env.query("SELECT count(*) AS n FROM tasks")[0]["n"]


def _attempt_with_release(env: Env, campaign: dict) -> None:
    now = "2026-01-01T00:00:00Z"
    env.execute(
        "INSERT INTO campaign_stage_attempts (id, campaign_id, stage_key, attempt, status, "
        "hold_cents, release_id, revision, created_at, updated_at) "
        "VALUES ('att1', ?, 's1', 1, 'reserved', 6, ?, 1, ?, ?)",
        campaign["id"],
        env.release_id,
        now,
        now,
    )


def _revoke_release(env: Env, campaign: dict, approval: dict):
    revision = env.query("SELECT revision FROM snapshot_releases WHERE id=?", env.release_id)[0][0]
    payload = {"expected_revision": revision}
    principal, intent, key, digest = env.intent(
        campaign,
        approval,
        "revoke_release",
        {"release_id": env.release_id},
        payload,
        attempt=1,
    )
    return SnapshotReleaseStore(env.state).revoke_snapshot_release(
        principal, env.release_id, key, digest, revision
    )


def test_delegated_steps_run_as_approver_and_are_attributed(tmp_path):
    env = Env(tmp_path)
    campaign, approval = env.approved()
    task = env.create_task_step(campaign, approval)
    revision = {
        "expected_revision": task["revision"],
        "title": task["title"],
        "purpose": task["purpose"],
        "budget_cents": task["budget_cents"],
        "max_duration_seconds": task["max_duration_seconds"],
        "execution_spec": {
            "argv": ["python", "-c", "print(1)"],
            "input_snapshot_digest": env.snapshot_digest,
            "pinned_skill_digests": [],
            "output_paths": ["data.json"],
            "profile": "local-cpu",
            "network_enabled": False,
            "max_output_bytes": 1024,
        },
    }
    principal, _, key, digest = env.intent(
        campaign, approval, "revise_plan", {"task_id": task["id"]}, revision
    )
    _, revised = env.plain.revise_task_plan(principal, task["id"], key, digest, revision)
    approve = {"expected_revision": revised["revision"]}
    principal, _, key, digest = env.intent(
        campaign, approval, "approve_task", {"task_id": task["id"]}, approve
    )
    status, approved = env.plain.approve_task(
        principal, task["id"], key, digest, revised["revision"], revised["current_plan_digest"]
    )
    assert status == 200 and approved["status"] == "approved"
    assert {row["status"] for row in env.intents(campaign["id"])} == {"done"}
    event = env.events("task.approved")[-1]
    payload = event["payload"]
    assert payload["decision_class"] == "campaign_delegated"
    assert payload["campaign_id"] == campaign["id"]
    assert payload["approval_id"] == approval["id"]
    assert payload["approval_decision_class"] == "human_session"
    assert event["actor_principal_id"] == approval["approving_principal_id"]
    completed = env.events("campaign.step_completed")
    assert [item["payload"]["step"] for item in completed] == [
        "create_task",
        "revise_plan",
        "approve_task",
    ]


def test_owner_approval_class_and_label_are_carried(tmp_path):
    env = Env(tmp_path)
    campaign, approval = env.approved(env.owner)
    assert approval["decision_class"] == "owner_automation_labelled"
    assert approval["automation_label"] == "pytest"
    env.create_task_step(campaign, approval)
    # The delegated actor passes require_decision_actor only through the verified hook;
    # without the marker the same unlabelled owner is refused (next test).


def test_bare_owner_cannot_approve_without_label(tmp_path):
    env = Env(tmp_path)
    owner = {key: value for key, value in env.owner.items() if key != "automation_label"}
    campaign = env.submit(owner)
    body = {
        "expected_revision": campaign["revision"],
        "expected_spec_digest": campaign["spec_digest"],
    }
    with pytest.raises(Forbidden, match="human_session"):
        env.store.approve_campaign(owner, campaign["id"], "a", canonical_digest(body), body)
    assert env.query("SELECT count(*) AS n FROM campaign_approvals")[0]["n"] == 0


def test_marker_without_intent_is_refused_and_writes_nothing(tmp_path):
    env = Env(tmp_path)
    campaign, approval = env.approved()
    before = _task_count(env)
    forged = {
        "id": approval["approving_principal_id"],
        "org_id": env.owner["org_id"],
        CAMPAIGN_DELEGATION_KEY: {"campaign_id": campaign["id"], "intent_id": "intent_nope"},
    }
    payload = env.task_payload()
    with pytest.raises(Forbidden):
        env.plain.create_task(forged, env.project["id"], "k", canonical_digest(payload), payload)
    assert _task_count(env) == before
    assert env.query("SELECT count(*) AS n FROM idempotency WHERE idempotency_key='k'")[0][0] == 0


def test_intent_must_match_request_exactly(tmp_path):
    env = Env(tmp_path)
    campaign, approval = env.approved()
    payload = env.task_payload()
    resources = {"project_id": env.project["id"]}
    principal, _, key, digest = env.intent(campaign, approval, "create_task", resources, payload)
    before = _task_count(env)
    with pytest.raises(Forbidden):  # different request body
        env.plain.create_task(
            principal, env.project["id"], key, canonical_digest({"x": 1}), payload
        )
    with pytest.raises(Forbidden):  # different idempotency key
        env.plain.create_task(principal, env.project["id"], "other-key", digest, payload)
    with pytest.raises(Forbidden):  # different path (another project id)
        env.plain.create_task(principal, "project_other", key, digest, payload)
    assert _task_count(env) == before
    assert env.query("SELECT status FROM campaign_step_intents")[0]["status"] == "pending"
    status, _ = env.plain.create_task(principal, env.project["id"], key, digest, payload)
    assert status == 201 and _task_count(env) == before + 1


def test_intent_cannot_be_used_by_another_campaign_or_principal(tmp_path):
    env = Env(tmp_path)
    first, first_approval = env.approved()
    second = env.approve(env.submit(), env.human("other"))
    payload = env.task_payload()
    principal, intent, key, digest = env.intent(
        first, first_approval, "create_task", {"project_id": env.project["id"]}, payload
    )
    wrong_campaign = {
        **principal,
        CAMPAIGN_DELEGATION_KEY: {"campaign_id": second["id"], "intent_id": intent["id"]},
    }
    with pytest.raises(Forbidden):
        env.plain.create_task(wrong_campaign, env.project["id"], key, digest, payload)
    other_actor = {**principal, "id": second["approval"]["approving_principal_id"]}
    with pytest.raises(Forbidden):
        env.plain.create_task(other_actor, env.project["id"], key, digest, payload)


def test_replay_returns_stored_response_without_second_effect(tmp_path):
    env = Env(tmp_path)
    campaign, approval = env.approved()
    payload = env.task_payload()
    principal, intent, key, digest = env.intent(
        campaign, approval, "create_task", {"project_id": env.project["id"]}, payload
    )
    first = env.plain.create_task(principal, env.project["id"], key, digest, payload)
    again = env.plain.create_task(principal, env.project["id"], key, digest, payload)
    assert first == again
    assert (
        env.query("SELECT status FROM campaign_step_intents WHERE id=?", intent["id"])[0][0]
        == "done"
    )
    assert len(env.events("campaign.step_completed")) == 1
    # write_intent is idempotent per slot and returns the same row.
    campaign_row = dict(env.query("SELECT * FROM campaigns")[0])
    approval_row = dict(env.query("SELECT * FROM campaign_approvals")[0])
    with env.store.runner_connection() as connection:
        again_intent = write_intent(
            connection,
            campaign=campaign_row,
            approval=approval_row,
            stage_key="s1",
            attempt=None,
            step="create_task",
            ordinal=0,
            method="POST",
            path=f"/projects/{env.project['id']}/tasks",
            request_digest=digest,
            idempotency_key=key,
            resources={"project_id": env.project["id"]},
        )
    assert again_intent["id"] == intent["id"] and again_intent["status"] == "done"


def test_superseded_approval_cannot_execute_a_pending_intent(tmp_path):
    env = Env(tmp_path)
    campaign, approval = env.approved()
    payload = env.task_payload()
    principal, _, key, digest = env.intent(
        campaign, approval, "create_task", {"project_id": env.project["id"]}, payload
    )
    env.execute("UPDATE campaign_approvals SET status='superseded' WHERE id=?", approval["id"])
    with pytest.raises(Forbidden):
        env.plain.create_task(principal, env.project["id"], key, digest, payload)
    assert env.query("SELECT count(*) AS n FROM tasks WHERE title LIKE '%/s1'")[0][0] == 0


def test_cancel_requested_refuses_forward_steps_but_allows_reduce_only(tmp_path):
    env = Env(tmp_path)
    campaign, approval = env.approved()
    _attempt_with_release(env, campaign)
    cancel = {"expected_revision": campaign["revision"], "reason": "stop now"}
    env.store.cancel_campaign(env.human(), campaign["id"], "c", canonical_digest(cancel), cancel)
    payload = env.task_payload("late")
    principal, _, key, digest = env.intent(
        campaign,
        approval,
        "create_task",
        {"project_id": env.project["id"]},
        payload,
        stage_key="s2",
    )
    with pytest.raises(Forbidden):
        env.plain.create_task(principal, env.project["id"], key, digest, payload)
    assert "revoke_release" in REDUCE_ONLY_STEPS
    status, result = _revoke_release(env, campaign, approval)
    assert status == 200 and result["status"] == "revoked"


def test_expired_approval_stops_forward_but_not_reduce_only(tmp_path):
    env = Env(tmp_path)
    campaign, approval = env.approved()
    _attempt_with_release(env, campaign)
    env.execute(
        "UPDATE campaign_approvals SET expires_at='2000-01-01T00:00:00Z' WHERE id=?",
        approval["id"],
    )
    payload = env.task_payload()
    principal, _, key, digest = env.intent(
        campaign, approval, "create_task", {"project_id": env.project["id"]}, payload
    )
    with pytest.raises(Forbidden):
        env.plain.create_task(principal, env.project["id"], key, digest, payload)
    assert _revoke_release(env, campaign, approval)[0] == 200


def test_terminal_campaign_allows_nothing(tmp_path):
    env = Env(tmp_path)
    campaign, approval = env.approved()
    _attempt_with_release(env, campaign)
    env.execute("UPDATE campaigns SET status='failed' WHERE id=?", campaign["id"])
    with pytest.raises(Forbidden):
        _revoke_release(env, campaign, approval)


def test_failure_after_command_leaves_intent_pending_and_no_effect(tmp_path, monkeypatch):
    env = Env(tmp_path)
    campaign, approval = env.approved()
    payload = env.task_payload("atomic")
    principal, intent, key, digest = env.intent(
        campaign, approval, "create_task", {"project_id": env.project["id"]}, payload
    )
    before = _task_count(env)

    def boom(*args):
        raise RuntimeError("synthetic failure after the command")

    with monkeypatch.context() as patch:
        patch.setattr(delegation, "complete", boom)
        with pytest.raises(RuntimeError, match="synthetic"):
            env.plain.create_task(principal, env.project["id"], key, digest, payload)
    assert _task_count(env) == before
    row = env.query("SELECT status FROM campaign_step_intents WHERE id=?", intent["id"])[0]
    assert row["status"] == "pending"
    assert (
        env.query("SELECT count(*) AS n FROM idempotency WHERE idempotency_key=?", key)[0][0] == 0
    )
    assert env.events("campaign.step_completed") == []
    # The intent is still usable: re-issuing the exact call commits effect and completion.
    assert env.plain.create_task(principal, env.project["id"], key, digest, payload)[0] == 201


def test_resources_must_be_owned_by_the_campaign(tmp_path):
    env = Env(tmp_path)
    campaign, approval = env.approved()
    human_task = env.query("SELECT id FROM tasks LIMIT 1")[0]["id"]
    with pytest.raises(Forbidden):  # a task no stage of this campaign created
        env.intent(campaign, approval, "approve_task", {"task_id": human_task}, {"a": 1})
        principal, _, key, digest = env.intent(
            campaign, approval, "approve_task", {"task_id": human_task}, {"a": 1}, ordinal=1
        )
        env.plain.approve_task(principal, human_task, key, digest, 1, "0" * 64)
    # A release the campaign never recorded cannot be revoked.
    with pytest.raises(Forbidden):
        _revoke_release(env, campaign, approval)
    # Unknown resource names and a mismatched path value are refused at write time.
    with pytest.raises(Forbidden):
        env.intent(
            campaign,
            approval,
            "create_task",
            {"project_id": env.project["id"], "grant_id": "g"},
            {},
        )
    with pytest.raises(Forbidden):
        env.intent(
            campaign,
            approval,
            "create_task",
            {"project_id": "elsewhere"},
            {},
            ordinal=2,
            path=f"/projects/{env.project['id']}/tasks",
        )
    campaign_row = dict(env.query("SELECT * FROM campaigns")[0])
    approval_row = dict(env.query("SELECT * FROM campaign_approvals")[0])
    with pytest.raises(StoreError), env.store.runner_connection() as connection:
        write_intent(
            connection,
            campaign=campaign_row,
            approval=approval_row,
            stage_key="s1",
            attempt=None,
            step="export_everything",  # not in the closed table
            ordinal=0,
            method="POST",
            path="/exports",
            request_digest="0" * 64,
            idempotency_key="k",
            resources={},
        )


def test_foreign_project_in_a_resource_is_refused_at_admission(tmp_path):
    env = Env(tmp_path)
    campaign, approval = env.approved()
    payload = env.task_payload()
    principal, intent, key, digest = env.intent(
        campaign, approval, "create_task", {"project_id": env.project["id"]}, payload
    )
    env.execute(
        'UPDATE campaign_step_intents SET resources_json=\'{"project_id":"project_x"}\' WHERE id=?',
        intent["id"],
    )
    with pytest.raises(Forbidden):
        env.plain.create_task(principal, env.project["id"], key, digest, payload)


def test_only_the_first_plan_is_delegated(tmp_path):
    env = Env(tmp_path)
    campaign, approval = env.approved()
    task = env.create_task_step(campaign, approval)
    revision = {
        "expected_revision": task["revision"],
        "title": task["title"],
        "purpose": task["purpose"],
        "budget_cents": task["budget_cents"],
        "max_duration_seconds": task["max_duration_seconds"],
        "execution_spec": None,
    }
    principal, _, key, digest = env.intent(
        campaign, approval, "revise_plan", {"task_id": task["id"]}, revision
    )
    _, revised = env.plain.revise_task_plan(principal, task["id"], key, digest, revision)
    second = {**revision, "expected_revision": revised["revision"], "title": "again"}
    principal, _, key, digest = env.intent(
        campaign, approval, "revise_plan", {"task_id": task["id"]}, second, ordinal=1
    )
    with pytest.raises(Forbidden):
        env.plain.revise_task_plan(principal, task["id"], key, digest, second)


def test_revoked_approver_lapses_delegation(tmp_path):
    env = Env(tmp_path)
    approver = env.human("approver")
    campaign, approval = env.approved(approver)
    payload = env.task_payload()
    principal, _, key, digest = env.intent(
        campaign, approval, "create_task", {"project_id": env.project["id"]}, payload
    )
    approval_row = dict(env.query("SELECT * FROM campaign_approvals")[0])
    with env.store.runner_connection() as connection:
        assert approver_is_live(connection, approval_row)
    env.plain.revoke_credential(env.owner, approver["credential_id"], "rv", "rv")
    with env.store.runner_connection() as connection:
        assert not approver_is_live(connection, approval_row)
    with pytest.raises(Forbidden):
        env.plain.create_task(principal, env.project["id"], key, digest, payload)
    assert _task_count(env) == 1  # only the fixture's own task


def test_marker_never_survives_authentication_or_http(tmp_path, monkeypatch):
    env = Env(tmp_path)
    actor = env.human("marker")
    assert CAMPAIGN_DELEGATION_KEY not in actor
    original = Store._credential_principal

    def tainted(connection, row, audience):
        result = original(connection, row, audience)
        result[CAMPAIGN_DELEGATION_KEY] = {"campaign_id": "c", "intent_id": "i"}
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Store, "_credential_principal", staticmethod(tainted))
        stripped = env.plain.authenticate(token_for(env, "marker"))
    assert CAMPAIGN_DELEGATION_KEY not in stripped

    client = TestClient(create_app(env.state))
    owner_token = (env.state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    response = client.post(
        f"/projects/{env.project['id']}/tasks",
        headers={
            "Authorization": f"Bearer {owner_token}",
            "Idempotency-Key": "http-1",
            CAMPAIGN_DELEGATION_KEY: json.dumps({"campaign_id": "c", "intent_id": "i"}),
            "X-Tessera-Automation": "http-test",
        },
        json={**env.task_payload("http"), CAMPAIGN_DELEGATION_KEY: {"intent_id": "i"}},
    )
    assert response.status_code == 422  # extra body field refused
    response = client.post(
        f"/projects/{env.project['id']}/tasks",
        headers={
            "Authorization": f"Bearer {owner_token}",
            "Idempotency-Key": "http-2",
            CAMPAIGN_DELEGATION_KEY: json.dumps({"campaign_id": "c", "intent_id": "i"}),
            "X-Tessera-Automation": "http-test",
        },
        json=env.task_payload("http"),
    )
    assert response.status_code == 201  # header ignored: ordinary, undelegated call
    assert env.query("SELECT count(*) AS n FROM campaign_step_intents")[0][0] == 0
    assert env.events("campaign.step_completed") == []


def token_for(env: Env, name: str) -> str:
    return f"synthetic-{name}-token-" + "x" * 30


def test_task_creation_is_not_attributed_to_a_campaign_without_the_hook(tmp_path):
    env = Env(tmp_path)
    env.approved()
    payload = env.task_payload("plain")
    env.plain.create_task(env.owner, env.project["id"], "plain", canonical_digest(payload), payload)
    assert env.events("campaign.step_completed") == []


def test_step_table_paths_match_the_store_methods():
    """Each template must equal the literal path its store method passes to _mutate."""
    sources = {
        "create_task": ("store.py", 'path = f"/projects/{project_id}/tasks"'),
        "revise_plan": ("store.py", 'path = f"/tasks/{task_id}/plan"'),
        "approve_task": ("store.py", 'path = f"/tasks/{task_id}/approve"'),
        "release_snapshot": ("snapshot_releases.py", 'f"/snapshots/{snapshot_id}/releases"'),
        "revoke_release": ("snapshot_releases.py", 'f"/snapshot-releases/{release_id}/revoke"'),
        "create_experiment": ("experiments.py", 'f"/projects/{project_id}/experiments"'),
        "authorize_budget": ("experiments.py", 'f"/experiments/{experiment_id}/budget/{action}"'),
        "issue_grant": ("execution.py", 'path = f"/projects/{project_id}/execution-grants"'),
        "admit_attempt": (
            "experiments.py",
            'f"/experiment-variants/{variant_id}/admitted-attempts"',
        ),
        "revoke_grant": ("execution.py", 'path = f"/execution-grants/{grant_id}/revoke"'),
        "reconcile_grant": ("execution.py", 'path = f"/execution-grants/{grant_id}/reconcile"'),
        "reconcile_unattempted": (
            "execution.py",
            'path = f"/execution-grants/{grant_id}/reconcile-expired-unattempted"',
        ),
        "record_disposition": (
            "artifact_dispositions.py",
            'path = f"/artifact-manifests/{manifest_id}/dispositions"',
        ),
        "custody_release": (
            "artifact_custody.py",
            'f"/artifact-manifests/{manifest_id}/custody-releases"',
        ),
        "resolve_attempt": (
            "experiments.py",
            'f"/experiment-admitted-attempts/{attempt_id}/resolve"',
        ),
        "link_artifacts": ("experiments.py", 'f"/experiment-attempts/{attempt_id}/artifacts"'),
        "derive_measurement": (
            "experiments.py",
            'f"/experiment-attempts/{attempt_id}/derived-measurements"',
        ),
        "bind_split": (
            "gate_evidence_splits.py",
            'f"/experiment-attempts/{attempt_id}/split-binding"',
        ),
        "record_gate_metric": (
            "gate_evidence_metrics.py",
            'f"/experiment-attempts/{attempt_id}/metric-records"',
        ),
        "ingest_evidence_rows": (
            "gate_evidence_ingest.py",
            'f"/experiment-attempts/{attempt_id}/evidence-rows"',
        ),
        "record_provider_fetch": (
            "provider_fetch_record.py",
            'f"/experiment-attempts/{attempt_id}/provider-fetch"',
        ),
        "register_lineage_node": (
            "artifact_lineage.py",
            'path = f"/projects/{project_id}/artifact-lineage/nodes"',
        ),
        "record_lineage_edge": (
            "artifact_lineage.py",
            'path = f"/projects/{project_id}/artifact-lineage/edges"',
        ),
        "compose_snapshot": (
            "snapshot_composition.py",
            'f"/projects/{project_id}/composed-snapshots"',
        ),
        "create_study": ("research.py", 'path = f"/projects/{project_id}/studies"'),
        "create_study_source": ("research.py", 'f"/studies/{study_id}/sources"'),
        "register_study_variant": ("research.py", 'f"/studies/{study_id}/variants"'),
        "create_split_lock": ("gate_evidence_splits.py", 'f"/projects/{project_id}/split-locks"'),
        "propose_decision": ("interaction.py", '"POST", "/interaction/decisions"'),
        "assess_source": ("research.py", 'f"/sources/{source_id}/assessment"'),
        "resolve_decision": ("interaction.py", 'f"/interaction/decisions/{decision_id}/resolve"'),
        "request_capability": ("capabilities.py", 'f"/tasks/{task_id}/capabilities"'),
        "approve_capability": ("capabilities.py", 'f"/capabilities/{ident}/approve"'),
        "propose_route": ("remote_execution.py", 'f"/tasks/{task_id}/remote-executions"'),
        "approve_route": ("remote_execution.py", 'f"/remote-executions/{ident}/commands"'),
        "start_route": ("remote_execution.py", 'f"/remote-executions/{ident}/commands"'),
        "cancel_route": ("remote_execution.py", 'f"/remote-executions/{ident}/commands"'),
        "reconcile_unclaimed_salad": (
            "salad_unclaimed.py",
            'path = f"/execution-grants/{grant_id}/reconcile-unclaimed-salad"',
        ),
    }
    assert set(sources) == set(STEP_TABLE)
    for step, (module, literal) in sources.items():
        assert literal in (SRC / module).read_text(), step
        template = STEP_TABLE[step][1]
        if step != "authorize_budget":  # the budget route's action is the literal 'authorization'
            assert re.sub(r"\{[a-z_]+\}", "{}", template) in re.sub(
                r"\{[a-z_]+\}", "{}", literal
            ), step
    assert STEP_TABLE["revise_plan"][0] == "PUT"
    assert all(
        method == "POST" for step, (method, _) in STEP_TABLE.items() if step != "revise_plan"
    )
    assert STEP_TABLE["authorize_budget"][1].endswith("/budget/authorization")


def test_conflict_from_the_command_does_not_complete_the_intent(tmp_path):
    env = Env(tmp_path)
    campaign, approval = env.approved()
    payload = {"title": "", "purpose": "x", "budget_cents": 1, "max_duration_seconds": 1}
    principal, intent, key, digest = env.intent(
        campaign, approval, "create_task", {"project_id": env.project["id"]}, payload
    )
    with pytest.raises((Conflict, StoreError, KeyError, Forbidden)):
        env.plain.create_task(principal, "wrong-project", key, digest, payload)
    assert (
        env.query("SELECT status FROM campaign_step_intents WHERE id=?", intent["id"])[0][0]
        == "pending"
    )


def test_a_pinned_artifact_manifest_is_owned_only_for_the_compose_step(monkeypatch):
    from tessera_control import campaign_delegation as delegation

    class _Row:
        def __init__(self, found):
            self.found = found

        def fetchone(self):
            return (1,) if self.found else None

    class _Connection:
        def execute(self, sql, params=()):
            return _Row("campaign_stage_io" in sql)  # only the pinned index row exists

    source = {"campaign_id": "c0", "stage": "s", "output": "o", "sha256": "a" * 64}
    monkeypatch.setattr(delegation, "_spec_artifact_sources", lambda campaign: [source])
    campaign = {"id": "c1", "org_id": "org", "project_id": "p"}
    owned = delegation._resource_owned
    assert owned(_Connection(), campaign, "manifest_id", "m1", "compose_snapshot")
    for step in ("record_disposition", "custody_release", None):
        assert not owned(_Connection(), campaign, "manifest_id", "m1", step)


def test_stopping_admits_forward_steps_only_for_an_attempt_already_in_flight(tmp_path):
    from tessera_control.campaign_delegation import admit

    env = Env(tmp_path)
    campaign, approval = env.approved()
    _attempt_with_release(env, campaign)  # s1 attempt 1 is reserved
    env.execute("UPDATE campaigns SET status='stopping' WHERE id=?", campaign["id"])
    nodes = f"/projects/{env.project['id']}/artifact-lineage/nodes"

    def admitted(stage_key, attempt):
        body = {"kind": "x", "stage": stage_key, "attempt": attempt}
        principal, _i, key, digest = env.intent(
            campaign,
            approval,
            "register_lineage_node",
            {"project_id": env.project["id"]},
            body,
            stage_key=stage_key,
            attempt=attempt,
        )
        live = {
            "id": approval["approving_principal_id"],
            "org_id": campaign["org_id"],
            "role": "human",
            "project_id": None,
        }
        with env.store.runner_connection() as connection:
            return admit(
                connection, live, principal["campaign_delegation"], "POST", nodes, digest, key
            )

    assert admitted("s1", 1)["step"] == "register_lineage_node"
    with pytest.raises(Forbidden):
        admitted("s2", 1)  # no attempt was ever reserved for s2
    with pytest.raises(Forbidden):
        admitted("s1", None)  # a stage-level step is not part of an in-flight attempt
    env.execute("UPDATE campaign_stage_attempts SET status='succeeded' WHERE id='att1'")
    with pytest.raises(Forbidden):
        admitted("s1", 1)  # the attempt finished: nothing more is admitted
