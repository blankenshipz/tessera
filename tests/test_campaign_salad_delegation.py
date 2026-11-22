"""Delegation rows added for Salad stages: exact intents, closed resources, no human steps."""

from __future__ import annotations

import pytest
from campaign_fixture import Env

from tessera_control.campaign_delegation import REDUCE_ONLY_STEPS, STEP_TABLE, admit
from tessera_control.research import ResearchStore
from tessera_control.store import Forbidden, StoreError, canonical_digest

NEW_STEPS = {
    "create_study",
    "create_study_source",
    "register_study_variant",
    "create_split_lock",
    "propose_decision",
    "request_capability",
    "approve_capability",
    "propose_route",
    "approve_route",
    "start_route",
    "cancel_route",
    "reconcile_unclaimed_salad",
}


@pytest.fixture(autouse=True)
def _stage_provider_may_take_every_step():
    """These tests exercise the closed step table itself, through a stage whose provider
    declares every step (the per-provider step sets have their own tests)."""
    from dataclasses import replace

    from tessera_control import campaign_provider_worker as worker
    from tessera_control.campaign_providers import register_provider, unregister_provider

    unregister_provider("local-docker")
    descriptor = replace(
        worker._descriptor("local-docker", ""), delegable_steps=frozenset(STEP_TABLE)
    )
    register_provider(worker.WorkerGrantProvider(descriptor))
    yield
    unregister_provider("local-docker")
    worker._REGISTERED.discard("local-docker")
    worker.register_from_config(None)


def study_payload(note="operational"):
    return {"purpose": f"{note} stage", "protocol": "bounded attempts, quarantined output"}


def own_study(env, campaign):
    """A study row the campaign owns, as the runner records after create_study."""
    now = "2026-01-01T00:00:00Z"
    env.execute(
        "INSERT INTO campaign_stages (campaign_id, stage_key, topo_index, status, prep_phase, "
        "output_data_class, revision, updated_at) VALUES (?, 'g1', 9, 'pending', 'none', "
        "'public', 1, ?)",
        campaign["id"],
        now,
    )
    env.execute(
        "INSERT INTO campaign_salad_stages (campaign_id, stage_key, profile, study_id, revision, "
        "updated_at) VALUES (?, 'g1', 'gpu-main', 'study_owned', 1, ?)",
        campaign["id"],
        now,
    )


def test_the_new_rows_are_in_the_table_and_the_reduce_only_rows_are_marked():
    assert NEW_STEPS <= set(STEP_TABLE)
    assert {"cancel_route", "reconcile_unclaimed_salad"} <= REDUCE_ONLY_STEPS
    assert not {"approve_route", "start_route", "propose_route"} & REDUCE_ONLY_STEPS
    # The three command rows share one path and are told apart by the command resource.
    assert len({STEP_TABLE[s] for s in ("approve_route", "start_route", "cancel_route")}) == 1


@pytest.mark.parametrize(
    "path",
    [
        "/sources/{id}/assess",
        "/interaction/decisions/resolve",
        "/experiments/{id}/budget/charges",
        "/experiments/{id}/budget/bounds",
        "/projects/{id}/execution-limits",
    ],
)
def test_human_only_paths_are_never_in_the_table(path):
    assert path not in {template for _method, template in STEP_TABLE.values()}
    # Source assessment and decision resolution are delegated only under an exact-digest
    # pre-approval (ADR 0044); no other review-like step exists.
    assert {s for s in STEP_TABLE if "assess" in s or "resolve" in s} == {
        "assess_source",
        "resolve_decision",
        "resolve_attempt",
    }


def test_create_study_is_admitted_with_its_exact_intent_and_nothing_else(tmp_path):
    env = Env(tmp_path)
    campaign, approval = env.approved()
    payload = study_payload()
    principal, _intent, key, digest = env.intent(
        campaign,
        approval,
        "create_study",
        {"project_id": env.project["id"]},
        payload,
        stage_key="s1",
    )
    status, study = ResearchStore(env.state).create_study(
        principal, env.project["id"], key, digest, payload
    )
    assert status == 201 and study["project_id"] == env.project["id"]
    # The same intent cannot be replayed against a different body or path.
    other = study_payload("changed")
    with pytest.raises(Forbidden):
        ResearchStore(env.state).create_study(
            principal, env.project["id"], key, canonical_digest(other), other
        )


def admit_call(env, campaign, approval, principal, path, digest, key):
    """Run the delegation hook exactly as Store._mutate does, inside a transaction."""
    live = {
        "id": approval["approving_principal_id"],
        "org_id": campaign["org_id"],
        "role": "human",
        "project_id": None,
    }
    with env.store.runner_connection() as connection:
        return admit(connection, live, principal["campaign_delegation"], "POST", path, digest, key)


def test_a_study_the_campaign_does_not_own_is_refused_for_a_source(tmp_path):
    env = Env(tmp_path)
    campaign, approval = env.approved()
    own_study(env, campaign)
    body = {"uri": "urn:tessera:x", "quoted_passage": "q", "content_digest": "d" * 64}
    mine, _i, key, digest = env.intent(
        campaign,
        approval,
        "create_study_source",
        {"study_id": "study_owned"},
        body,
        stage_key="s1",
    )
    admit_call(env, campaign, approval, mine, "/studies/study_owned/sources", digest, key)
    foreign, _i2, key2, digest2 = env.intent(
        campaign,
        approval,
        "create_study_source",
        {"study_id": "study_foreign"},
        body,
        stage_key="s1",
        ordinal=1,
    )
    with pytest.raises(Forbidden):
        admit_call(
            env, campaign, approval, foreign, "/studies/study_foreign/sources", digest2, key2
        )


@pytest.mark.parametrize(
    "step,resources",
    [
        ("approve_route", {"route_id": "route_x", "command": "start"}),
        ("start_route", {"route_id": "route_x", "command": "approve"}),
        ("cancel_route", {"route_id": "route_x", "command": "start"}),
        ("approve_route", {"route_id": "route_x"}),
        ("propose_decision", {"project_id": "p", "study_id": "s", "decision_action": "go_no_go"}),
        ("propose_decision", {"project_id": "p", "study_id": "s"}),
        ("release_snapshot", {"snapshot_id": "snap"}),
        ("release_snapshot", {"snapshot_id": "snap", "classification": "secret"}),
    ],
)
def test_resources_must_name_the_exact_command_action_and_class(tmp_path, step, resources):
    env = Env(tmp_path)
    campaign, approval = env.approved()
    with pytest.raises((Forbidden, StoreError)):
        env.intent(campaign, approval, step, resources, {"x": 1}, stage_key="s1")


def test_a_start_route_intent_cannot_carry_an_approve_payload(tmp_path):
    env = Env(tmp_path)
    campaign, approval = env.approved()
    own_route(env, campaign)
    approve = {"expected_revision": 1, "command": "approve", "proposal_digest": "a" * 64}
    start = {**approve, "command": "start"}
    resources = {"route_id": "route_owned", "command": "start"}
    principal, intent, key, digest = env.intent(
        campaign, approval, "start_route", resources, start, stage_key="s1", attempt=1
    )
    path = "/remote-executions/route_owned/commands"
    assert digest == canonical_digest(start) != canonical_digest(approve)
    # The exact body is admitted by the hook; the approve body is not.
    with pytest.raises(Forbidden):
        admit_call(env, campaign, approval, principal, path, canonical_digest(approve), key)
    assert admit_call(env, campaign, approval, principal, path, digest, key)["id"] == intent["id"]


def own_route(env, campaign):
    now = "2026-01-01T00:00:00Z"
    env.execute(
        "INSERT INTO campaign_stages (campaign_id, stage_key, topo_index, status, prep_phase, "
        "output_data_class, revision, updated_at) VALUES (?, 'g1', 9, 'pending', 'none', "
        "'public', 1, ?)",
        campaign["id"],
        now,
    )
    env.execute(
        "INSERT INTO campaign_stage_attempts (id, campaign_id, stage_key, attempt, status, "
        "hold_cents, revision, created_at, updated_at) "
        "VALUES ('att_route', ?, 'g1', 1, 'reserved', 6, 1, ?, ?)",
        campaign["id"],
        now,
        now,
    )
    env.execute(
        "INSERT INTO campaign_salad_attempts (attempt_id, phase, route_id, revision, updated_at) "
        "VALUES ('att_route', 'route_proposed', 'route_owned', 1, ?)",
        now,
    )


def test_a_route_the_campaign_does_not_own_is_refused(tmp_path):
    env = Env(tmp_path)
    campaign, approval = env.approved()
    own_route(env, campaign)
    body = {"expected_revision": 1, "command": "approve", "proposal_digest": "a" * 64}
    mine, _i, key, digest = env.intent(
        campaign,
        approval,
        "approve_route",
        {"route_id": "route_owned", "command": "approve"},
        body,
        stage_key="s1",
        attempt=1,
    )
    admit_call(
        env, campaign, approval, mine, "/remote-executions/route_owned/commands", digest, key
    )
    foreign, _i2, key2, digest2 = env.intent(
        campaign,
        approval,
        "approve_route",
        {"route_id": "route_foreign", "command": "approve"},
        body,
        stage_key="s1",
        attempt=1,
        ordinal=1,
    )
    with pytest.raises(Forbidden):
        admit_call(
            env,
            campaign,
            approval,
            foreign,
            "/remote-executions/route_foreign/commands",
            digest2,
            key2,
        )
