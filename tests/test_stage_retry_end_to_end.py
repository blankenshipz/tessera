"""A real finished campaign through the retry builder, validate and artifact resolution."""

from __future__ import annotations

import pytest
from campaign_runner_harness import World, dump
from test_campaign_providers import _v2_chain  # noqa: F401 - shared v2 builders

pytest.importorskip("mcp")

from tessera_control import campaign_provider_worker as worker  # noqa: E402
from tessera_control.agent_campaign_reads import AgentCampaignReads  # noqa: E402
from tessera_mcp.stage_retry import build_retry_spec  # noqa: E402

OK = '{"score": 3}'
ACTIONS = ["campaign:submit", "campaign:read", "campaign:cancel", "artifact:read", "campaign:logs"]


@pytest.fixture(autouse=True)
def _local_docker_worker():
    yield
    worker.register_from_config(None)


def _failed_after_one_success(tmp_path):
    world = World(tmp_path)
    env = world.env
    env.trust()
    worker.register_from_config({"local_docker": {"worker_principal_id": env.worker["id"]}})
    world.scenarios = [(0, {"data.json": OK}), (9, {"data.json": '{"score": 4}'})]
    campaign = world.campaign(_v2_chain(env, "local-docker"))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "failed", dump(world, campaign["id"])
    return world, campaign["id"]


def _read(world, agent, campaign_id):
    reads = AgentCampaignReads(world.env.state)
    detail = reads.get_campaign(agent, campaign_id)
    outputs = reads.campaign_outputs(agent, campaign_id)
    return detail, outputs


def test_a_failed_chain_is_retried_from_its_recorded_outputs_and_the_spec_validates(tmp_path):
    world, cid = _failed_after_one_success(tmp_path)
    agent = world.env.agent(ACTIONS, "steerer")
    detail, outputs = _read(world, agent, cid)
    assert {s["stage_key"]: s["status"] for s in detail["stages"]} == {
        "a": "succeeded",
        "b": "failed",
    }
    plan = build_retry_spec(detail, outputs)
    assert plan["retried_stages"] == ["b"] and plan["source_campaign_id"] == cid
    (pin,) = plan["reused_outputs"]
    row = world.row(
        "SELECT sha256 FROM campaign_stage_io WHERE campaign_id=? AND stage_key='a' "
        "AND direction='output'",
        cid,
    )
    assert pin == {"stage": "a", "output": "result", "sha256": row["sha256"]}
    # The platform accepts it: the pin resolves from a succeeded attempt in the terminal receipt
    # whose manifest the runner custody-released (retain_quarantined) when stage a finished.
    verdict = AgentCampaignReads(world.env.state).validate_campaign_spec(
        agent, world.env.project["id"], {"spec": plan["spec"]}
    )
    assert verdict["valid"] is True and verdict["stage_order"] == ["b"]
    # Submitting what was validated stores a campaign with exactly that digest.
    body = {"spec": plan["spec"]}
    from tessera_control.store import canonical_digest

    status, stored = world.env.store.submit_campaign(
        agent, world.env.project["id"], "retry-submit", canonical_digest(body), body
    )
    assert status == 201 and stored["spec_digest"] == verdict["spec_digest"]


def test_a_retry_pin_stops_resolving_when_custody_is_revoked(tmp_path):
    """The retry depends on the source output's custody release; without it validation refuses."""
    world, cid = _failed_after_one_success(tmp_path)
    agent = world.env.agent(ACTIONS, "steerer")
    detail, outputs = _read(world, agent, cid)
    plan = build_retry_spec(detail, outputs)
    manifest = world.row(
        "SELECT m.* FROM artifact_manifests m JOIN campaign_stage_io io ON io.manifest_id = m.id "
        "WHERE io.campaign_id=? AND io.stage_key='a' AND io.direction='output'",
        cid,
    )
    human = world.row("SELECT id FROM principals WHERE role='human'")["id"]
    world.env.execute(
        """INSERT INTO artifact_custody_releases (id, manifest_id, manifest_digest, revision,
        state, rationale, actor_principal_id, created_at)
        VALUES ('rel_revoked', ?, ?, 99, 'revoked', 'test', ?, '2026-01-01T00:00:00Z')""",
        manifest["id"],
        manifest["manifest_digest"],
        human,
    )
    from tessera_control.store import Conflict

    with pytest.raises(Conflict, match="custody-released"):
        AgentCampaignReads(world.env.state).validate_campaign_spec(
            agent, world.env.project["id"], {"spec": plan["spec"]}
        )
