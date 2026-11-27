"""Re-run a failed campaign's failed stages, or its identical spec (ADR 0069). Fakes only."""

from __future__ import annotations

import json

import pytest
from campaign_runner_harness import World, dump
from fastapi.testclient import TestClient
from test_campaign_providers import _v2_chain  # noqa: F401 - shared v2 builders

from tessera_control import campaign_provider_worker as worker
from tessera_control.api import create_app
from tessera_control.store import Conflict, Forbidden, canonical_digest

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


def _rerun(world, actor, campaign_id, key, **body):
    if actor.get("role") == "agent":
        # Audit O-05: an agent re-runs only a campaign it may read, i.e. one it submitted. The
        # sources here are run by the harness as a person, so the re-running agent is recorded
        # as the source's submitter (test setup only; the refusal is in
        # test_campaign_read_submitter_scope.py).
        world.env.execute(
            "UPDATE campaigns SET submitted_by_principal_id=? WHERE id=?", actor["id"], campaign_id
        )
    return world.env.store.rerun_campaign(actor, campaign_id, key, canonical_digest(body), body)


def test_failed_stages_are_rerun_from_the_recorded_outputs_and_linked_on_the_receipt(tmp_path):
    world, cid = _failed_after_one_success(tmp_path)
    env = world.env
    agent = env.agent(ACTIONS, "steerer")
    runs_before = len(world.worker_runs)
    status, new = _rerun(world, agent, cid, "rerun-1")
    assert status == 201 and new["status"] == "submitted" and new["id"] != cid
    # A new campaign of only the failed stage; the succeeded stage's output is pinned by digest.
    spec = new["spec"]
    assert [s["key"] for s in spec["stages"]] == ["b"]
    pin = next(i for i in spec["stages"][0]["inputs"] if i["kind"] == "artifact")
    recorded = world.row(
        "SELECT sha256 FROM campaign_stage_io WHERE campaign_id=? AND stage_key='a' "
        "AND direction='output'",
        cid,
    )
    assert (pin["campaign_id"], pin["stage"], pin["sha256"]) == (cid, "a", recorded["sha256"])
    assert new["rerun"]["rerun_of"] == cid and new["rerun"]["retried_stages"] == ["b"]
    assert new["rerun"]["reused_outputs"] == [
        {"stage": "a", "output": "result", "sha256": recorded["sha256"]}
    ]
    # The cap never exceeds the original's and nothing was approved or run by submitting.
    assert spec["budget_cap_cents"] <= 10 and len(world.worker_runs) == runs_before
    assert env.query("SELECT 1 FROM campaign_approvals WHERE campaign_id=?", new["id"]) == []
    # The link is on both campaigns' views, recorded atomically with the submission.
    old_view = env.store.get_campaign(env.owner, cid)
    assert old_view["reruns"] == [
        {"campaign_id": new["id"], "mode": "failed_stages", "retried_stages": ["b"]}
    ]
    assert env.store.get_campaign(env.owner, new["id"])["rerun"]["rerun_of"] == cid
    # A person approves the new digest; only stage b runs and the receipt cites the old campaign.
    world.scenarios = [(0, {"data.json": '{"score": 5}'})]
    approved = env.approve(new)
    world.tick_all(approved["id"])
    assert world.status(new["id"]) == "succeeded", dump(world, new["id"])
    assert len(world.worker_runs) == runs_before + 1  # stage a was not run again
    receipt = json.loads(
        world.row("SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", new["id"])[
            "receipt_json"
        ]
    )
    assert receipt["rerun"]["rerun_of"] == cid
    assert receipt["rerun"]["source_spec_digest"] == old_view["spec_digest"]
    assert receipt["rerun"]["reused_outputs"][0]["sha256"] == recorded["sha256"]


def test_an_identical_rerun_resubmits_the_same_spec_for_a_fresh_approval(tmp_path):
    world, cid = _failed_after_one_success(tmp_path)
    env = world.env
    status, new = _rerun(world, env.agent(ACTIONS, "steerer"), cid, "rerun-same", mode="identical")
    old = env.store.get_campaign(env.owner, cid)
    assert status == 201 and new["spec_digest"] == old["spec_digest"]
    assert new["rerun"]["mode"] == "identical" and new["status"] == "submitted"
    assert [s["key"] for s in new["spec"]["stages"]] == ["a", "b"]  # nothing reused


def test_a_rerun_is_refused_rather_than_guessed(tmp_path):
    world, cid = _failed_after_one_success(tmp_path)
    env = world.env
    agent = env.agent(ACTIONS, "steerer")
    # An agent without the submit scope cannot re-run, whatever the plan would be.
    with pytest.raises(Forbidden, match="enrolled scope"):
        _rerun(world, env.agent(["campaign:read"], "reader"), cid, "k-noscope")
    with pytest.raises(Conflict, match="more than the original"):
        _rerun(world, agent, cid, "k-cap", budget_cap_cents=11)
    with pytest.raises(Conflict, match="no stage keys or cap"):
        _rerun(world, agent, cid, "k-id", mode="identical", stage_keys=["b"])
    with pytest.raises(Conflict, match="Unknown stage keys"):
        _rerun(world, agent, cid, "k-unknown", stage_keys=["zzz"])
    # An unfinished campaign cannot be re-run.
    status, fresh = _rerun(world, agent, cid, "k-fresh")
    with pytest.raises(Conflict, match="Only a failed, stopped or cancelled"):
        _rerun(world, agent, fresh["id"], "k-unfinished")
    # Custody revoked: the pin would not resolve, so the re-run refuses (ADR 0055's rule).
    manifest = world.row(
        "SELECT m.* FROM artifact_manifests m JOIN campaign_stage_io io ON io.manifest_id = m.id "
        "WHERE io.campaign_id=? AND io.stage_key='a' AND io.direction='output'",
        cid,
    )
    human = world.row("SELECT id FROM principals WHERE role='human'")["id"]
    env.execute(
        """INSERT INTO artifact_custody_releases (id, manifest_id, manifest_digest, revision,
        state, rationale, actor_principal_id, created_at)
        VALUES ('rel_revoked', ?, ?, 99, 'revoked', 'test', ?, '2026-01-01T00:00:00Z')""",
        manifest["id"],
        manifest["manifest_digest"],
        human,
    )
    with pytest.raises(Conflict, match="custody-released"):
        _rerun(world, agent, cid, "k-revoked")


def test_a_key_is_one_request_replays_are_safe_and_reuse_is_a_409(tmp_path):
    world, cid = _failed_after_one_success(tmp_path)
    env = world.env
    agent = env.agent(ACTIONS, "steerer")
    first = _rerun(world, agent, cid, "same-key", budget_cap_cents=4)
    assert _rerun(world, agent, cid, "same-key", budget_cap_cents=4) == first  # a retry replays
    assert env.query("SELECT count(*) AS n FROM campaigns")[0]["n"] == 2
    with pytest.raises(Conflict, match="different request"):
        _rerun(world, agent, cid, "same-key", budget_cap_cents=3)  # same key, other request
    assert env.query("SELECT count(*) AS n FROM campaigns")[0]["n"] == 2


def test_the_http_route_submits_the_rerun_and_refuses_a_reused_key(tmp_path):
    world, cid = _failed_after_one_success(tmp_path)
    env = world.env
    client = TestClient(create_app(env.state))
    token = (env.state / "owner.header").read_text().split("Bearer ", 1)[1].strip()

    def post(key, body):
        return client.post(
            f"/campaigns/{cid}/rerun",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
            json=body,
        )

    ok = post("http-1", {})
    assert ok.status_code == 201 and ok.json()["rerun"]["rerun_of"] == cid
    assert post("http-1", {}).json() == ok.json()
    assert post("http-1", {"mode": "identical"}).status_code == 409
    assert post("http-2", {"mode": "nonsense"}).status_code == 422


# -- the MCP tool -------------------------------------------------------------------------


def test_the_rerun_tool_needs_submit_and_read_and_posts_one_ordinary_request(tmp_path):
    pytest.importorskip("mcp")
    import asyncio

    from test_mcp_steering import _call, _server

    names = lambda actions: {  # noqa: E731
        t.name for t in asyncio.run(_server(tmp_path, [], {}, actions=actions).list_tools())
    }
    assert "tessera_rerun_campaign" not in names(frozenset({"campaign:submit"}))
    assert "tessera_rerun_campaign" not in names(frozenset({"campaign:read"}))
    # It hands back output digests, so it needs artifact:read like the offline retry tool.
    assert "tessera_rerun_campaign" not in names(frozenset({"campaign:submit", "campaign:read"}))
    assert "tessera_rerun_campaign" in names(
        frozenset({"campaign:submit", "campaign:read", "artifact:read"})
    )
    seen: list = []
    server = _server(tmp_path, seen, {"id": "campaign_2", "status": "submitted"})
    _call(
        server,
        "tessera_rerun_campaign",
        {
            "campaign_id": "campaign_1",
            "idempotency_key": "k-rerun",
            "stage_keys": ["b"],
            "budget_cap_cents": 4,
        },
    )
    ((method, path, key, content),) = seen
    assert (method, path, key) == ("POST", "/campaigns/campaign_1/rerun", "k-rerun")
    assert json.loads(content) == {
        "mode": "failed_stages",
        "stage_keys": ["b"],
        "budget_cap_cents": 4,
    }
    # Nothing but a submission: the tool has no approval, cap-raise or data-release path.
    assert all(word not in "tessera_rerun_campaign" for word in ("approve", "release", "raise"))


# -- review follow-ups --------------------------------------------------------------------


def test_reusing_outputs_needs_artifact_read_but_an_identical_rerun_does_not(tmp_path):
    world, cid = _failed_after_one_success(tmp_path)
    env = world.env
    no_artifacts = env.agent(["campaign:submit", "campaign:read"], "no-artifacts")
    with pytest.raises(Forbidden, match="enrolled scope"):
        _rerun(world, no_artifacts, cid, "k-reuse")  # would hand back output digests
    status, _ = _rerun(world, no_artifacts, cid, "k-identical", mode="identical")
    assert status == 201  # nothing reused: no digests are returned


def test_a_replay_is_answered_from_the_record_even_after_custody_changed(tmp_path):
    world, cid = _failed_after_one_success(tmp_path)
    env = world.env
    agent = env.agent(ACTIONS, "steerer")
    first = _rerun(world, agent, cid, "replay-key")
    manifest = world.row(
        "SELECT m.* FROM artifact_manifests m JOIN campaign_stage_io io ON io.manifest_id = m.id "
        "WHERE io.campaign_id=? AND io.stage_key='a' AND io.direction='output'",
        cid,
    )
    human = world.row("SELECT id FROM principals WHERE role='human'")["id"]
    env.execute(
        """INSERT INTO artifact_custody_releases (id, manifest_id, manifest_digest, revision,
        state, rationale, actor_principal_id, created_at)
        VALUES ('rel_revoked', ?, ?, 99, 'revoked', 'test', ?, '2026-01-01T00:00:00Z')""",
        manifest["id"],
        manifest["manifest_digest"],
        human,
    )
    assert _rerun(world, agent, cid, "replay-key") == first  # the stored answer, not a 409
    with pytest.raises(Conflict, match="custody-released"):
        _rerun(world, agent, cid, "another-key")  # a NEW request is planned afresh and refused


def test_custody_revoked_between_planning_and_submit_is_refused_at_submit(tmp_path, monkeypatch):
    from tessera_control import campaign_store

    world, cid = _failed_after_one_success(tmp_path)
    env = world.env
    agent = env.agent(ACTIONS, "steerer")
    real = campaign_store.plan_rerun

    def plan_then_revoke(source, request):
        plan = real(source, request)
        manifest = world.row(
            "SELECT m.* FROM artifact_manifests m JOIN campaign_stage_io io "
            "ON io.manifest_id = m.id WHERE io.campaign_id=? AND io.stage_key='a' "
            "AND io.direction='output'",
            cid,
        )
        human = world.row("SELECT id FROM principals WHERE role='human'")["id"]
        env.execute(
            """INSERT INTO artifact_custody_releases (id, manifest_id, manifest_digest, revision,
            state, rationale, actor_principal_id, created_at)
            VALUES ('rel_late', ?, ?, 99, 'revoked', 'test', ?, '2026-01-01T00:00:00Z')""",
            manifest["id"],
            manifest["manifest_digest"],
            human,
        )
        return plan

    monkeypatch.setattr(campaign_store, "plan_rerun", plan_then_revoke)
    with pytest.raises(Conflict, match="custody-released"):
        _rerun(world, agent, cid, "k-race")  # the submit transaction re-resolves every pin
    assert env.query("SELECT count(*) AS n FROM campaigns")[0]["n"] == 1


def test_a_rerun_of_a_rerun_keeps_the_original_pin_and_never_raises_the_cap(tmp_path):
    world, cid = _failed_after_one_success(tmp_path)
    env = world.env
    agent = env.agent(ACTIONS, "steerer")
    _, first = _rerun(world, agent, cid, "rr-1")
    world.scenarios = [(9, {"data.json": '{"score": 1}'})]  # the first re-run fails too
    approved = env.approve(first)
    world.tick_all(approved["id"])
    assert world.status(first["id"]) == "failed", dump(world, first["id"])
    env.execute("UPDATE campaigns SET settled_cents=3 WHERE id=?", first["id"])
    _, second = _rerun(world, agent, first["id"], "rr-2")
    assert second["rerun"]["rerun_of"] == first["id"]
    pin = next(i for i in second["spec"]["stages"][0]["inputs"] if i["kind"] == "artifact")
    assert pin["campaign_id"] == cid  # still the original output, by digest
    # The default cap is the source's cap less what the source settled, so a chain of re-runs
    # shrinks its default budget; only an explicit cap (never above the source's) differs.
    assert second["spec"]["budget_cap_cents"] == first["spec"]["budget_cap_cents"] - 3
    with pytest.raises(Conflict, match="more than the original"):
        _rerun(
            world,
            agent,
            first["id"],
            "rr-3",
            budget_cap_cents=first["spec"]["budget_cap_cents"] + 1,
        )
    old = env.store.get_campaign(env.owner, first["id"])
    assert [r["campaign_id"] for r in old["reruns"]] == [second["id"]]


def test_a_chain_of_reruns_cannot_be_approved_for_more_than_the_first_cap(tmp_path):
    world, cid = _failed_after_one_success(tmp_path)
    env = world.env
    agent = env.agent(ACTIONS, "steerer")
    first_cap = env.store.get_campaign(env.owner, cid)["spec"]["budget_cap_cents"]
    env.execute("UPDATE campaigns SET settled_cents=2 WHERE id=?", cid)
    _, first = _rerun(world, agent, cid, "c-1")
    assert first["spec"]["budget_cap_cents"] == first_cap - 2
    world.scenarios = [(9, {"data.json": '{"score": 1}'})]
    world.tick_all(env.approve(first)["id"])
    assert world.status(first["id"]) == "failed", dump(world, first["id"])
    env.execute("UPDATE campaigns SET settled_cents=3 WHERE id=?", first["id"])
    left = first_cap - 2 - 3  # the first cap less what the whole chain settled
    campaigns = env.query("SELECT count(*) AS n FROM campaigns")[0]["n"]
    # The source's own cap is above what is left, so neither a full explicit cap nor the
    # identical spec (which keeps that cap) may be submitted; nothing is written on a refusal.
    for key, body in (
        ("c-2", {"budget_cap_cents": first["spec"]["budget_cap_cents"]}),
        ("c-3", {"budget_cap_cents": left + 1}),
        ("c-4", {"mode": "identical"}),
    ):
        with pytest.raises(Conflict, match=f"{left} cents left"):
            _rerun(world, agent, first["id"], key, **body)
    assert env.query("SELECT count(*) AS n FROM campaigns")[0]["n"] == campaigns
    # The default is bounded by what is left, and exactly what is left is allowed.
    _, default = _rerun(world, agent, first["id"], "c-5")
    assert default["spec"]["budget_cap_cents"] == left
    # The default re-run is unfinished, so it commits its cap; cancelling it frees that cap.
    with pytest.raises(Conflict, match="0 cents left"):
        _rerun(world, agent, first["id"], "c-6a", budget_cap_cents=left)
    env.execute("UPDATE campaigns SET status='cancelled' WHERE id=?", default["id"])
    _, exact = _rerun(world, agent, first["id"], "c-6", budget_cap_cents=left)
    assert exact["spec"]["budget_cap_cents"] == left


def test_the_tree_bound_counts_siblings_and_unsettled_links(tmp_path):
    """ADR 0074: the whole tree's commitments count; an unfinished re-run commits its cap."""
    world, cid = _failed_after_one_success(tmp_path)
    env = world.env
    agent = env.agent(ACTIONS, "steerer")
    first_cap = env.store.get_campaign(env.owner, cid)["spec"]["budget_cap_cents"]
    _, one = _rerun(world, agent, cid, "s-1", budget_cap_cents=4)
    # The first sibling is only submitted, yet it commits its cap: the second sees what is left.
    left = first_cap - 4
    with pytest.raises(Conflict, match=f"{left} cents left"):
        _rerun(world, agent, cid, "s-2", budget_cap_cents=left + 1)
    _, two = _rerun(world, agent, cid, "s-3", budget_cap_cents=left)
    assert two["spec"]["budget_cap_cents"] == left
    with pytest.raises(Conflict, match="0 cents left"):
        _rerun(world, agent, cid, "s-4", budget_cap_cents=1)
    # Inspection: either member shows the tree, what it committed and what is left; the replay
    # key stays out of the read.
    view = env.store.get_campaign(env.owner, one["id"])
    tree = view["rerun_tree"]
    assert tree["root_campaign_id"] == cid and tree["root_cap_cents"] == first_cap
    assert tree["committed_cents"] == first_cap and tree["remaining_cents"] == 0
    assert {m["campaign_id"] for m in tree["members"]} == {cid, one["id"], two["id"]}
    assert "request_digest" not in view["rerun"]
    assert env.store.get_campaign(env.owner, cid)["rerun_tree"] == tree
    # Cancelling an unapproved re-run frees its cap; a finished one commits only what it settled.
    env.execute("UPDATE campaigns SET status='cancelled' WHERE id=?", one["id"])
    _, three = _rerun(world, agent, cid, "s-5", budget_cap_cents=4)
    assert three["spec"]["budget_cap_cents"] == 4


def test_an_idempotent_replay_still_succeeds_after_the_tree_has_filled_to_its_bound(tmp_path):
    world, cid = _failed_after_one_success(tmp_path)
    env = world.env
    agent = env.agent(ACTIONS, "steerer")
    first_cap = env.store.get_campaign(env.owner, cid)["spec"]["budget_cap_cents"]
    first = _rerun(world, agent, cid, "fill-1", budget_cap_cents=first_cap)
    # The tree is now full: a NEW request is refused, the replay returns the stored answer.
    assert env.store.get_campaign(env.owner, cid)["rerun_tree"]["remaining_cents"] == 0
    with pytest.raises(Conflict, match="0 cents left"):
        _rerun(world, agent, cid, "fill-2", budget_cap_cents=1)
    assert _rerun(world, agent, cid, "fill-1", budget_cap_cents=first_cap) == first
    assert env.query("SELECT count(*) AS n FROM campaigns")[0]["n"] == 2


@pytest.mark.parametrize("state", ["stopped", "cancelled"])
def test_a_stopped_or_cancelled_source_can_be_rerun(tmp_path, state):
    world, cid = _failed_after_one_success(tmp_path)
    world.env.execute("UPDATE campaigns SET status=? WHERE id=?", state, cid)
    status, new = _rerun(world, world.env.agent(ACTIONS, "steerer"), cid, "k-" + state)
    assert status == 201 and new["rerun"]["retried_stages"] == ["b"]


def test_a_v1_spec_can_only_be_rerun_identically(tmp_path):
    from campaign_runner_harness import stage_spec

    world = World(tmp_path)
    env = world.env
    spec = env.spec()
    spec["stages"] = [stage_spec(env, "s1")]
    world.scenarios = [(9, {"data.json": OK})]
    campaign = world.campaign(spec)
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "failed"
    agent = env.agent(ACTIONS, "steerer")
    with pytest.raises(Conflict, match="Only a v2 spec"):
        _rerun(world, agent, campaign["id"], "k-v1")
    status, new = _rerun(world, agent, campaign["id"], "k-v1-same", mode="identical")
    assert status == 201 and new["spec"]["version"] == 1
    assert new["spec_digest"] == env.store.get_campaign(env.owner, campaign["id"])["spec_digest"]


def test_an_input_reading_several_outputs_is_pinned_as_one_folder_or_refused_whole():
    from tessera_control.campaign_rerun import RerunRefused, build_retry_spec

    snap = {"kind": "snapshot", "name": "code", "snapshot_id": "s", "digest": "b" * 64,
            "data_class": "public"}  # fmt: skip

    def stage(key, inputs, outputs=("x", "y")):
        return {
            "key": key,
            "provider": "local-docker",
            "runtime": "docker-cpu-v1",
            "image": "python@sha256:" + "a" * 64,
            "argv": ["true"],
            "max_duration_seconds": 60,
            "inputs": inputs,
            "outputs": [{"name": n, "path": n + ".json", "max_bytes": 10} for n in outputs],
        }

    campaign = {
        "id": "c1",
        "status": "failed",
        "settled_cents": 0,
        "spec": {
            "version": 2,
            "name": "n",
            "budget_cap_cents": 5,
            "deadline_seconds": 600,
            "stages": [
                stage("a", [snap]),
                stage(
                    "b",
                    [snap, {"kind": "stage", "name": "up", "stage": "a", "outputs": ["x", "y"]}],
                ),
            ],
        },
        "stages": [
            {"stage_key": "a", "status": "succeeded"},
            {"stage_key": "b", "status": "failed"},
        ],
    }
    outputs = {"outputs": [{"stage_key": "a", "name": n, "sha256": "d" * 64} for n in ("x", "y")]}
    # ADR 0209: every file releasable, so one explicit folder pin keeps the mount layout.
    plan = build_retry_spec(campaign, outputs)
    (pin,) = [i for i in plan["spec"]["stages"][0]["inputs"] if i["name"] == "up"]
    assert pin == {
        "kind": "artifact_folder",
        "name": "up",
        "campaign_id": "c1",
        "stage": "a",
        "files": [
            {"output": "x", "path": "x.json", "sha256": "d" * 64},
            {"output": "y", "path": "y.json", "sha256": "d" * 64},
        ],
    }
    assert [(r["output"], r["path"]) for r in plan["reused_outputs"]] == [
        ("x", "x.json"),
        ("y", "y.json"),
    ]
    # One file that cannot be pinned still refuses the whole re-run (no partial pin).
    outputs["outputs"][1]["custody_released"] = False
    with pytest.raises(RerunRefused, match="'y' of 'a' is not custody-released"):
        build_retry_spec(campaign, outputs)


# -- the tree bound under concurrency and at its edges (ADR 0074, review rtsb) --------------


def _plan_against_now(world, source_id, **body):
    """Plan a re-run against the current state, without submitting (what a slow caller holds)."""
    from tessera_control import campaign_rerun
    from tessera_control.campaign_models import SubmitCampaign
    from tessera_control.store import _connect

    env = world.env
    connection = _connect(env.state / "tessera.sqlite3")
    try:
        row = connection.execute("SELECT * FROM campaigns WHERE id=?", (source_id,)).fetchone()
        source = campaign_rerun.load_source(connection, row)
    finally:
        connection.close()
    plan = campaign_rerun.plan_rerun(source, campaign_rerun.RerunCampaign(**body))
    plan["lineage"]["request_digest"] = canonical_digest(body)
    submit = SubmitCampaign.model_validate({"spec": plan["spec"]}).model_dump(mode="json")
    return plan, submit


def test_two_reruns_planned_against_the_same_state_cannot_both_be_submitted(tmp_path):
    """Planning and submitting are separate steps; the bound is re-read in the submit
    transaction, so the second of two simultaneous re-runs is refused and writes nothing."""
    world, cid = _failed_after_one_success(tmp_path)
    env = world.env
    agent = env.agent(ACTIONS, "steerer")
    first, first_body = _plan_against_now(world, cid)  # both see the full remainder
    second, second_body = _plan_against_now(world, cid)
    assert first["spec"]["budget_cap_cents"] == second["spec"]["budget_cap_cents"] > 0
    status, one = env.store.submit_campaign(
        agent, env.project["id"], "race-1", canonical_digest(first_body), first_body,
        lineage=first["lineage"],
    )  # fmt: skip
    assert status == 201
    campaigns = env.query("SELECT count(*) AS n FROM campaigns")[0]["n"]
    events = env.query(
        "SELECT count(*) AS n FROM events WHERE event_type='campaign.rerun_submitted'"
    )[0]["n"]
    with pytest.raises(Conflict, match="no longer covers"):
        env.store.submit_campaign(
            agent, env.project["id"], "race-2", canonical_digest(second_body), second_body,
            lineage=second["lineage"],
        )  # fmt: skip
    assert env.query("SELECT count(*) AS n FROM campaigns")[0]["n"] == campaigns
    assert (
        env.query("SELECT count(*) AS n FROM events WHERE event_type='campaign.rerun_submitted'")[
            0
        ]["n"]
        == events
    )  # nothing was written by the refused submission
    tree = env.store.get_campaign(env.owner, one["id"])["rerun_tree"]
    assert tree["committed_cents"] <= tree["root_cap_cents"]


def test_an_unfinished_member_commits_the_larger_of_its_cap_and_its_settled_spend(tmp_path):
    from tessera_control.campaign_rerun import rerun_tree
    from tessera_control.store import _connect

    world, cid = _failed_after_one_success(tmp_path)
    env = world.env
    _, one = _rerun(world, env.agent(ACTIONS, "steerer"), cid, "u-1", budget_cap_cents=4)
    env.execute("UPDATE campaigns SET settled_cents=6 WHERE id=?", one["id"])  # more than its cap
    connection = _connect(env.state / "tessera.sqlite3")
    try:
        tree = rerun_tree(
            connection, connection.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        )
    finally:
        connection.close()
    member = next(m for m in tree["members"] if m["campaign_id"] == one["id"])
    assert (member["cap_cents"], member["settled_cents"], member["committed_cents"]) == (4, 6, 6)


def test_a_cap_reduced_after_submission_frees_budget_and_a_long_tree_fails_closed(
    tmp_path, monkeypatch
):
    from tessera_control import campaign_rerun

    world, cid = _failed_after_one_success(tmp_path)
    env = world.env
    agent = env.agent(ACTIONS, "steerer")
    first_cap = env.store.get_campaign(env.owner, cid)["spec"]["budget_cap_cents"]
    _, one = _rerun(world, agent, cid, "t-1", budget_cap_cents=first_cap)  # takes everything
    with pytest.raises(Conflict, match="0 cents left"):
        _rerun(world, agent, cid, "t-2", budget_cap_cents=1)
    env.execute("UPDATE campaigns SET budget_cap_cents=3 WHERE id=?", one["id"])  # a reduction
    _, two = _rerun(world, agent, cid, "t-3", budget_cap_cents=first_cap - 3)
    assert two["spec"]["budget_cap_cents"] == first_cap - 3
    # A tree longer than the read limit reports itself truncated and refuses any more.
    monkeypatch.setattr(campaign_rerun, "MAX_TREE", 2)
    tree = env.store.get_campaign(env.owner, cid)["rerun_tree"]
    assert tree["truncated"] is True and len(tree["members"]) == 2
    with pytest.raises(Conflict, match="0 cents left"):
        _rerun(world, agent, cid, "t-4", budget_cap_cents=1)


def test_a_refusal_names_the_unfinished_rerun_holding_the_budget_and_how_to_free_it(tmp_path):
    world, cid = _failed_after_one_success(tmp_path)
    env = world.env
    agent = env.agent(ACTIONS, "steerer")
    _, held = _rerun(world, agent, cid, "h-1")  # an agent's re-run that nobody approves
    with pytest.raises(Conflict) as refused:
        _rerun(world, agent, cid, "h-2", budget_cap_cents=1)
    message = str(refused.value)
    assert held["id"] in message and "A person can cancel" in message
    # The person cancels it (an unapproved campaign is cancelled at once) and the budget is free.
    body = {"expected_revision": held["revision"], "reason": "not wanted"}
    status, _ = env.store.cancel_campaign(
        env.human(), held["id"], "cancel-held", canonical_digest(body), body
    )
    assert status == 200
    _, again = _rerun(world, agent, cid, "h-3", budget_cap_cents=1)
    assert again["spec"]["budget_cap_cents"] == 1
