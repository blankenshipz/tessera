"""Baseline-vs-intervention comparison over a receipt the real campaign runner wrote.

The sibling module inserts a hand-built receipt row; here the campaign runs to ``succeeded``
through the in-process harness, so the comparison reads the receipt shape the platform itself
produces. Synthetic data only; no provider or paid call."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from campaign_fixture import declare_snapshot_public
from campaign_runner_harness import World, dump, stage_spec
from fastapi.testclient import TestClient

from tessera_control.api import create_app
from tessera_control.snapshot_releases import SnapshotReleaseStore
from tessera_control.snapshots import SnapshotStore
from tessera_control.store import _connect, canonical_digest
from tessera_control.workspace import capture_workspace, inventory_digest

METRICS = [
    {"name": "acc", "path": "data.json", "json_pointer": "/acc"},
    {"name": "loss", "path": "data.json", "json_pointer": "/loss"},
]
VALUES = {"base": (0.5, 2.0), "int1": (0.75, 1.5), "int2": (0.25, 3.0)}
QUERY = (
    "baseline=base&intervention=int1&intervention=int2&metric=acc&metric=loss&evaluation_input=eval"
)


def _eval_snapshot(world: World, label: str) -> dict:
    """Adopt, release and declare public a second snapshot to serve as an evaluation input."""
    env = world.env
    root = world.tmp / f"eval-{label}"
    root.mkdir()
    (root / "eval.txt").write_bytes(f"held-out {label}".encode())
    capture = capture_workspace(root.absolute())
    digest = inventory_digest(capture.inventory)
    _, snapshot = SnapshotStore(env.state).adopt_snapshot(
        env.owner, env.project["id"], f"adopt-{label}", digest, capture
    )
    payload = {
        "expected_digest": digest,
        "worker_principal_id": env.worker["id"],
        "classification": "public",
        "permits_worker_transfer": True,
        "rationale": "Synthetic evaluation input release.",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
    }
    SnapshotReleaseStore(env.state).release_snapshot(
        env.owner, snapshot["id"], f"release-{label}", canonical_digest(payload), payload
    )
    declare_snapshot_public(env.state, snapshot["id"])
    return {
        "kind": "snapshot",
        "name": "eval",
        "snapshot_id": snapshot["id"],
        "digest": snapshot["digest"],
        "data_class": "public",
    }


def _run_campaign(world: World, evals: dict[str, dict], *, fail: str | None = None) -> str:
    env = world.env
    code = stage_spec(env, "x")["inputs"]
    stages = [stage_spec(env, key, inputs=[*code, evals[key]], metrics=METRICS) for key in VALUES]
    spec = env.spec()
    spec["stages"] = stages
    campaign = world.campaign(spec)
    world.scenarios = [
        (9 if key == fail else 0, {"data.json": json.dumps({"acc": a, "loss": b})})
        for key, (a, b) in VALUES.items()
    ]
    if fail is None:
        world.tick_all(campaign["id"])
        assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    return campaign["id"]


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def _client(world: World):
    client = TestClient(create_app(world.env.state))
    header = (world.env.state / "owner.header").read_text().strip().split(": ", 1)[1]
    return client, {"Authorization": header}


def _receipt(world: World, cid: str) -> dict:
    row = world.row("SELECT * FROM campaign_receipts WHERE campaign_id=?", cid)
    return json.loads(row["receipt_json"]), row["receipt_digest"]


def test_real_receipt_comparison_deltas_identity_and_no_verdict(world):
    shared = _eval_snapshot(world, "a")
    cid = _run_campaign(world, dict.fromkeys(VALUES, shared))
    client, headers = _client(world)
    response = client.get(f"/campaigns/{cid}/comparison?{QUERY}", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    receipt, digest = _receipt(world, cid)
    assert body["campaign_id"] == cid and body["receipt_digest"] == digest
    assert body["scientific_validity"] == "not_assessed"
    assert not {"decision", "verdict", "recommendation"} & set(body)
    # Identity is what the real receipt recorded for the named input, not a caller claim.
    recorded = {
        s["stage_key"]: [
            i for i in s["attempts"][0]["io"] if i["direction"] == "input" and i["name"] == "eval"
        ]
        for s in receipt["stages"]
    }
    assert all(len(items) == 1 for items in recorded.values())
    item = recorded["base"][0]
    assert item["snapshot_id"] == shared["snapshot_id"]
    identity = body["evaluation_inputs"]["eval"]
    assert identity["identity_field"] in ("sha256", "snapshot_id")
    assert identity["sha256"] == item[identity["identity_field"]]
    assert sorted(identity["arms"]) == sorted(VALUES)
    # Deltas are intervention minus baseline of the recorded measurements.
    attempts = {s["stage_key"]: s["attempts"][0] for s in receipt["stages"]}
    by_name = {m["name"]: m for m in body["metrics"]}
    for index, name in enumerate(("acc", "loss")):
        row = by_name[name]
        assert row["baseline"]["value"] == VALUES["base"][index]
        for arm in row["interventions"]:
            assert arm["value"] == VALUES[arm["stage_key"]][index]
            assert arm["delta"] == pytest.approx(
                VALUES[arm["stage_key"]][index] - row["baseline"]["value"]
            )
            assert arm["run_receipt_digest"] == attempts[arm["stage_key"]]["run_receipt_digest"]
    assert body["baseline"]["run_receipt_digest"] == attempts["base"]["run_receipt_digest"]
    assert body["baseline"]["evidence_digest"] == attempts["base"]["evidence_digest"]
    assert body["baseline"]["run_receipt_digest"]


def test_real_receipt_with_a_different_evaluation_snapshot_is_409(world):
    shared = _eval_snapshot(world, "a")
    other = _eval_snapshot(world, "b")
    assert other["snapshot_id"] != shared["snapshot_id"]
    cid = _run_campaign(world, {"base": shared, "int1": shared, "int2": other})
    client, headers = _client(world)
    response = client.get(f"/campaigns/{cid}/comparison?{QUERY}", headers=headers)
    assert response.status_code == 409
    assert "evaluation_inputs_differ" in response.json()["detail"]


def test_non_terminal_campaign_has_no_receipt_and_is_404(world):
    # get_campaign_receipt raises NotFound when no campaign_receipts row exists yet.
    shared = _eval_snapshot(world, "a")
    env = world.env
    code = stage_spec(env, "x")["inputs"]
    spec = env.spec()
    spec["stages"] = [
        stage_spec(env, key, inputs=[*code, shared], metrics=METRICS) for key in VALUES
    ]
    campaign = world.campaign(spec)  # approved, runner has not ticked
    assert world.status(campaign["id"]) != "succeeded"
    assert world.row("SELECT 1 FROM campaign_receipts WHERE campaign_id=?", campaign["id"]) is None
    client, headers = _client(world)
    response = client.get(f"/campaigns/{campaign['id']}/comparison?{QUERY}", headers=headers)
    assert response.status_code == 404


def test_another_project_cannot_read_the_comparison_404(world):
    shared = _eval_snapshot(world, "a")
    cid = _run_campaign(world, dict.fromkeys(VALUES, shared))
    client, headers = _client(world)
    assert client.get(f"/campaigns/{cid}/comparison?{QUERY}", headers=headers).status_code == 200
    # Move the campaign to a project the caller does not belong to.
    with _connect(world.env.state / "tessera.sqlite3") as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("UPDATE campaigns SET project_id='project_other' WHERE id=?", (cid,))
    response = client.get(f"/campaigns/{cid}/comparison?{QUERY}", headers=headers)
    assert response.status_code == 404


def test_a_failed_arm_attempt_is_not_a_result_409(world):
    shared = _eval_snapshot(world, "a")
    cid = _run_campaign(world, dict.fromkeys(VALUES, shared), fail="int1")
    world.tick_all(cid)
    receipt, _ = _receipt(world, cid)
    statuses = {s["stage_key"]: [a["status"] for a in s["attempts"]] for s in receipt["stages"]}
    assert "succeeded" not in statuses["int1"], statuses
    client, headers = _client(world)
    response = client.get(f"/campaigns/{cid}/comparison?{QUERY}", headers=headers)
    assert response.status_code == 409
    assert "arm_attempt_not_unique" in response.json()["detail"]
