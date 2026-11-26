"""Completion-cancel recovery registers the same lineage the forward path does (audit M12)."""

from __future__ import annotations

from salad_campaign_fixture import SaladWorld, salad_provider_reset  # noqa: F401
from test_completion_cancel_success import (
    SHA,
    historical,
    output_rows,
    recover,
    run_with_cancel,
    settle,
)


def _sub(root, name):
    path = root / name
    path.mkdir()
    return path


def lineage(world: SaladWorld) -> tuple[list, list]:
    nodes = world.env.query(
        "SELECT * FROM artifact_lineage_nodes WHERE project_id=? ORDER BY sha256",
        world.row("SELECT project_id FROM campaigns WHERE id=?", world.campaign_id)["project_id"],
    )
    edges = world.env.query("SELECT * FROM artifact_lineage_edges ORDER BY id")
    return nodes, edges


def shape(world: SaladWorld) -> list[tuple]:
    nodes, edges = lineage(world)
    by_id = {n["id"]: n for n in nodes}
    return sorted(
        [(n["origin"], n["kind"], n["label"], n["operation_id"] is not None) for n in nodes]
        + [
            (
                "edge",
                by_id[e["child_node_id"]]["label"],
                by_id[e["parent_node_id"]]["label"],
                e["relation"],
                True,
            )
            for e in edges
        ]
    )


def test_recovery_registers_tessera_attempt_lineage_nodes_and_edges(tmp_path, monkeypatch):
    forward = SaladWorld(_sub(tmp_path, "forward"))
    run_with_cancel(forward, "execution_complete")
    settle(forward, 1)
    assert forward.status() == "succeeded", forward.dump()
    expected = shape(forward)

    world = SaladWorld(_sub(tmp_path, "recovered"))
    historical(world, monkeypatch)
    before = lineage(world)
    assert not [n for n in before[0] if n["sha256"] == SHA and n["origin"] == "tessera_attempt"]

    status, result = recover(world)
    assert status == 200, result
    attempt = world.attempt()
    nodes, edges = lineage(world)
    output = [n for n in nodes if n["sha256"] == SHA]
    assert len(output) == 1
    assert output[0]["origin"] == "tessera_attempt"
    assert output[0]["producing_attempt_id"] == attempt["admitted_attempt_id"]
    assert output[0]["operation_id"] == attempt["operation_id"]
    assert output[0]["artifact_id"] is not None
    assert output_rows(world)[0]["lineage_node_id"] == output[0]["id"]
    for edge in edges:
        assert edge["relation"] == "derived_from"
    assert shape(world) == expected

    # A replay (same key) and a fresh recovery leave lineage exactly as it was.
    assert recover(world)[0] == 200
    assert recover(world, key="recover-2")[0] == 200
    again_nodes, again_edges = lineage(world)
    assert [n["id"] for n in again_nodes] == [n["id"] for n in nodes]
    assert [e["id"] for e in again_edges] == [e["id"] for e in edges]
