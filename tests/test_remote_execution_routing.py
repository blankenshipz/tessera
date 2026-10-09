"""Offline remote routing: real control authority, fake external effects only."""

import copy
import json
import socket
import sqlite3
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_kubernetes_dispatch import _service
from test_kubernetes_execution import _admitted_package, _cluster, _FakeKubernetes
from test_program_interactions import ProgramJourney

from tessera_control.remote_execution import RemoteExecutionStore
from tessera_control.remote_execution_models import (
    ExecutionSelection,
    ProviderReadback,
    TargetPolicy,
)
from tessera_control.store import Conflict, Forbidden, NotFound, Store, StoreError, utc_now
from tessera_worker.kubernetes_execution import KubernetesExecutionProtocolAdapter
from tessera_worker.remote_dispatch import KubernetesRouteAdapter, RemoteDispatcher
from tessera_worker.salad_remote import (
    SaladGroupReadback,
    SaladOfflineAdapter,
    SaladProxyAdapter,
    SaladProxyConfig,
)


def setup_route(
    tmp_path,
    target="salad_container",
    *,
    approve=True,
    start=True,
    agent_proposal=False,
    salad_execution=None,
):
    response, worker, package = _admitted_package(
        tmp_path,
        argv=[salad_execution["command"], *salad_execution["argv"]] if salad_execution else None,
    )
    base = response["package"]
    journey = ProgramJourney(
        tmp_path,
        execution=SimpleNamespace(
            state=tmp_path / "state",
            project={"id": base["project_id"]},
            experiment={"id": base["experiment"]["experiment_id"]},
        ),
    )
    journey.assess()
    plan = journey.scope()
    plan["brief"]["compute"]["target"] = target
    program = journey.resolve(journey.propose(plan))
    source = journey.resolve(journey.propose(journey.scope("source_use")))
    cluster = _cluster()
    neutral = KubernetesExecutionProtocolAdapter
    selection = dict(
        target=target,
        target_digest=neutral.target_binding(cluster).identity_digest
        if target == "kubernetes"
        else "f" * 64,
        resources=dict(
            cpu_millicores=1000,
            memory_mb=1024,
            gpu_class=None,
            gpu_count=0,
            disk_mb=1024,
            max_seconds=60,
        ),
        manifests=dict(
            image=base["pinned_image"],
            data=base["snapshot_digest"],
            code="c" * 64,
            model="d" * 64,
            environment="e" * 64,
        ),
    )
    if target == "salad_container":
        selection["salad"] = dict(
            organization="fixture-org",
            project="fixture-project",
            group="attempt-" + base["operation_id"],
            country="US",
            provider_trust="broad_egress_ephemeral_reallocation_credit_not_hard_cap",
        )
        if salad_execution:
            selection["salad"]["execution"] = salad_execution
    policy = TargetPolicy(selection=ExecutionSelection.model_validate(selection))
    store = RemoteExecutionStore(journey.state, policies=(policy,))
    journey.client.app.state.remote_execution = store
    body = dict(
        operation_id=base["operation_id"],
        package_digest=response["package_digest"],
        attempt_id=response["admitted_attempt_id"],
        plan_digest=base["plan_digest"],
        lease_id=base["lease_id"],
        epoch=base["epoch"],
        selection=selection,
        study_id=journey.study["id"],
        program_review_id=program["id"],
        source_review_id=source["id"],
    )
    proposer = (
        Store(journey.state).authenticate(journey.agent.removeprefix("Bearer "))
        if agent_proposal
        else journey.actor
    )
    route = store.propose(proposer, base["task_id"], "route", body)[1]
    if target == "kubernetes":
        fake = _FakeKubernetes(package, cluster)
        adapter = KubernetesRouteAdapter(
            neutral(fake.adapter(tmp_path / "kube.sqlite3")), package, policy
        )
    else:
        fake = adapter = SaladOfflineAdapter(policy)
    h = SimpleNamespace(
        store=store,
        journey=journey,
        worker=worker,
        body=body,
        route=route,
        package=package,
        policy=policy,
        fake=fake,
        adapter=adapter,
    )
    h.dispatcher = RemoteDispatcher(store, worker, {policy.selection.target_digest: adapter})
    if approve:
        h.route = command(h, "approve")
    if start and approve:
        h.route = command(h, "start")
    return h


def command(h, choice, *, actor=None):
    h.route = h.store.get(h.journey.actor, h.route["id"])
    return h.store.command(
        actor or h.journey.actor,
        h.route["id"],
        uuid4().hex,
        dict(
            expected_revision=h.route["revision"],
            command=choice,
            proposal_digest=h.route["proposal_digest"],
        ),
    )[1]


def dispatch(h, action):
    h.route = h.dispatcher.dispatch(h.route["id"], action)
    return h.route


@pytest.mark.parametrize(
    "target", ["desktop", "local_process", "local_cpu", "local-cpu", "unknown"]
)
def test_desktop_and_local_targets_rejected(target):
    with pytest.raises(ValidationError):
        ExecutionSelection.model_validate(dict(target=target, target_digest="a" * 64))


@pytest.mark.parametrize("target", [[], {}, None, 1])
def test_malformed_target_is_validation_error_not_server_error(target):
    with pytest.raises(ValidationError):
        ExecutionSelection.model_validate(dict(target=target, target_digest="a" * 64))


def test_agent_proposes_but_cannot_approve_start_or_change_policy(tmp_path):
    h = setup_route(tmp_path, approve=False, agent_proposal=True)
    agent = Store(h.journey.state).authenticate(h.journey.agent.removeprefix("Bearer "))
    assert h.route["proposed_by"] == agent["id"]
    for choice in ("approve", "start", "pause", "cancel"):
        with pytest.raises(Forbidden):
            command(h, choice, actor=agent)
    with pytest.raises(Conflict):
        dispatch(h, "create")
    h.store = RemoteExecutionStore(h.journey.state)
    with pytest.raises(Forbidden, match="allowlisted"):
        command(h, "approve")
    assert h.adapter.calls == []
    assert h.store.get(agent, h.route["id"])["provider_enabled"] is False


@pytest.mark.parametrize(
    "field", ["epoch", "package_digest", "plan_digest", "attempt_id", "lease_id", "image", "data"]
)
def test_binding_mismatch_is_atomic(tmp_path, field):
    h = setup_route(tmp_path, approve=False)
    body = copy.deepcopy(h.body)
    if field in {"image", "data"}:
        body["selection"]["manifests"][field] = (
            "wrong@sha256:" + "0" * 64 if field == "image" else "0" * 64
        )
    else:
        body[field] = 999 if field == "epoch" else "0" * 64
    with pytest.raises((Conflict, NotFound)):
        h.store.propose(h.journey.actor, h.route["task_id"], uuid4().hex, body)
    assert len(h.store.list(h.journey.actor, h.route["task_id"])["routes"]) == 1
    assert h.adapter.calls == []


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE execution_grants SET epoch=epoch+1",
        "UPDATE execution_leases SET expires_at='2000-01-01T00:00:00Z'",
        "UPDATE tasks SET revision=revision+1",
        "UPDATE worker_registry SET lifecycle_state='retired'",
    ],
)
def test_fresh_authority_and_stale_epochs_fail_closed(tmp_path, sql):
    h = setup_route(tmp_path)
    with sqlite3.connect(h.store.database_path) as db:
        db.execute(sql)
    with pytest.raises((Conflict, Forbidden)):
        dispatch(h, "create")
    assert h.adapter.calls == []


def test_salad_stopped_start_cancel_202_unknown_then_confirmed_and_delete(tmp_path):
    h = setup_route(tmp_path)
    with pytest.raises(Conflict, match="stopped-group"):
        dispatch(h, "start")
    assert dispatch(h, "create")["state"] == "stopped"
    assert dispatch(h, "start")["state"] == "running"
    command(h, "pause")
    pending = dispatch(h, "stop")
    assert pending["cancellation"] == "pending" and not pending["termination_confirmed"]
    with pytest.raises(Conflict, match="attempted"):
        dispatch(h, "stop")
    h.adapter.readbacks.append(TimeoutError("secret must never be retained"))
    assert dispatch(h, "observe")["cancellation"] == "unknown"
    h.adapter.state = "stopped"
    confirmed = dispatch(h, "observe")
    assert confirmed["cancellation"] == "confirmed" and confirmed["termination_confirmed"]
    assert dispatch(h, "delete")["state"] == "deleted"
    with pytest.raises(Conflict):
        command(h, "start")
    assert "secret must" not in json.dumps(h.store.get(h.journey.actor, h.route["id"]))


@pytest.mark.parametrize("action", ["create", "start", "stop", "delete"])
def test_uncertain_effects_are_never_retried_across_restart(tmp_path, action):
    h = setup_route(tmp_path)
    if action != "create":
        dispatch(h, "create")
    if action in {"stop", "delete"}:
        command(h, "cancel")
    h.adapter.readbacks.append(TimeoutError())
    assert dispatch(h, action)["state"] == "unknown"
    restarted = RemoteDispatcher(
        RemoteExecutionStore(h.journey.state, policies=(h.policy,)),
        h.worker,
        {h.policy.selection.target_digest: h.adapter},
    )
    with pytest.raises(Conflict, match="attempted"):
        restarted.dispatch(h.route["id"], action)
    assert h.adapter.calls.count(action) == 1


def test_reallocation_is_unknown_and_never_restarts(tmp_path):
    h = setup_route(tmp_path)
    dispatch(h, "create")
    dispatch(h, "start")
    h.adapter.readbacks.append(
        dict(provider_id=h.adapter.provider_id, allocation_id="allocation-2", state="running")
    )
    state = dispatch(h, "observe")
    assert state["reallocated"] and state["state"] == "unknown"
    with pytest.raises(Conflict):
        dispatch(h, "start")
    assert h.adapter.calls.count("start") == 1


def test_intent_committed_before_provider_and_crash_is_unknown(tmp_path):
    h = setup_route(tmp_path)
    original = h.adapter.perform

    def inspect_intent(action, proposal, state):
        persisted = RemoteExecutionStore(h.journey.state).get(h.journey.actor, h.route["id"])
        assert persisted["effects"][-1]["action"] == action
        assert persisted["state"] == "unknown"
        return original(action, proposal, state)

    h.adapter.perform = inspect_intent
    dispatch(h, "create")
    # Simulate death between committed start intent and provider call/receipt.
    h.store.begin(h.worker, h.route["id"], "start", preflight_digest="a" * 64)
    assert h.store.get(h.journey.actor, h.route["id"])["state"] == "unknown"
    with pytest.raises(Conflict):
        dispatch(h, "start")


def test_kubernetes_selection_uses_existing_runner_and_collection_handoff(tmp_path):
    h = setup_route(tmp_path, "kubernetes")
    with sqlite3.connect(h.store.database_path) as db:
        row = db.execute(
            "SELECT * FROM kubernetes_dispatches WHERE operation_id=?",
            (h.body["operation_id"],),
        ).fetchone()
        assert row is not None
        dispatch_id = row[0]
    assert h.route["dispatch_id"] == dispatch_id
    assert h.route["receipts"] == [] and h.route["provider_enabled"] is False
    with pytest.raises(Conflict, match="outbox"):
        dispatch(h, "create")
    service = _service(tmp_path, h.journey.client, {"id": dispatch_id}, h.fake, _cluster())
    first = service.run(max_cycles=1)
    assert first["dispatch_id"] == dispatch_id
    assert first["operation_id"] == h.body["operation_id"]
    assert h.fake.creates == 1
    resumed = _service(tmp_path, h.journey.client, {"id": dispatch_id}, h.fake, _cluster())
    assert resumed.run(max_cycles=1)["operation_id"] == first["operation_id"]
    assert h.fake.creates == 1


def test_kubernetes_route_review_and_start_are_separate_atomic_decisions(tmp_path):
    h = setup_route(tmp_path, "kubernetes", start=False)
    with sqlite3.connect(h.store.database_path) as db:
        assert db.execute("SELECT count(*) FROM kubernetes_dispatches").fetchone()[0] == 0
    h.route = command(h, "start")
    with pytest.raises(Conflict):
        command(h, "start")
    with sqlite3.connect(h.store.database_path) as db:
        assert db.execute("SELECT count(*) FROM kubernetes_dispatches").fetchone()[0] == 1
    assert h.fake.creates == 0


def test_kubernetes_route_fenced_start_does_not_queue(tmp_path):
    h = setup_route(tmp_path, "kubernetes", start=False)
    with sqlite3.connect(h.store.database_path) as db:
        db.execute("UPDATE execution_grants SET epoch=epoch+1")
    with pytest.raises((Conflict, Forbidden)):
        command(h, "start")
    with sqlite3.connect(h.store.database_path) as db:
        assert db.execute("SELECT count(*) FROM kubernetes_dispatches").fetchone()[0] == 0


def test_kubernetes_route_cancel_fences_queued_outbox(tmp_path):
    h = setup_route(tmp_path, "kubernetes")
    command(h, "cancel")
    with sqlite3.connect(h.store.database_path) as db:
        assert (
            db.execute(
                "SELECT state FROM kubernetes_dispatches WHERE id=?", (h.route["dispatch_id"],)
            ).fetchone()[0]
            == "cancel_requested"
        )
    assert h.fake.creates == 0


def test_evidence_cannot_invent_artifact_custody_or_swap_provider(tmp_path):
    h = setup_route(tmp_path)
    dispatch(h, "create")
    h.adapter.readbacks.append(dict(state="stopped", provider_id="replacement"))
    with pytest.raises(Conflict, match="substitution"):
        dispatch(h, "observe")
    h.adapter.readbacks.append(
        dict(
            state="stopped",
            provider_id=h.adapter.provider_id,
            evidence=[
                dict(
                    kind="artifact", sha256="a" * 64, manifest_id="invented", custody="quarantined"
                )
            ],
        )
    )
    with pytest.raises(Conflict, match="custody"):
        dispatch(h, "observe")
    assert h.store.get(h.journey.actor, h.route["id"])["state"] == "unknown"


def test_provider_disabled_and_secret_control_input_non_exposure_no_spawn(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("local process spawn or network forbidden")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    h = setup_route(tmp_path)
    for key in ("endpoint", "port", "credential", "command", "env", "salad_api_key"):
        body = copy.deepcopy(h.body)
        body["selection"][key] = "secret"
        with pytest.raises(StoreError):
            h.store.propose(h.journey.actor, h.route["task_id"], uuid4().hex, body)
    cfg = SaladProxyConfig(
        proxy_reference="proxy-ref:server", credential_reference="credential-ref:salad"
    )
    disabled = SaladProxyAdapter(cfg, SimpleNamespace(preflight=forbidden, mutate=forbidden))
    dispatcher = RemoteDispatcher(h.store, h.worker, {h.policy.selection.target_digest: disabled})
    with pytest.raises(Conflict, match="preflight"):
        dispatcher.dispatch(h.route["id"], "create")
    with pytest.raises(ValidationError):
        SaladProxyConfig.model_validate(cfg.model_dump() | {"live_enabled": True})
    dispatch(h, "create")
    assert "credential-ref" not in json.dumps(h.store.get(h.journey.actor, h.route["id"]))


def test_control_api_reconnect_no_effect_and_scoped_access(tmp_path):
    h = setup_route(tmp_path)
    dispatch(h, "create")
    dispatch(h, "start")
    h.journey.client.close()  # Desktop/control connection lifetime is not execution lifetime.
    restarted = RemoteExecutionStore(h.journey.state)
    state = restarted.get(h.journey.actor, h.route["id"])
    assert state["state"] == "running" and state["control_surface"] == "desktop_control_only"
    assert state["go_no_go"] == "no_go_live_activation_blocked"
    # Wrong-tenant principal cannot use caller-claimed org to fetch records.
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = ProgramJourney(other_root)
    with pytest.raises((Forbidden, NotFound)):
        restarted.get(other.actor, h.route["id"])
    assert h.adapter.calls == ["create", "start"]


def test_receipt_202_is_not_termination_evidence():
    with pytest.raises(ValidationError):
        ProviderReadback(
            provider_id="group",
            state="stopped",
            observed_at=utc_now(),
            response_status=202,
            termination_confirmed=True,
        )


def test_cancellation_after_intent_fences_provider_use(tmp_path):
    h = setup_route(tmp_path)
    begin = h.store.begin

    def revoke_after_intent(*args, **kwargs):
        intent = begin(*args, **kwargs)
        command(h, "cancel")
        return intent

    h.store.begin = revoke_after_intent
    assert dispatch(h, "create")["state"] == "unknown"
    assert h.adapter.calls == []


@pytest.mark.parametrize("field", ["organization", "project", "group", "country", "replicas"])
def test_exact_salad_stopped_group_readback_contract(tmp_path, field):
    from tessera_control.remote_execution_models import RemoteProposal

    h = setup_route(tmp_path)
    dispatch(h, "create")
    proposal = h.policy.selection.model_dump(mode="json")
    proposal["salad"][field] = 2 if field == "replicas" else "CA" if field == "country" else "other"
    with pytest.raises((Conflict, ValidationError)):
        SaladGroupReadback(
            selection=proposal,
            provider_id=h.adapter.provider_id,
            state="stopped",
            observed_at=utc_now(),
        ).require_stopped(RemoteProposal.model_validate(h.route["proposal"]), h.adapter.provider_id)
    assert h.adapter.calls == ["create"]


def test_exact_resource_image_and_code_policy_and_stale_preflight(tmp_path):
    from tessera_control.remote_execution_models import RemoteProposal

    h = setup_route(tmp_path, approve=False)
    for field in ("code", "model", "image"):
        changed = h.policy.model_dump(mode="json")
        changed["selection"]["manifests"][field] = (
            "other@sha256:" + "0" * 64 if field == "image" else "0" * 64
        )
        h.store = RemoteExecutionStore(
            h.journey.state, policies=(TargetPolicy.model_validate(changed),)
        )
        with pytest.raises(Forbidden, match="allowlisted"):
            command(h, "approve")
    changed = h.policy.model_dump(mode="json")
    changed["selection"]["resources"]["cpu_millicores"] += 1
    h.store = RemoteExecutionStore(
        h.journey.state, policies=(TargetPolicy.model_validate(changed),)
    )
    with pytest.raises(Forbidden, match="allowlisted"):
        command(h, "approve")
    with pytest.raises(Conflict):
        SaladGroupReadback(
            selection=h.policy.selection,
            provider_id="group",
            state="stopped",
            observed_at="2000-01-01T00:00:00Z",
        ).require_stopped(RemoteProposal.model_validate(h.route["proposal"]), "group")


def test_program_and_source_review_required_and_rechecked(tmp_path):
    h = setup_route(tmp_path)
    h.journey.propose(h.journey.scope("source_use"))
    with pytest.raises(Conflict, match="reviews"):
        dispatch(h, "create")
    assert h.adapter.calls == []


def test_api_default_disabled_and_review_replay_is_not_effect_authority(tmp_path):
    from fastapi.testclient import TestClient

    from tessera_control.api import create_app

    h = setup_route(tmp_path, approve=False)
    client = TestClient(create_app(h.journey.state))
    headers = {"Authorization": h.journey.owner, "Idempotency-Key": "approval"}
    body = dict(
        expected_revision=h.route["revision"],
        proposal_digest=h.route["proposal_digest"],
        command="approve",
    )
    path = f"/remote-executions/{h.route['id']}/commands"
    assert client.post(path, headers=headers, json=body).status_code == 403
    client.app.state.remote_execution = h.store
    approved = client.post(path, headers=headers, json=body)
    assert approved.status_code == 200
    assert client.post(path, headers=headers, json=body).json() == approved.json()
    assert (
        client.get(f"/tasks/{h.route['task_id']}/remote-executions", headers=headers).json()[
            "routes"
        ][0]["id"]
        == h.route["id"]
    )
    assert h.adapter.calls == []


def test_held_restore_retains_route_history_but_cannot_mutate_or_replay(tmp_path):
    from test_execution import _owner

    from tessera_control.recovery import backup_state, restore_state

    h = setup_route(tmp_path)
    dispatch(h, "create")
    backup_state(h.journey.state, tmp_path / "backup")
    restore_state(tmp_path / "backup", tmp_path / "held")
    held = RemoteExecutionStore(tmp_path / "held", policies=(h.policy,))
    owner = _owner(tmp_path / "held")
    state = held.get(owner, h.route["id"])
    assert state["authority_status"] == "fenced_reconciliation_only"
    with pytest.raises(Forbidden, match="held"):
        held.command(
            owner,
            state["id"],
            "cancel",
            dict(
                command="cancel",
                expected_revision=state["revision"],
                proposal_digest=state["proposal_digest"],
            ),
        )
