"""One-time holdout claims of final evaluation stages (ADR 0068).

Synthetic fixtures only: the fake worker writes the scorer's output, and the "labels" are a
small private snapshot staged in a temporary project. No experiment-specific code.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from campaign_runner_harness import IMAGE, World, dump
from pydantic import ValidationError

from tessera_control import campaign_provider_worker as worker
from tessera_control import campaign_providers as cp
from tessera_control.agent_campaign_reads import AgentCampaignReads
from tessera_control.campaign_models import parse_spec, spec_digest
from tessera_control.migrations import LATEST_SCHEMA_VERSION
from tessera_control.snapshots import SnapshotStore
from tessera_control.store import Conflict, canonical_digest
from tessera_control.workspace import capture_workspace, inventory_digest

SCOPE = "study-a/final-test"


@pytest.fixture(autouse=True)
def _local_provider():
    yield
    worker.register_from_config(None)


def _world(tmp_path) -> World:
    from provider_trust_fixture import DERIVED, install_trust

    world = World(tmp_path)
    worker_id = world.env.worker["id"]
    cp.configure_providers({"local_docker": {"worker_principal_id": worker_id}})
    install_trust({"local-docker": (DERIVED, [worker_id])})
    return world


def _labels(world: World, content: bytes = b"id,label\n1,0\n") -> tuple[str, str]:
    """A private snapshot standing in for sealed test labels (recorded private_derived)."""
    root = world.tmp / f"labels-{len(content)}"
    root.mkdir()
    (root / "labels.csv").write_bytes(content)
    capture = capture_workspace(root.absolute())
    digest = inventory_digest(capture.inventory)
    _, snapshot = SnapshotStore(world.env.state).adopt_snapshot(
        world.env.owner, world.env.project["id"], f"labels-{len(content)}", digest, capture
    )
    return snapshot["id"], snapshot["digest"]


def _code(env) -> dict:
    return {
        "kind": "snapshot",
        "name": "code",
        "snapshot_id": env.snapshot_id,
        "digest": env.snapshot_digest,
        "data_class": "public",
    }


def _stage(key: str, inputs: list[dict], metric: str, **extra) -> dict:
    return {
        "key": key,
        "provider": "local-docker",
        "runtime": "docker-cpu-v1",
        "image": IMAGE,
        "argv": ["python", "-c", "print(1)"],
        "outputs": [{"name": "result", "path": "data.json", "kind": "metrics", "max_bytes": 1024}],
        "max_duration_seconds": 60,
        "inputs": inputs,
        "metrics": [{"name": metric, "path": "data.json", "json_pointer": "/score"}],
        **extra,
    }


def _spec(
    env, labels: tuple[str, str], *, scope: str = SCOPE, name: str = "eval", code=None
) -> dict:
    snapshot_id, digest = labels
    select = _stage("select", [code or _code(env)], "val_score")
    final = _stage(
        "final",
        [
            {"kind": "stage", "name": "chosen", "stage": "select", "outputs": ["result"]},
            {
                "kind": "snapshot",
                "name": "labels",
                "snapshot_id": snapshot_id,
                "digest": digest,
                "data_class": "private_derived",
            },
        ],
        "test_score",
        evaluation={
            "final": True,
            "holdout": {"input": "labels", "partition_digest": digest, "claim_scope": scope},
        },
    )
    return {
        "version": 2,
        "name": name,
        "budget_cap_cents": 10,
        "deadline_seconds": 3600,
        "stages": [select, final],
    }


def _submit(env, spec: dict, key: str):
    body = {"spec": spec}
    return env.store.submit_campaign(
        env.owner, env.project["id"], key, canonical_digest(body), body
    )


def _claims(world: World) -> list:
    return world.env.query("SELECT * FROM campaign_holdout_claims ORDER BY opened_at")


# -- spec validation (pure) --------------------------------------------------------------

DIGEST = "b" * 64


def _pure(final_extra=None, **evaluation) -> dict:
    labels = {
        "kind": "snapshot",
        "name": "labels",
        "snapshot_id": "snap_labels",
        "digest": DIGEST,
        "data_class": "private_derived",
    }
    code = {
        "kind": "snapshot",
        "name": "code",
        "snapshot_id": "snap_code",
        "digest": "c" * 64,
        "data_class": "public",
    }
    holdout = {"input": "labels", "partition_digest": DIGEST, "claim_scope": SCOPE}
    final = _stage(
        "final",
        [{"kind": "stage", "name": "chosen", "stage": "select", "outputs": ["result"]}, labels],
        "test_score",
        evaluation={"final": True, "holdout": holdout, **evaluation},
        **(final_extra or {}),
    )
    return {
        "version": 2,
        "name": "eval",
        "budget_cap_cents": 10,
        "deadline_seconds": 3600,
        "stages": [_stage("select", [code], "val_score"), final],
    }


def _refused(spec: dict, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        parse_spec(spec)


def test_a_valid_holdout_spec_parses_and_specs_without_it_keep_their_digest():
    spec = parse_spec(_pure())
    assert spec.stages[1].evaluation.holdout.claim_scope == SCOPE
    plain = _pure()
    del plain["stages"][1]["evaluation"]
    dumped = parse_spec(plain).model_dump(mode="json")
    assert "evaluation" not in dumped["stages"][1]
    assert spec_digest(parse_spec(plain)) != spec_digest(spec)


def test_holdout_shape_refusals():
    _refused(_pure(final=False), "only a stage designated final")
    _refused(
        _pure(holdout={"input": "chosen", "partition_digest": DIGEST, "claim_scope": SCOPE}),
        "must name a snapshot or artifact input",
    )
    _refused(
        _pure(holdout={"input": "labels", "partition_digest": "d" * 64, "claim_scope": SCOPE}),
        "does not match its input's digest",
    )
    _refused(
        _pure({"retry": {"max_attempts": 2, "transient_exit_codes": [7]}}),
        "retry.max_attempts of 1",
    )
    _refused(_pure({"retry": {"max_attempts": 1, "infra_retries": 1}}), "no infra_retries")
    _refused(_pure({"metrics": []}), "must declare the metrics")
    public = _pure()
    public["stages"][1]["inputs"][1]["data_class"] = "public"
    _refused(public, "cannot be declared public")


def test_a_final_stage_feeds_nothing_and_only_it_reads_the_sealed_partition():
    downstream = _pure()
    downstream["stages"].append(
        _stage(
            "after",
            [{"kind": "stage", "name": "score", "stage": "final", "outputs": ["result"]}],
            "x",
        )
    )
    _refused(downstream, "cannot feed another stage")
    peek = _pure()
    peek["stages"][0]["inputs"].append(dict(peek["stages"][1]["inputs"][1]))
    _refused(peek, "is the sealed partition of final")
    twice = _pure()
    second = json.loads(json.dumps(twice["stages"][1]))
    second["key"] = "final2"
    twice["stages"].append(second)
    _refused(twice, "sealed partition")


# -- runner and store -------------------------------------------------------------------


def test_the_final_stage_opens_the_claim_once_and_records_its_scored_metrics(tmp_path):
    world = _world(tmp_path)
    env = world.env
    labels = _labels(world)
    campaign = world.campaign(_spec(env, labels))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])

    (claim,) = _claims(world)
    assert (claim["partition_digest"], claim["claim_scope"], claim["stage_key"]) == (
        labels[1],
        SCOPE,
        "final",
    )
    assert claim["status"] == "scored"
    assert claim["scored_attempt"] == 1
    assert json.loads(claim["metrics_json"]) == {"test_score": 3.0}
    assert claim["approval_id"] == campaign["approval"]["id"]
    assert claim["spec_digest"] == campaign["spec_digest"]

    # Opened exactly once, after the selection stage succeeded and before the final stage's
    # input was released to the worker.
    (event,) = world.events_of("campaign.holdout_claimed")
    assert event["payload"]["claim_id"] == claim["id"]
    final = world.row(
        "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND stage_key='final'",
        campaign["id"],
    )
    released = [
        e
        for e in world.events_of("workspace.snapshot_released")
        if e["payload"]["release_id"] == final["release_id"]
    ]
    assert released and event["cursor"] < released[0]["cursor"]
    assert event["payload"]["attempt"] == 1

    receipt = json.loads(
        world.row("SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", campaign["id"])[
            "receipt_json"
        ]
    )
    (on_receipt,) = receipt["holdout_claims"]
    assert on_receipt["status"] == "scored"
    assert on_receipt["metrics"] == {"test_score": 3.0}
    stage = next(s for s in receipt["stages"] if s["stage_key"] == "final")
    assert stage["attempts"][0]["measurements"][0]["value"] == 3.0


def test_the_claim_never_opens_before_every_dependency_succeeded(tmp_path):
    from tessera_control import campaign_holdout
    from tessera_control.campaign_models import normalize, parse_spec_json
    from tessera_control.store import _connect

    world = _world(tmp_path)
    env = world.env
    campaign = world.campaign(_spec(env, _labels(world)))
    connection = _connect(env.state / "tessera.sqlite3")
    try:
        row = connection.execute("SELECT * FROM campaigns WHERE id=?", (campaign["id"],)).fetchone()
        stage = normalize(parse_spec_json(row["spec_json"])).stage("final")
        with pytest.raises(RuntimeError, match="before select succeeded"):
            campaign_holdout.open_claim(
                connection, row, campaign["approval"], stage, "2026-01-01T00:00:00Z"
            )
    finally:
        connection.close()
    assert _claims(world) == []


def test_a_repeat_claim_is_refused_at_submit_but_another_scope_is_a_new_claim(tmp_path):
    world = _world(tmp_path)
    env = world.env
    labels = _labels(world)
    campaign = world.campaign(_spec(env, labels))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])

    with pytest.raises(Conflict, match="already claimed"):
        _submit(env, _spec(env, labels, name="again"), "repeat")
    status, _ = _submit(env, _spec(env, labels, scope="study-a/other", name="other"), "other")
    assert status == 201
    # A non-holdout stage cannot read a partition once it is known to be sealed.
    peek = _spec(env, labels, name="peek")
    peek["stages"] = [
        _stage("peek", [peek["stages"][1]["inputs"][1]], "x"),
    ]
    with pytest.raises(Conflict, match="is a sealed partition"):
        _submit(env, peek, "peek")


def test_two_approved_campaigns_race_and_only_one_opens_the_partition(tmp_path):
    world = _world(tmp_path)
    env = world.env
    labels = _labels(world)
    first = world.campaign(_spec(env, labels, name="first"))
    # Its own code snapshot, so the two selection stages do not share one release.
    other_id, other_digest = _labels(world, b"print('other code')\n")
    code = {
        "kind": "snapshot",
        "name": "code",
        "snapshot_id": other_id,
        "digest": other_digest,
        "data_class": "private_derived",
    }
    second = world.campaign(
        _spec(env, labels, name="second", code=code)
    )  # approved before any claim
    world.tick_all(first["id"])
    world.tick_all(second["id"])
    assert world.status(first["id"]) == "succeeded", dump(world, first["id"])
    assert world.status(second["id"]) == "failed", dump(world, second["id"])

    (claim,) = _claims(world)
    assert claim["campaign_id"] == first["id"]
    refused = world.row(
        "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND stage_key='final'",
        second["id"],
    )
    assert refused is not None, dump(world, second["id"])
    assert refused["failure_class"] == "holdout_already_claimed"
    assert refused["release_id"] is None and refused["grant_id"] is None
    stop = world.row("SELECT stop_reason FROM campaigns WHERE id=?", second["id"])["stop_reason"]
    assert stop == "stage_failed:final:holdout_already_claimed"
    assert len(world.events_of("campaign.holdout_claimed")) == 1


def test_a_failed_scorer_spends_the_claim_and_the_partition_stays_sealed(tmp_path):
    world = _world(tmp_path)
    env = world.env
    labels = _labels(world)
    world.scenarios = [
        (0, {"data.json": json.dumps({"score": 1})}),
        (1, {"data.json": json.dumps({"score": 0})}),
    ]
    campaign = world.campaign(_spec(env, labels))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "failed", dump(world, campaign["id"])

    (claim,) = _claims(world)
    assert claim["status"] == "spent"
    assert claim["close_reason"] == "stage_failed"
    assert claim["metrics_json"] is None
    receipt = json.loads(
        world.row("SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", campaign["id"])[
            "receipt_json"
        ]
    )
    assert receipt["holdout_claims"][0]["status"] == "spent"
    with pytest.raises(Conflict, match="already claimed"):
        _submit(env, _spec(env, labels, name="retry"), "retry")
    # The identical spec under a new idempotency key is refused the same way.
    with pytest.raises(Conflict, match="already claimed"):
        _submit(env, _spec(env, labels), "same-spec-new-key")


def test_a_failed_dependency_never_opens_the_claim(tmp_path):
    world = _world(tmp_path)
    env = world.env
    labels = _labels(world)
    world.scenarios = [(1, {"data.json": json.dumps({"score": 0})})]
    campaign = world.campaign(_spec(env, labels))
    world.tick_all(campaign["id"])
    assert world.stages(campaign["id"])["final"] == "skipped", dump(world, campaign["id"])
    assert _claims(world) == []
    status, _ = _submit(env, _spec(env, labels, name="later"), "later")
    assert status == 201


def test_claims_are_permanent_and_readable_through_the_agent_read_path(tmp_path):
    world = _world(tmp_path)
    env = world.env
    labels = _labels(world)
    campaign = world.campaign(_spec(env, labels))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])

    with sqlite3.connect(env.state / "tessera.sqlite3") as db:
        with pytest.raises(sqlite3.IntegrityError, match="permanent"):
            db.execute("DELETE FROM campaign_holdout_claims")
        with pytest.raises(sqlite3.IntegrityError, match="cannot change"):
            db.execute("UPDATE campaign_holdout_claims SET status='opened'")

    reads = AgentCampaignReads(env.state)
    view = reads.campaign_holdout(env.owner, campaign["id"])
    (stage,) = view["evaluation_stages"]
    assert stage["stage_key"] == "final" and stage["final"] is True
    assert stage["holdout"]["partition_digest"] == labels[1]
    assert stage["claim"]["status"] == "scored"
    assert stage["claim"]["metrics"] == {"test_score": 3.0}
    listed = reads.holdout_claims(env.owner, env.project["id"], labels[1])
    assert [c["campaign_id"] for c in listed["claims"]] == [campaign["id"]]
    assert reads.holdout_claims(env.owner, env.project["id"], "e" * 64)["claims"] == []
    assert env.store.get_campaign(env.owner, campaign["id"])["holdout_claims"][0]["status"] == (
        "scored"
    )


def test_a_campaign_reapproved_after_opening_its_claim_keeps_it(tmp_path):
    world = _world(tmp_path)
    env = world.env
    labels = _labels(world)
    campaign = world.campaign(_spec(env, labels))
    world.tick_until(lambda: bool(_claims(world)))
    # The approver's delegation lapsed mid-run; a person re-approves the same digest.
    env.execute("UPDATE campaigns SET status='paused_delegation_lapsed' WHERE id=?", campaign["id"])
    current = world.row("SELECT revision, spec_digest FROM campaigns WHERE id=?", campaign["id"])
    env.approve({"id": campaign["id"], **dict(current)})
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    (claim,) = _claims(world)
    assert claim["status"] == "scored"
    assert len(world.events_of("campaign.holdout_claimed")) == 1


def _final_attempts(world: World, campaign_id: str) -> list:
    return world.env.query(
        "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND stage_key='final' "
        "ORDER BY attempt",
        campaign_id,
    )


def test_a_not_started_holdout_attempt_spends_the_claim_and_never_releases_again(
    tmp_path, monkeypatch
):
    """Review finding 1: an ADR 0046 not-started requeue would release the partition again."""
    from test_campaign_autoreconcile import _lapse, _refuse_launches

    world = _world(tmp_path)
    env = world.env
    campaign = world.campaign(_spec(env, _labels(world)))
    cid = campaign["id"]
    world.tick_until(lambda: world.stages(cid)["select"] == "succeeded")
    world.tick_until(
        lambda: any(a["status"] == "admitted" for a in _final_attempts(world, cid)),
        worker=False,
    )
    _refuse_launches(monkeypatch)
    world.poller.run(max_cycles=1)  # the worker leased the grant; Docker refused the launch
    monkeypatch.undo()
    _lapse(world, _final_attempts(world, cid)[0]["grant_id"])
    world.tick_all(cid)

    assert world.status(cid) == "failed", dump(world, cid)
    (only,) = _final_attempts(world, cid)
    assert (only["status"], only["failure_class"]) == ("failed_transient", "not_started")
    stop = world.row("SELECT stop_reason FROM campaigns WHERE id=?", cid)["stop_reason"]
    assert stop == "stage_failed:final:holdout_spent:not_started"
    (claim,) = _claims(world)
    assert claim["status"] == "spent" and claim["metrics_json"] is None
    released = [
        e
        for e in world.events_of("workspace.snapshot_released")
        if e["payload"]["release_id"] == only["release_id"]
    ]
    assert len(released) == 1
    assert len(world.events_of("campaign.holdout_claimed")) == 1


def _output_sha(world: World, campaign_id: str, stage_key: str, name: str = "result") -> str:
    return world.row(
        "SELECT sha256 FROM campaign_stage_io WHERE campaign_id=? AND stage_key=? "
        "AND direction='output' AND name=?",
        campaign_id,
        stage_key,
        name,
    )["sha256"]


def _pause(world: World, campaign_id: str, paused: int) -> None:
    env = world.env
    env.execute(
        """INSERT INTO campaign_steering (campaign_id, org_id, project_id, paused, pause_count,
        updated_at) VALUES (?,?,?,?,1,'2026-01-01T00:00:00Z')
        ON CONFLICT(campaign_id) DO UPDATE SET paused=excluded.paused""",
        campaign_id,
        env.project["org_id"],
        env.project["id"],
        paused,
    )


def test_a_campaign_approved_before_the_claim_is_refused_at_release(tmp_path):
    """Review finding 2: the sealed-input check runs again when the release is created."""
    world = _world(tmp_path)
    env = world.env
    labels = _labels(world)
    peek = _spec(env, labels, name="peek")
    peek["stages"] = [_stage("train", [peek["stages"][1]["inputs"][1]], "x")]
    reader = world.campaign(peek)  # approved while nothing seals the partition yet
    _pause(world, reader["id"], 1)
    holder = world.campaign(_spec(env, labels, name="holder"))
    world.tick_all(holder["id"])
    assert world.status(holder["id"]) == "succeeded", dump(world, holder["id"])
    _pause(world, reader["id"], 0)
    world.tick_all(reader["id"])

    assert world.status(reader["id"]) == "failed", dump(world, reader["id"])
    (attempt,) = world.attempts(reader["id"])
    assert attempt["failure_class"] == "sealed_partition"
    assert attempt["release_id"] is None and attempt["grant_id"] is None
    assert "is a sealed partition" in attempt["failure_detail"]


def test_a_holdout_stage_output_cannot_be_another_campaign_artifact_input(tmp_path):
    """Review finding 3: a held-out score never feeds another campaign."""
    world = _world(tmp_path)
    env = world.env
    holder = world.campaign(_spec(env, _labels(world), name="holder"))
    world.tick_all(holder["id"])
    assert world.status(holder["id"]) == "succeeded", dump(world, holder["id"])

    def consumer(stage_key: str) -> dict:
        source = {
            "kind": "artifact",
            "name": "score",
            "campaign_id": holder["id"],
            "stage": stage_key,
            "output": "result",
            "sha256": _output_sha(world, holder["id"], stage_key),
        }
        return {
            "version": 2,
            "name": "consumer",
            "budget_cap_cents": 10,
            "deadline_seconds": 3600,
            "stages": [_stage("next", [source], "x")],
        }

    with pytest.raises(Conflict, match="output of holdout stage final"):
        _submit(env, consumer("final"), "from-final")
    # The selection stage of the same campaign is an ordinary source.
    status, _ = _submit(env, consumer("select"), "from-select")
    assert status == 201


def test_a_holdout_input_can_be_an_artifact_pinned_by_sha256(tmp_path):
    """Review finding 5: the sealed partition is another campaign's output (an artifact)."""
    world = _world(tmp_path)
    env = world.env
    world.scenarios = [(0, {"data.json": json.dumps({"score": 9})})]
    source = world.campaign(
        {
            "version": 2,
            "name": "labels",
            "budget_cap_cents": 10,
            "deadline_seconds": 3600,
            "stages": [_stage("make", [_code(env)], "rows")],
        }
    )
    world.tick_all(source["id"])
    assert world.status(source["id"]) == "succeeded", dump(world, source["id"])
    sha = _output_sha(world, source["id"], "make")

    spec = _spec(env, ("unused", sha))
    spec["stages"][1]["inputs"][1] = {
        "kind": "artifact",
        "name": "labels",
        "campaign_id": source["id"],
        "stage": "make",
        "output": "result",
        "sha256": sha,
    }
    campaign = world.campaign(spec)
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    (claim,) = _claims(world)
    assert (claim["partition_digest"], claim["status"]) == (sha, "scored")
    with pytest.raises(Conflict, match="already claimed"):
        _submit(env, spec, "artifact-repeat")


def test_a_holdout_stage_with_alternate_providers_claims_once(tmp_path):
    """Review finding 5: a holdout stage placed among approved candidates (ADR 0060)."""
    import test_campaign_placement as placement_fixture

    made: list[str] = []

    def providers(provider_id, principal, slots=1):
        placement_fixture._register(provider_id, principal, slots)
        made.append(provider_id)

    try:
        world = placement_fixture._world(tmp_path, providers)
        env = world.env
        spec = _spec(env, _labels(world))
        for stage in spec["stages"]:
            stage["provider"] = placement_fixture.NEAR
        spec["stages"][1]["alternate_providers"] = [
            {"provider": placement_fixture.FAR, "runtime": "docker-cpu-v1"}
        ]
        campaign = world.campaign(spec)
        world.tick_all(campaign["id"])
        assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
        (claim,) = _claims(world)
        assert claim["status"] == "scored"
        (final,) = _final_attempts(world, campaign["id"])
        assert final["provider_id"] in {placement_fixture.NEAR, placement_fixture.FAR}
        assert len(world.events_of("campaign.holdout_claimed")) == 1
    finally:
        from tessera_control.campaign_providers import unregister_provider

        for provider_id in made:
            unregister_provider(provider_id)


# -- second review (shs2) ----------------------------------------------------------------


def test_an_approved_holdout_declaration_seals_the_partition_before_any_claim(tmp_path):
    """Findings 2/3: sealing starts at the first human approval of a holdout spec."""
    world = _world(tmp_path)
    env = world.env
    labels = _labels(world)
    peek = _spec(env, labels, name="peek")
    peek["stages"] = [_stage("train", [peek["stages"][1]["inputs"][1]], "x")]
    # A submitted but unapproved holdout spec seals nothing (an agent cannot block data).
    submitted = env.submit(spec=_spec(env, labels, name="pending"))
    status, _ = _submit(env, peek, "peek-before")
    assert status == 201
    env.approve(submitted)  # never run: no claim exists
    assert _claims(world) == []
    with pytest.raises(Conflict, match="is a sealed partition"):
        _submit(env, peek, "peek-after")
    # Another holdout stage may still name it as its own holdout input, under a new scope.
    status, _ = _submit(env, _spec(env, labels, scope="study-a/other", name="b"), "other")
    assert status == 201


def test_holdout_stage_outputs_cannot_be_staged_as_an_input_snapshot(tmp_path):
    """Finding 1: the snapshot-staging path is closed for holdout-stage outputs."""
    from tessera_control.dataset_staging import DatasetStagingStore

    world = _world(tmp_path)
    env = world.env
    campaign = world.campaign(_spec(env, _labels(world)))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    staging = DatasetStagingStore(env.state)

    def stage(stage_key: str):
        attempt = world.row(
            "SELECT manifest_id, manifest_digest FROM campaign_stage_attempts "
            "WHERE campaign_id=? AND stage_key=?",
            campaign["id"],
            stage_key,
        )
        payload = {
            "manifest_id": attempt["manifest_id"],
            "expected_manifest_digest": attempt["manifest_digest"],
            "expected_snapshot_digest": "0" * 64,
        }
        return staging.stage(env.human(), env.project["id"], f"stage-{stage_key}", payload)

    with pytest.raises(Conflict, match="holdout stage final"):
        stage("final")
    # The selection stage's outputs reach the ordinary checks (here: a wrong digest).
    with pytest.raises(Conflict) as error:
        stage("select")
    assert "holdout" not in str(error.value)
    assert env.query("SELECT 1 FROM events WHERE event_type='snapshot.staged_from_artifacts'") == []


def test_a_lost_race_at_the_insert_is_a_clean_refusal(tmp_path, monkeypatch):
    """Finding 4: open_claim returns the refusal even if the unique key decides the race."""
    from tessera_control import campaign_holdout
    from tessera_control.campaign_models import normalize, parse_spec_json
    from tessera_control.store import _connect

    world = _world(tmp_path)
    env = world.env
    labels = _labels(world)
    first = world.campaign(_spec(env, labels, name="first"))
    other_id, other_digest = _labels(world, b"print('other code')\n")
    code = {
        "kind": "snapshot",
        "name": "code",
        "snapshot_id": other_id,
        "digest": other_digest,
        "data_class": "private_derived",
    }
    second = world.campaign(_spec(env, labels, name="second", code=code))
    world.tick_all(first["id"])
    assert world.status(first["id"]) == "succeeded", dump(world, first["id"])

    real = campaign_holdout.existing_claim
    calls = []

    def stale(*args):  # the lookup misses the winner once, as an interleaved writer would
        calls.append(args)
        return None if len(calls) == 1 else real(*args)

    monkeypatch.setattr(campaign_holdout, "existing_claim", stale)
    env.execute(
        "UPDATE campaign_stages SET status='succeeded' WHERE campaign_id=? AND stage_key='select'",
        second["id"],
    )
    connection = _connect(env.state / "tessera.sqlite3")
    try:
        row = connection.execute("SELECT * FROM campaigns WHERE id=?", (second["id"],)).fetchone()
        stage = normalize(parse_spec_json(row["spec_json"])).stage("final")
        held, claim = campaign_holdout.open_claim(
            connection, row, second["approval"], stage, "2026-01-01T00:00:00Z"
        )
    finally:
        connection.close()
    assert held is False and claim["campaign_id"] == first["id"]
    assert len(calls) == 2
    (only,) = _claims(world)
    assert only["campaign_id"] == first["id"]


def _other_org(env) -> dict:
    now = "2026-01-01T00:00:00Z"
    env.execute("INSERT OR IGNORE INTO organizations VALUES ('org_two','Two',?)", now)
    env.execute(
        "INSERT OR IGNORE INTO principals (id, org_id, role, token_hash, revoked_at, created_at) "
        "VALUES ('p_two','org_two','human',?,NULL,?)",
        "f" * 64,
        now,
    )
    env.execute(
        "INSERT OR IGNORE INTO credentials "
        "(id, principal_id, kind, audience, project_id, parent_id, "
        "expires_at, revoked_at, created_at, created_by_principal_id) "
        "VALUES ('c_two','p_two','local_owner','control',NULL,NULL,NULL,NULL,?,'p_two')",
        now,
    )
    return {"id": "p_two", "org_id": "org_two", "kind": "local_owner", "automation_label": "t"}


def test_holdout_reads_are_tenant_scoped_and_capped(tmp_path):
    """Findings 5 and 6: another org sees nothing; the per-campaign read reports truncation."""
    from tessera_control.store import NotFound

    world = _world(tmp_path)
    env = world.env
    labels = _labels(world)
    campaign = world.campaign(_spec(env, labels))
    world.tick_all(campaign["id"])
    reads = AgentCampaignReads(env.state)
    stranger = _other_org(env)
    with pytest.raises(NotFound):
        reads.campaign_holdout(stranger, campaign["id"])
    with pytest.raises(NotFound):
        reads.holdout_claims(stranger, env.project["id"])
    with pytest.raises(NotFound):
        reads.holdout_claims(stranger, env.project["id"], labels[1])
    view = reads.campaign_holdout(env.owner, campaign["id"])
    assert view["truncated"] is False and len(view["evaluation_stages"]) == 1


def test_migration_71_to_72_adds_the_claim_table_and_its_triggers(tmp_path):
    """Finding 5: a populated v71 database gains the table and the triggers it would have
    fresh, and the triggers hold after migration."""
    from campaign_fixture import Env

    from tessera_control.migrations import migrate_database
    from tessera_control.store import _connect, validate_database

    env = Env(tmp_path)
    env.submit()  # populated: a campaign row exists
    path = env.state / "tessera.sqlite3"
    query = "SELECT name, sql FROM sqlite_schema WHERE tbl_name='campaign_holdout_claims'"
    fresh = dict(sqlite3.connect(path).execute(query).fetchall())
    assert len(fresh) >= 4  # table, its unique indexes and three triggers
    with sqlite3.connect(path) as db:
        for name in fresh:
            if name.startswith("campaign_holdout_claims_") and "autoindex" not in name:
                db.execute(f"DROP TRIGGER {name}")
        db.execute("DROP TABLE campaign_holdout_claims")
        db.execute("UPDATE schema_meta SET schema_version=71")
    assert migrate_database(path, _connect) == LATEST_SCHEMA_VERSION
    assert migrate_database(path, _connect) == LATEST_SCHEMA_VERSION  # a second run is a no-op
    validate_database(path)
    assert dict(sqlite3.connect(path).execute(query).fetchall()) == fresh
    campaign = env.query("SELECT * FROM campaigns")[0]
    approval_id = "approval_x"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(
            """INSERT INTO campaign_holdout_claims (id, org_id, project_id, partition_digest,
            claim_scope, campaign_id, stage_key, input_name, approval_id, spec_digest, status,
            opened_at, closed_at) VALUES ('h1',?,?,?,?,?,'final','labels',?,?,'scored',?,?)""",
            (
                campaign["org_id"],
                campaign["project_id"],
                "a" * 64,
                SCOPE,
                campaign["id"],
                approval_id,
                campaign["spec_digest"],
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:01Z",
            ),
        )
    with sqlite3.connect(path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="permanent"):
            db.execute("DELETE FROM campaign_holdout_claims")
        with pytest.raises(sqlite3.IntegrityError, match="cannot change"):
            db.execute("UPDATE campaign_holdout_claims SET metrics_json='{}'")


# -- third review follow-ups -------------------------------------------------------------------


def test_sealed_digests_read_only_approved_specs_and_skip_unparseable_ones(tmp_path):
    from tessera_control import campaign_holdout
    from tessera_control.store import _connect

    world = _world(tmp_path)
    env = world.env
    labels = _labels(world)
    project = env.project["id"]

    def sealed() -> set:
        connection = _connect(env.state / "tessera.sqlite3")
        try:
            return campaign_holdout.sealed_digests(connection, project)
        finally:
            connection.close()

    submitted = env.submit(spec=_spec(env, labels, name="pending"))
    assert labels[1] not in sealed()  # submitted, never approved: seals nothing
    env.approve(submitted)
    assert labels[1] in sealed()
    assert labels[1] in sealed()  # served from the per-campaign cache, same answer
    # A stored spec that now declares another digest replaces its cache entry.
    other = _labels(world, b"id,label\n22,1\n")
    env.execute(
        "UPDATE campaigns SET spec_json=? WHERE id=?",
        json.dumps(_spec(env, other, name="pending")),
        submitted["id"],
    )
    assert other[1] in sealed() and labels[1] not in sealed()
    # A stored spec that no longer parses is skipped, not fatal, and its cached digests are
    # not reused for the changed text.
    env.execute(
        "UPDATE campaigns SET spec_json=? WHERE id=?",
        '{"version": 2, "stages": [{"evaluation": {"holdout": "broken"}}]}',
        submitted["id"],
    )
    assert labels[1] not in sealed()


def test_the_insert_race_returns_the_stages_own_claim(tmp_path, monkeypatch):
    """open_claim's IntegrityError path when the row that won is this stage's own claim."""
    from tessera_control import campaign_holdout
    from tessera_control.campaign_models import normalize, parse_spec_json
    from tessera_control.store import _connect

    world = _world(tmp_path)
    env = world.env
    campaign = world.campaign(_spec(env, _labels(world)))
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    (own,) = _claims(world)

    class _Interleaved:
        """The first own-claim lookup misses, as if the claim were written just after it."""

        def __init__(self, connection):
            self.connection, self.missed = connection, False

        def execute(self, sql, params=()):
            if not self.missed and "FROM campaign_holdout_claims WHERE campaign_id=?" in sql:
                self.missed = True
                return self.connection.execute("SELECT 1 WHERE 0")
            return self.connection.execute(sql, params)

    monkeypatch.setattr(campaign_holdout, "existing_claim", lambda *args: None)
    connection = _connect(env.state / "tessera.sqlite3")
    try:
        row = connection.execute("SELECT * FROM campaigns WHERE id=?", (campaign["id"],)).fetchone()
        stage = normalize(parse_spec_json(row["spec_json"])).stage("final")
        proxy = _Interleaved(connection)
        held, claim = campaign_holdout.open_claim(
            proxy, row, campaign["approval"], stage, "2026-01-01T00:00:00Z"
        )
    finally:
        connection.close()
    assert proxy.missed
    assert held is True and claim["claim_id"] == own["id"]
    assert len(_claims(world)) == 1


def test_the_campaign_holdout_read_reports_truncation_at_the_cap(tmp_path, monkeypatch):
    from tessera_control import agent_campaign_reads

    world = _world(tmp_path)
    env = world.env
    campaign = world.campaign(_spec(env, _labels(world)))
    world.tick_all(campaign["id"])
    monkeypatch.setattr(agent_campaign_reads, "_ROW_CAP", 1)
    view = AgentCampaignReads(env.state).campaign_holdout(env.owner, campaign["id"])
    assert view["truncated"] is True and len(view["evaluation_stages"]) == 1


def test_declared_holdout_cache_is_bounded_and_thread_safe(monkeypatch) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from tessera_control import campaign_holdout as module

    monkeypatch.setattr(module, "_DECLARED", {})
    monkeypatch.setattr(module, "_DECLARED_LIMIT", 8)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: module._declared_holdouts(f"c{i}", "not json"), range(200)))
    assert len(module._DECLARED) <= 8
    assert module._declared_holdouts("c199", "not json") == frozenset()
