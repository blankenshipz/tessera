"""Artifact lineage store, HTTP routes and export; synthetic state only, no providers."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_artifact_disposition_authority import _other_tenant_headers
from test_artifact_manifests import _completed_outputs
from test_credentials import issue, token
from test_goal_to_decision import _design
from test_result_acceptances import _checkpoint_with_receipt

from tessera_control.api import create_app
from tessera_control.artifact_lineage import ArtifactLineageStore
from tessera_control.artifact_lineage_api import router
from tessera_control.experiments import ExperimentStore
from tessera_control.store import (
    Conflict,
    Forbidden,
    NotFound,
    Store,
    StoreError,
    _connect,
    canonical_digest,
)

SHA = {name: name[0] * 64 for name in ("a", "b", "c", "d", "e", "f")}


def _call(method, actor, *args) -> dict[str, Any]:
    return method(actor, *args)


def _node(store, owner, project, key, **fields) -> dict[str, Any]:
    body = {
        "kind": "dataset",
        "sha256": SHA["a"],
        "label": "node",
        "origin": "external_import",
    } | fields
    return store.register_node(owner, project, key, body)[1]


def _edge(store, owner, project, key, child, parent, relation="derived_from"):
    body = {
        "child_node_id": child["id"],
        "parent_node_id": parent["id"],
        "relation": relation,
        "rationale": "because",
    }
    return store.record_edge(owner, project, key, body)[1]


@pytest.fixture
def world(tmp_path: Path):
    state, owner, _worker, grant, _lease, _manifest, first, checkpoint, _digest = (
        _checkpoint_with_receipt(tmp_path)
    )
    return state, owner, grant["project_id"], first, checkpoint, grant


def _admit_attempt(state, owner, grant, project) -> dict[str, Any]:
    records = ExperimentStore(state)
    design = _design(grant=grant)
    experiment = records.create_experiment(
        owner, project, "experiment", canonical_digest(design), design
    )[1]
    variant = experiment["design"]["variants"][0]
    admission = {
        "expected_variant_revision": variant["revision"],
        "expected_design_revision": experiment["current_design_revision"],
        "grant_id": grant["id"],
    }
    return records.admit_attempt(
        owner, variant["id"], "attempt", canonical_digest(admission), admission
    )[1]


def test_distillation_chain_and_reads(world) -> None:
    state, owner, project, *_ = world
    store = ArtifactLineageStore(state)
    fmt = {"framework": "pytorch", "format": "safetensors"}
    teacher = _node(store, owner, project, "t", kind="model_checkpoint", sha256=SHA["a"], **fmt)
    labels = _node(store, owner, project, "l", kind="labels", sha256=SHA["b"])
    student = _node(store, owner, project, "s", kind="model_checkpoint", sha256=SHA["c"], **fmt)
    assert teacher["authoritative_scope"] == "attributed_lineage_bookkeeping_only"
    _edge(store, owner, project, "e1", labels, teacher, "labels_from")
    _edge(store, owner, project, "e2", student, labels, "derived_from")
    _edge(store, owner, project, "e3", student, teacher, "trained_from")
    up = store.lineage(owner, project, student["id"], "ancestors")
    assert {n["id"]: n["depth"] for n in up["nodes"]} == {
        student["id"]: 0,
        labels["id"]: 1,
        teacher["id"]: 1,
    }
    assert len(up["edges"]) == 3
    down = store.lineage(owner, project, teacher["id"], "descendants", 1)
    assert {n["id"] for n in down["nodes"]} == {teacher["id"], labels["id"], student["id"]}
    shallow = store.lineage(owner, project, teacher["id"], "descendants", 1)
    assert {n["id"] for n in shallow["nodes"]} == {teacher["id"], labels["id"], student["id"]}
    assert store.get_node(owner, project, student["id"])["kind"] == "model_checkpoint"
    assert [n["id"] for n in store.list_nodes(owner, project, 0, 10, "labels")] == [labels["id"]]
    with pytest.raises(StoreError):
        store.lineage(owner, project, teacher["id"], "sideways")
    with pytest.raises(StoreError):
        store.lineage(owner, project, teacher["id"], "ancestors", 65)


def test_relation_rules_self_edge_and_cycles(world) -> None:
    state, owner, project, *_ = world
    store = ArtifactLineageStore(state)
    a, b, c = (_node(store, owner, project, k, sha256=SHA[k], label=k) for k in "abc")
    with pytest.raises(StoreError):
        _edge(store, owner, project, "self", a, a)
    _edge(store, owner, project, "ab", b, a)
    with pytest.raises(Conflict):
        _edge(store, owner, project, "ba", a, b, "derived_from")
    _edge(store, owner, project, "bc", c, b)
    with pytest.raises(Conflict):
        _edge(store, owner, project, "ca", a, c)
    with pytest.raises(Conflict):
        _edge(store, owner, project, "dup2", b, a, "derived_from")
    with pytest.raises(Conflict):
        _edge(store, owner, project, "bad-trained", c, a, "trained_from")
    with pytest.raises(Conflict):
        _edge(store, owner, project, "bad-labels", c, a, "labels_from")
    with pytest.raises(Conflict):
        _edge(store, owner, project, "bad-eval", c, a, "evaluated_on")


def test_node_validation(world) -> None:
    state, owner, project, first, checkpoint, grant = world
    store = ArtifactLineageStore(state)
    with pytest.raises(StoreError):
        _node(store, owner, project, "nofmt", kind="model_checkpoint")
    with pytest.raises(StoreError):
        _node(store, owner, project, "noattempt", origin="tessera_attempt")
    with pytest.raises(StoreError):
        _node(store, owner, project, "extra", origin="external_import", producing_attempt_id="x")
    with pytest.raises(StoreError):
        _node(store, owner, project, "badsha", sha256="A" * 64)
    with pytest.raises(StoreError):
        _node(store, owner, project, "extrafield", bogus=1)
    with pytest.raises(Conflict):
        _node(store, owner, project, "mismatch", artifact_id=first["id"], sha256=SHA["d"])
    ok = _node(store, owner, project, "bound", artifact_id=first["id"], sha256=first["sha256"])
    assert ok["artifact_id"] == first["id"]
    with pytest.raises(Conflict):
        _node(store, owner, project, "dupsha", sha256=first["sha256"])
    with pytest.raises(NotFound):
        _node(store, owner, project, "noart", artifact_id="missing", sha256=SHA["e"])
    with pytest.raises(NotFound):
        _node(store, owner, project, "nockpt", result_checkpoint_id="missing", sha256=SHA["e"])
    with pytest.raises(NotFound):
        _node(store, owner, project, "noop", operation_id="0" * 32, sha256=SHA["e"])
    with pytest.raises(NotFound):
        _node(
            store,
            owner,
            project,
            "noatt",
            origin="tessera_attempt",
            producing_attempt_id="missing",
            sha256=SHA["e"],
        )
    node = _node(
        store,
        owner,
        project,
        "ckpt",
        result_checkpoint_id=checkpoint["id"],
        operation_id=checkpoint["operation_id"],
        sha256=SHA["f"],
    )
    assert node["result_checkpoint_id"] == checkpoint["id"]
    with pytest.raises(Conflict):
        _node(
            store,
            owner,
            project,
            "wrongop",
            result_checkpoint_id=checkpoint["id"],
            operation_id="1" * 32,
            sha256=SHA["e"],
        )


def test_attempt_binding_requires_same_project_admitted_attempt(tmp_path: Path) -> None:
    state, _worker, _lease, first, _second = _completed_outputs(tmp_path)
    owner = Store(state).authenticate(
        (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    )
    from tessera_control.execution import ExecutionStore

    project = first["project_id"]
    grant = ExecutionStore(state).get_execution_grant(owner, first["grant_id"])
    store = ArtifactLineageStore(state)
    attempt = _admit_attempt(state, owner, grant, project)
    node = _node(
        store,
        owner,
        project,
        "att",
        kind="model_checkpoint",
        origin="tessera_attempt",
        producing_attempt_id=attempt["id"],
        framework="pytorch",
        format="gguf",
    )
    assert node["producing_attempt_id"] == attempt["id"]
    second = _second_project(state, owner["org_id"])
    with pytest.raises(NotFound):
        _node(
            store,
            owner,
            second,
            "xatt",
            origin="tessera_attempt",
            producing_attempt_id=attempt["id"],
            sha256=SHA["e"],
        )


def _second_project(state, org_id: str) -> str:
    with closing(_connect(state / "tessera.sqlite3")) as db:
        db.execute(
            "INSERT INTO projects (id, org_id, name, created_at) VALUES (?,?,?,?)",
            ("project_second", org_id, "second", "2026-01-01T00:00:00Z"),
        )
        db.commit()
    return "project_second"


def test_tenant_and_project_isolation_and_human_only(world) -> None:
    state, owner, project, first, *_ = world
    store = ArtifactLineageStore(state)
    node = _node(store, owner, project, "n1", sha256=SHA["a"])
    node2 = _node(store, owner, project, "n2", sha256=SHA["b"])
    second = _second_project(state, owner["org_id"])
    # Same org, other project: neither nodes nor edges are reachable or creatable.
    with pytest.raises(NotFound):
        store.get_node(owner, second, node["id"])
    other_node = _node(store, owner, second, "o1", sha256=SHA["a"])
    with pytest.raises(NotFound):
        _edge(store, owner, second, "x", other_node, node)
    with pytest.raises(NotFound):
        _edge(store, owner, second, "x2", node, other_node)
    with pytest.raises(NotFound):
        _node(store, owner, second, "art", artifact_id=first["id"], sha256=first["sha256"])
    # Other org.
    other_token = _other_tenant_headers(state)["Authorization"].removeprefix("Bearer ")
    outsider = Store(state).authenticate(other_token)
    with pytest.raises(NotFound):
        store.get_node(outsider, project, node["id"])
    with pytest.raises(NotFound):
        store.list_nodes(outsider, project, 0, 10)
    with pytest.raises(NotFound):
        store.export_lineage(outsider, project)
    with pytest.raises(NotFound):
        _edge(store, outsider, project, "ox", node, node2)
    with pytest.raises(NotFound):
        _node(store, outsider, project, "on", sha256=SHA["e"])
    # Non-human writers.
    client = TestClient(create_app(state))
    agent_value = token("lineage-agent")
    agent_cred, _ = issue(
        client,
        state,
        value=agent_value,
        kind="agent",
        audience="control",
        project_id=project,
        key="issue-lineage-agent",
    )
    non_human = Store(state).authenticate(agent_value)
    assert non_human["role"] == "agent" and agent_cred["principal_id"] == non_human["id"]
    with pytest.raises(Forbidden):
        _node(store, non_human, project, "agent", sha256=SHA["e"])
    with pytest.raises(Forbidden):
        _edge(store, non_human, project, "agent-edge", node, node2)


def test_idempotent_replay_and_append_only(world) -> None:
    state, owner, project, *_ = world
    store = ArtifactLineageStore(state)
    body = {"kind": "dataset", "sha256": SHA["a"], "label": "n", "origin": "external_import"}
    first = store.register_node(owner, project, "same", body)
    assert store.register_node(owner, project, "same", body) == first
    with pytest.raises(Conflict):
        store.register_node(owner, project, "same", body | {"label": "different"})
    node2 = _node(store, owner, project, "n2", sha256=SHA["b"])
    edge = _edge(store, owner, project, "e", node2, first[1])
    with closing(_connect(state / "tessera.sqlite3")) as db:
        for sql in (
            "UPDATE artifact_lineage_nodes SET label='x'",
            "DELETE FROM artifact_lineage_nodes",
            "UPDATE artifact_lineage_edges SET rationale='x'",
            "DELETE FROM artifact_lineage_edges",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                db.execute(sql)
        events = db.execute(
            "SELECT event_type FROM events WHERE event_type LIKE 'human.artifact_lineage_%'"
        ).fetchall()
    assert edge["relation"] == "derived_from"
    assert sorted(e[0] for e in events) == [
        "human.artifact_lineage_edge_recorded",
        "human.artifact_lineage_node_registered",
        "human.artifact_lineage_node_registered",
    ]


def test_export_is_deterministic(world) -> None:
    state, owner, project, *_ = world
    store = ArtifactLineageStore(state)
    a = _node(store, owner, project, "a", sha256=SHA["a"])
    b = _node(store, owner, project, "b", sha256=SHA["b"])
    _edge(store, owner, project, "e", b, a)
    one = store.export_lineage(owner, project)
    two = store.export_lineage(owner, project)
    assert one == two and one["version"] == 1 and one["project_id"] == project
    assert len(one["nodes"]) == 2 and len(one["edges"]) == 1
    assert one["digest"] == canonical_digest({"nodes": one["nodes"], "edges": one["edges"]})
    _node(store, owner, project, "c", sha256=SHA["c"])
    assert store.export_lineage(owner, project)["digest"] != one["digest"]


def test_http_routes(world) -> None:
    state, owner, project, *_ = world
    app: FastAPI = create_app(state)
    app.include_router(router)
    client = TestClient(app)
    token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    auth = {"Authorization": f"Bearer {token}"}
    base = f"/projects/{project}/artifact-lineage"
    nodes = []
    for index, name in enumerate("ab"):
        response = client.post(
            f"{base}/nodes",
            headers=auth | {"Idempotency-Key": f"n{index}"},
            json={
                "kind": "dataset",
                "sha256": SHA[name],
                "label": name,
                "origin": "external_import",
            },
        )
        assert response.status_code == 201, response.text
        nodes.append(response.json())
    edge = {
        "child_node_id": nodes[1]["id"],
        "parent_node_id": nodes[0]["id"],
        "relation": "derived_from",
        "rationale": "r",
    }
    assert (
        client.post(f"{base}/edges", headers=auth | {"Idempotency-Key": "e"}, json=edge).status_code
        == 201
    )
    reverse = edge | {"child_node_id": nodes[0]["id"], "parent_node_id": nodes[1]["id"]}
    assert (
        client.post(
            f"{base}/edges", headers=auth | {"Idempotency-Key": "r"}, json=reverse
        ).status_code
        == 409
    )
    assert client.post(f"{base}/nodes", headers=auth, json={}).status_code in (400, 422)
    assert client.get(f"{base}/nodes", headers=auth).json()["nodes"][0]["id"] == nodes[0]["id"]
    assert client.get(f"{base}/nodes/{nodes[1]['id']}", headers=auth).status_code == 200
    lineage = client.get(
        f"{base}/nodes/{nodes[1]['id']}/lineage?direction=ancestors&max_depth=3", headers=auth
    ).json()
    assert len(lineage["nodes"]) == 2 and len(lineage["edges"]) == 1
    assert client.get(f"{base}/export", headers=auth).json()["version"] == 1
    outsider = _other_tenant_headers(state)
    assert client.get(f"{base}/export", headers=outsider).status_code == 404
    assert client.get(f"{base}/nodes/{nodes[0]['id']}", headers=outsider).status_code == 404
    assert client.get(f"{base}/export").status_code == 401


def _freeze_clock(monkeypatch, values):
    """Make every lineage insert see the next scripted clock value (repeating the last)."""
    from tessera_control import artifact_lineage

    it = iter(values)
    last = {"value": values[-1]}

    def now() -> str:
        last["value"] = next(it, last["value"])
        return last["value"]

    monkeypatch.setattr(artifact_lineage, "utc_now", now)


def _chain(store, owner, project, count):
    nodes = [
        _node(store, owner, project, f"n{i}", sha256=f"{i:064x}", label=f"n{i}")
        for i in range(count)
    ]
    edges = [_edge(store, owner, project, f"e{i}", nodes[i], nodes[i - 1]) for i in range(1, count)]
    return nodes, edges


@pytest.mark.parametrize(
    "script",
    [
        # The clock never advances: every insert lands in the same tick.
        ["2030-01-01T00:00:00.500000Z"],
        # The clock steps backwards between inserts.
        [f"2030-01-01T00:00:{59 - i:02d}.000000Z" for i in range(30)],
    ],
    ids=["stalled-clock", "clock-steps-back"],
)
def test_listing_and_export_order_is_creation_order_whatever_the_clock_does(
    world, monkeypatch, script
) -> None:
    state, owner, project, *_ = world
    store = ArtifactLineageStore(state)
    _freeze_clock(monkeypatch, script)
    nodes, edges = _chain(store, owner, project, 12)
    listed = store.list_nodes(owner, project, 0, 200)
    assert [n["id"] for n in listed] == [n["id"] for n in nodes]
    created = [n["created_at"] for n in listed]
    assert created == sorted(set(created))  # strictly increasing, no ties left to break
    exported = store.export_lineage(owner, project)
    assert [n["id"] for n in exported["nodes"]] == [n["id"] for n in nodes]
    assert [e["id"] for e in exported["edges"]] == [e["id"] for e in edges]
    assert [e["created_at"] for e in exported["edges"]] == sorted(
        {e["created_at"] for e in exported["edges"]}
    )


def test_creation_times_keep_microsecond_format_and_a_normal_clock_is_unchanged(
    world, monkeypatch
) -> None:
    import re

    state, owner, project, *_ = world
    store = ArtifactLineageStore(state)
    _freeze_clock(monkeypatch, ["2030-01-01T00:00:00Z", "2030-01-01T00:00:05.250000Z"])
    first = _node(store, owner, project, "a", sha256=SHA["a"])
    second = _node(store, owner, project, "b", sha256=SHA["b"])
    shape = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z")
    assert shape.fullmatch(first["created_at"]) and shape.fullmatch(second["created_at"])
    # A clock that advances is used as it is (only ties and regressions are adjusted).
    assert first["created_at"] == "2030-01-01T00:00:00.000000Z"
    assert second["created_at"] == "2030-01-01T00:00:05.250000Z"


def test_the_ordering_fix_only_touches_the_project_it_inserts_into(world, monkeypatch) -> None:
    state, owner, project, *_ = world
    store = ArtifactLineageStore(state)
    other = _second_project(state, owner["org_id"])
    _freeze_clock(monkeypatch, ["2030-01-01T00:00:00.000000Z"])
    mine = _node(store, owner, project, "m", sha256=SHA["a"])
    theirs = _node(store, owner, other, "t", sha256=SHA["a"])
    # Same clock tick in two projects: neither is pushed later by the other's rows.
    assert mine["created_at"] == theirs["created_at"] == "2030-01-01T00:00:00.000000Z"


def _insert_copy(store, table, source_id, **changes):
    """Insert a copy of a row with other values (the tables reject UPDATE and DELETE).

    Used to put rows written before the ordering fix, without microseconds, next to new ones.
    """
    with _connect(store.database_path) as db:
        db.row_factory = sqlite3.Row
        row = dict(db.execute(f"SELECT * FROM {table} WHERE id=?", (source_id,)).fetchone())
        row.update(changes)
        columns = ", ".join(row)
        marks = ", ".join(f":{name}" for name in row)
        db.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", row)
    return row


def test_rows_written_without_microseconds_order_by_time_not_by_text(world, monkeypatch) -> None:
    state, owner, project, *_ = world
    store = ArtifactLineageStore(state)
    seed = _node(store, owner, project, "seed", sha256=SHA["a"])
    # As text "...:05Z" sorts AFTER "...:05.000001Z" ('Z' > '.'); as times it is earlier.
    first = _insert_copy(
        store, "artifact_lineage_nodes", seed["id"],
        id="lineage_node_legacy1", sha256=SHA["b"], created_at="2030-01-01T00:00:05Z",
    )  # fmt: skip
    second = _insert_copy(
        store, "artifact_lineage_nodes", seed["id"],
        id="lineage_node_legacy2", sha256=SHA["c"], created_at="2030-01-01T00:00:05.000001Z",
    )  # fmt: skip
    expected = [seed["id"], first["id"], second["id"]]
    assert [n["id"] for n in store.list_nodes(owner, project, 0, 200)] == expected
    # A new row on a stalled clock lands after the latest TIME, not after the text-greatest row.
    _freeze_clock(monkeypatch, ["2030-01-01T00:00:05.000000Z"])
    third = _node(store, owner, project, "c", sha256=SHA["d"])
    assert third["created_at"] == "2030-01-01T00:00:05.000002Z"
    expected.append(third["id"])
    assert [n["id"] for n in store.list_nodes(owner, project, 0, 200)] == expected
    assert [n["id"] for n in store.export_lineage(owner, project)["nodes"]] == expected


def test_edges_written_without_microseconds_also_order_by_time(world, monkeypatch) -> None:
    state, owner, project, *_ = world
    store = ArtifactLineageStore(state)
    a, b, c = (_node(store, owner, project, k, sha256=SHA[k]) for k in "abc")
    seed = _edge(store, owner, project, "e0", b, a)
    first = _insert_copy(
        store, "artifact_lineage_edges", seed["id"],
        id="lineage_edge_legacy1", relation="trained_from", created_at="2030-01-01T00:00:09Z",
    )  # fmt: skip
    second = _insert_copy(
        store, "artifact_lineage_edges", seed["id"],
        id="lineage_edge_legacy2", relation="labels_from",
        created_at="2030-01-01T00:00:09.000001Z",
    )  # fmt: skip
    expected = [seed["id"], first["id"], second["id"]]
    assert [e["id"] for e in store.export_lineage(owner, project)["edges"]] == expected
    _freeze_clock(monkeypatch, ["2030-01-01T00:00:09.000000Z"])
    third = _edge(store, owner, project, "e3", c, b)
    assert third["created_at"] == "2030-01-01T00:00:09.000002Z"
    expected.append(third["id"])
    assert [e["id"] for e in store.export_lineage(owner, project)["edges"]] == expected


def test_concurrent_writers_never_share_a_creation_time(world, monkeypatch) -> None:
    from concurrent.futures import ThreadPoolExecutor

    state, owner, project, *_ = world
    store = ArtifactLineageStore(state)
    _freeze_clock(monkeypatch, ["2030-01-01T00:00:00.000000Z"])  # every writer sees one tick

    def write(index: int) -> str:
        return _node(store, owner, project, f"w{index}", sha256=f"{index + 1:064x}")["id"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(write, range(24)))
    listed = store.list_nodes(owner, project, 0, 200)
    assert sorted(n["id"] for n in listed) == sorted(ids)
    times = [n["created_at"] for n in listed]
    # The read of the latest time and the insert share one write transaction (BEGIN
    # IMMEDIATE), so writers are serialized: all times are distinct and strictly increasing.
    assert len(set(times)) == len(times) == 24
    assert times == sorted(times)


def _plan(store, sql, params) -> str:
    with _connect(store.database_path) as db:
        return " | ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, params))


def test_listing_order_is_served_by_the_time_indexes_without_a_sort(world) -> None:
    from tessera_control import artifact_lineage as al

    state, owner, project, *_ = world
    store = ArtifactLineageStore(state)
    for table, columns, index in (
        ("artifact_lineage_nodes", al._NODE_COLUMNS, "artifact_lineage_nodes_by_time"),
        ("artifact_lineage_edges", al._EDGE_COLUMNS, "artifact_lineage_edges_by_time"),
    ):
        plan = _plan(
            store,
            f"SELECT {columns} FROM {table} WHERE project_id=? {al._ORDER}",
            (project,),
        )
        assert index in plan and "TEMP B-TREE" not in plan, plan
    paged = _plan(
        store,
        "SELECT id FROM artifact_lineage_nodes WHERE project_id=? AND (? IS NULL OR kind=?) "
        f"{al._ORDER} LIMIT ? OFFSET ?",
        (project, None, None, 50, 0),
    )
    assert "artifact_lineage_nodes_by_time" in paged and "TEMP B-TREE" not in paged, paged


def test_a_database_without_the_time_indexes_returns_the_same_order(world) -> None:
    state, owner, project, *_ = world
    store = ArtifactLineageStore(state)
    with _connect(store.database_path) as db:  # a database created before the indexes existed
        db.execute("DROP INDEX artifact_lineage_nodes_by_time")
        db.execute("DROP INDEX artifact_lineage_edges_by_time")
    nodes, edges = _chain(store, owner, project, 6)
    assert [n["id"] for n in store.list_nodes(owner, project, 0, 200)] == [n["id"] for n in nodes]
    exported = store.export_lineage(owner, project)
    assert [e["id"] for e in exported["edges"]] == [e["id"] for e in edges]


def test_creation_times_can_only_be_chosen_inside_a_write_transaction(tmp_path) -> None:
    from tessera_control import artifact_lineage as al

    connection = _connect(tmp_path / "t.sqlite3")
    connection.execute(
        "CREATE TABLE artifact_lineage_nodes (id TEXT, project_id TEXT, created_at TEXT)"
    )
    with pytest.raises(RuntimeError, match="inside the write transaction"):
        al._created_at(connection, "artifact_lineage_nodes", "p")
    connection.execute("BEGIN IMMEDIATE")
    assert al._created_at(connection, "artifact_lineage_nodes", "p").endswith("Z")
    connection.execute("ROLLBACK")
    with pytest.raises(ValueError):
        connection.execute("BEGIN IMMEDIATE")
        al._created_at(connection, "other_table", "p")


def test_the_latest_time_read_and_the_insert_share_one_begin_immediate_transaction(
    world, monkeypatch
) -> None:
    from tessera_control import store as store_module

    state, owner, project, *_ = world
    store = ArtifactLineageStore(state)
    statements: list[str] = []
    real_connect = store_module._connect

    def traced(path):
        connection = real_connect(path)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(store_module, "_connect", traced)
    _node(store, owner, project, "traced", sha256=SHA["e"])
    text = [s.strip() for s in statements]
    begin = text.index("BEGIN IMMEDIATE")
    read = next(
        i
        for i, s in enumerate(text)
        if s.startswith("SELECT created_at FROM artifact_lineage_nodes")
    )
    insert = next(
        i for i, s in enumerate(text) if s.startswith("INSERT INTO artifact_lineage_nodes")
    )
    commit = text.index("COMMIT")
    assert begin < read < insert < commit
    # Nothing commits or rolls back between the read of the latest time and the insert.
    assert not any(s in {"COMMIT", "ROLLBACK"} for s in text[begin + 1 : insert])
