"""Split locks, leakage audits, attempt binding and audited dataset snapshots."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import pytest
from gate_evidence_fixture import GateWorld
from test_artifact_disposition_authority import _other_tenant_headers
from test_dataset_ingestion import _snapshot_payload

from tessera_control.dataset_ingestion import DatasetIngestionStore
from tessera_control.gate_evidence_splits import lock_audit_status
from tessera_control.store import canonical_digest


@pytest.fixture
def world(tmp_path):
    return GateWorld(tmp_path)


METRICS = [{"name": "rmse", "partition": "test", "unit": "kcal/mol"}]


def _body(train, test, name="split", train_groups=None, test_groups=None, **extra):
    return {
        "name": name,
        "partitions": {
            "train": {"keys": train, **({"groups": train_groups} if train_groups else {})},
            "test": {"keys": test, **({"groups": test_groups} if test_groups else {})},
        },
        **extra,
    }


def _lock(world, body, **kwargs):
    path = f"/projects/{world.project['id']}/split-locks"
    return world.post(path, body, **kwargs)


def test_digest_is_server_computed_and_order_independent(world):
    first = _lock(world, _body(["a", "b"], ["c"]))
    assert first["within_lock_audit"]["status"] == "clean"
    assert first["scientific_validity"] == "not_assessed"
    assert first["member_count"] == 3
    assert "members" not in first and "keys" not in str(first["partitions"])
    # Same membership in another key order is the same split: duplicate, not a new lock.
    again = _body(["b", "a"], ["c"], name="renamed")
    world.post(
        f"/projects/{world.project['id']}/split-locks",
        again | {"expected_membership_digest": first["membership_digest"]},
        status=409,
    )
    world.post(
        f"/projects/{world.project['id']}/split-locks",
        _body(["z"], ["y"]) | {"expected_membership_digest": first["membership_digest"]},
        status=409,
    )
    # Digest is order-independent: reproduce it on a second project.
    other = world.post("/projects", {"name": "second"})
    second = world.post(f"/projects/{other['id']}/split-locks", again)
    assert second["membership_digest"] == first["membership_digest"]
    assert world.get(f"/split-locks/{first['id']}")["leakage_audits"][0]["kind"] == "within_lock"


def test_overlap_is_recorded_not_hidden_and_groups_are_separate(world):
    keyed = _lock(world, _body(["a", "b"], ["b", "c"]))
    audit = keyed["within_lock_audit"]
    assert audit["status"] == "overlap_detected"
    assert (audit["key_overlap_count"], audit["group_overlap_count"]) == (1, 0)
    assert audit["overlap_sample"]["keys"] == ["b"]
    grouped = _lock(
        world, _body(["a", "b"], ["c", "d"], train_groups=["s1", "s2"], test_groups=["s2", "s3"])
    )["within_lock_audit"]
    assert grouped["status"] == "overlap_detected"
    assert (grouped["key_overlap_count"], grouped["group_overlap_count"]) == (0, 1)
    assert grouped["overlap_sample"]["groups"] == ["s2"]


def test_cross_set_audit_clean_and_overlapping(world):
    corpus = _lock(world, {"name": "corpus", "partitions": {"pretrain": {"keys": ["p1", "p2"]}}})
    evalset = _lock(world, _body(["a"], ["c"]))
    path = f"/split-locks/{evalset['id']}/leakage-audits"
    clean = world.post(
        path,
        {
            "reference_lock_id": corpus["id"],
            "reference_partitions": ["pretrain"],
            "eval_partitions": ["test"],
        },
    )
    assert clean["status"] == "clean" and clean["kind"] == "cross_set"
    leaky_eval = _lock(world, _body(["a"], ["p2", "q"], name="leaky"))
    leaky = world.post(
        f"/split-locks/{leaky_eval['id']}/leakage-audits",
        {
            "reference_lock_id": corpus["id"],
            "reference_partitions": ["pretrain"],
            "eval_partitions": ["test"],
        },
    )
    assert leaky["status"] == "overlap_detected" and leaky["key_overlap_count"] == 1
    world.post(
        path,
        {
            "reference_lock_id": evalset["id"],
            "reference_partitions": ["test"],
            "eval_partitions": ["test"],
        },
        status=409,
    )
    world.post(
        path,
        {
            "reference_lock_id": corpus["id"],
            "reference_partitions": ["nope"],
            "eval_partitions": ["test"],
        },
        status=409,
    )
    with world.db() as connection:
        assert lock_audit_status(connection, evalset["id"], [corpus["id"], "none"]) == {
            "within_lock": "clean",
            "cross_set": {corpus["id"]: "clean", "none": "missing"},
        }
        assert lock_audit_status(connection, leaky_eval["id"], [corpus["id"]]) == {
            "within_lock": "clean",
            "cross_set": {corpus["id"]: "overlap_detected"},
        }
        assert lock_audit_status(connection, "missing", [])["within_lock"] == "missing"
        # Coverage is per partition: the clean audit covered only "test".
        assert lock_audit_status(connection, evalset["id"], [corpus["id"]], "train")[
            "cross_set"
        ] == {corpus["id"]: "missing"}
    # A later clean audit over a narrower partition set never masks a recorded overlap.
    world.post(
        f"/split-locks/{leaky_eval['id']}/leakage-audits",
        {
            "reference_lock_id": corpus["id"],
            "reference_partitions": ["pretrain"],
            "eval_partitions": ["train"],
        },
    )
    with world.db() as connection:
        assert lock_audit_status(connection, leaky_eval["id"], [corpus["id"]], "train")[
            "cross_set"
        ] == {corpus["id"]: "overlap_detected"}


def test_tenant_project_and_agent_boundaries(world):
    lock = _lock(world, _body(["a"], ["b"]))
    outsider = _other_tenant_headers(world.state)
    world.get(f"/split-locks/{lock['id']}", status=404, headers=outsider)
    world.post(
        f"/projects/{world.project['id']}/split-locks",
        _body(["x"], ["y"]),
        status=404,
        headers=outsider,
    )
    world.post(
        f"/split-locks/{lock['id']}/leakage-audits",
        {
            "reference_lock_id": lock["id"],
            "reference_partitions": ["train"],
            "eval_partitions": ["test"],
        },
        status=404,
        headers=outsider,
    )
    attempt = world.admitted_attempt()
    world.post(
        f"/experiment-attempts/{attempt}/split-binding",
        {"expected_revision": 1, "split_lock_id": lock["id"], "seed": 0, "metrics": METRICS},
        status=404,
        headers=outsider,
    )
    world.get(f"/experiment-attempts/{attempt}/split-binding", status=404, headers=outsider)
    # Lock in a different project of the same org cannot be bound to this attempt.
    other = world.post("/projects", {"name": "second"})
    foreign = world.post(f"/projects/{other['id']}/split-locks", _body(["m"], ["n"]))
    world.post(
        f"/experiment-attempts/{attempt}/split-binding",
        {"expected_revision": 1, "split_lock_id": foreign["id"], "seed": 0, "metrics": METRICS},
        status=409,
    )
    world.post(
        f"/projects/{world.project['id']}/split-locks",
        _body(["m"], ["n"], dataset_snapshot_id="dataset_nope"),
        status=404,
    )
    agent = {"Authorization": "Bearer synthetic_agent_" + "x" * 48}
    world.post(
        "/credentials",
        {
            "token": agent["Authorization"].removeprefix("Bearer "),
            "kind": "agent",
            "audience": "control",
            "project_id": world.project["id"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    world.post(
        f"/projects/{world.project['id']}/split-locks",
        _body(["q"], ["r"]),
        status=403,
        headers=agent,
    )
    world.post(
        f"/experiment-attempts/{attempt}/split-binding",
        {"expected_revision": 1, "split_lock_id": lock["id"], "seed": 0, "metrics": METRICS},
        status=403,
        headers=agent,
    )
    world.post(
        f"/split-locks/{lock['id']}/leakage-audits",
        {
            "reference_lock_id": lock["id"],
            "reference_partitions": ["train"],
            "eval_partitions": ["test"],
        },
        status=403,
        headers=agent,
    )


def test_idempotent_replay_and_key_reuse(world):
    path = f"/projects/{world.project['id']}/split-locks"
    body = _body(["a"], ["b"])
    first = world.post(path, body, key="k1")
    assert world.post(path, body, key="k1") == first
    world.post(path, _body(["a"], ["c"]), key="k1", status=409)
    with world.db() as connection:
        assert connection.execute("SELECT COUNT(*) FROM gate_split_locks").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='gate.split_lock_created'"
            ).fetchone()[0]
            == 1
        )


def test_binding_requires_admitted_state_fresh_revision_and_is_single(world):
    lock = _lock(world, _body(["a"], ["b"]))
    attempt = world.admitted_attempt()
    path = f"/experiment-attempts/{attempt}/split-binding"
    world.get(path, status=404)
    world.post(
        path,
        {"expected_revision": 7, "split_lock_id": lock["id"], "seed": 0, "metrics": METRICS},
        status=409,
    )
    bound = world.post(
        path, {"expected_revision": 1, "split_lock_id": lock["id"], "seed": 0, "metrics": METRICS}
    )
    assert bound["membership_digest"] == lock["membership_digest"]
    assert bound["attempt_state_at_binding"] == "admitted" and bound["attempt_revision"] == 2
    assert world.revision(attempt) == 2
    assert world.get(path)["split_lock_id"] == lock["id"]
    world.post(
        path,
        {"expected_revision": 2, "split_lock_id": lock["id"], "seed": 0, "metrics": METRICS},
        status=409,
    )
    running = world.admitted_attempt()
    with world.db() as connection:
        connection.execute(
            "UPDATE research_experiment_admitted_attempts SET state='running' WHERE id=?",
            (running,),
        )
    world.post(
        f"/experiment-attempts/{running}/split-binding",
        {"expected_revision": 1, "split_lock_id": lock["id"], "seed": 0, "metrics": METRICS},
        status=409,
    )
    with world.db() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='gate.attempt_split_bound'"
            ).fetchone()[0]
            == 1
        )


def test_audited_dataset_snapshot_requires_clean_lock(world):
    store = DatasetIngestionStore(world.state)
    owner = world.owner

    def cmd(method, *args, payload):
        status, value = method(
            owner,
            *args,
            f"k-{method.__name__}-{len(str(payload))}",
            canonical_digest(payload),
            payload,
        )
        assert status in (200, 201)
        return value

    study = cmd(
        store.create_study,
        world.project["id"],
        payload={"purpose": "Record provenance.", "protocol": "Review inputs."},
    )
    source = cmd(
        store.create_source,
        study["id"],
        payload={
            "uri": "https://example.invalid/x",
            "quoted_passage": "Local source declaration for a dataset snapshot.",
            "content_digest": "c" * 64,
            "locator": "manifest",
        },
    )
    source = cmd(
        store.assess_source,
        source["id"],
        payload={"expected_revision": source["revision"], "verdict": "supported", "reason": "ok"},
    )
    base = _snapshot_payload(source)
    path = f"/studies/{study['id']}/dataset-snapshots"

    def audited(lock_id, digest):
        body = copy.deepcopy(base)
        body["snapshot_digest"] = digest
        body["split_strategy"]["leakage_status"] = "audited"
        if lock_id is not None:
            body["split_strategy"]["split_lock_id"] = lock_id
        return body

    clean = _lock(world, _body(["a"], ["b"]))
    leaky = _lock(world, _body(["a"], ["a"], name="leaky"))
    world.post(path, audited(None, "1" * 64), status=422)
    declared = copy.deepcopy(base)
    declared["snapshot_digest"] = "2" * 64
    declared["split_strategy"]["split_lock_id"] = clean["id"]
    world.post(path, declared, status=422)
    world.post(path, audited("split_lock_nope", "3" * 64), status=409)
    world.post(path, audited(leaky["id"], "4" * 64), status=409)
    ok = world.post(path, audited(clean["id"], "5" * 64))
    assert ok["split_strategy"]["split_lock_id"] == clean["id"]
    plain = world.post(path, base)
    assert "split_lock_id" not in plain["split_strategy"]
    # A lock may link a snapshot of its own project.
    linked = _lock(world, _body(["p"], ["q"], dataset_snapshot_id=ok["id"]))
    assert linked["dataset_snapshot_id"] == ok["id"]


def test_binding_requires_seed_and_is_unique_per_variant_and_lock(world):
    lock = _lock(world, _body(["a"], ["b"]))
    a1, a2, a3 = (world.admitted_attempt() for _ in range(3))
    path = "/experiment-attempts/{}/split-binding"
    world.post(
        path.format(a1),
        {"expected_revision": 1, "split_lock_id": lock["id"], "metrics": METRICS},
        status=422,
    )
    for bad in (-1, "0", True, 1.5):
        world.post(
            path.format(a1),
            {"expected_revision": 1, "split_lock_id": lock["id"], "seed": bad, "metrics": METRICS},
            status=422,
        )
    body = {"expected_revision": 1, "split_lock_id": lock["id"], "seed": 4, "metrics": METRICS}
    assert world.post(path.format(a1), body)["seed"] == 4
    assert world.get(path.format(a1))["seed"] == 4
    world.post(path.format(a2), body, status=409)
    other_lock = _lock(world, _body(["c"], ["d"], name="other"))
    assert world.post(path.format(a2), {**body, "split_lock_id": other_lock["id"]})["seed"] == 4
    assert world.post(path.format(a3), {**body, "seed": 5})["seed"] == 5


def test_partial_reference_partition_audit_is_not_clean(world):
    corpus = _lock(
        world,
        {"name": "corpus", "partitions": {"web": {"keys": ["w1"]}, "code": {"keys": ["c1"]}}},
    )
    evalset = _lock(world, _body(["a"], ["x"]))
    path = f"/split-locks/{evalset['id']}/leakage-audits"
    world.post(
        path,
        {
            "reference_lock_id": corpus["id"],
            "reference_partitions": ["web"],
            "eval_partitions": ["test"],
        },
    )
    with world.db() as connection:
        assert lock_audit_status(connection, evalset["id"], [corpus["id"]], "test")[
            "cross_set"
        ] == {corpus["id"]: "missing"}
    world.post(
        path,
        {
            "reference_lock_id": corpus["id"],
            "reference_partitions": ["code", "web"],
            "eval_partitions": ["test"],
        },
    )
    with world.db() as connection:
        assert lock_audit_status(connection, evalset["id"], [corpus["id"]], "test")[
            "cross_set"
        ] == {corpus["id"]: "clean"}


def test_v60_database_migrates_to_v61_with_fresh_binding_schema(tmp_path):
    from tessera_control.migrations import LATEST_SCHEMA_VERSION, migrate_database

    def table_sql(connection):
        return connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='gate_attempt_split_bindings'"
        ).fetchone()[0]

    for name in ("fresh", "old"):
        (tmp_path / name).mkdir(mode=0o700)
    fresh = GateWorld(tmp_path / "fresh")
    connection = fresh.db()
    expected = table_sql(connection)
    connection.close()
    old = GateWorld(tmp_path / "old")
    connection = old.db()
    connection.execute("ALTER TABLE gate_attempt_split_bindings DROP COLUMN metrics_json")
    connection.execute("ALTER TABLE gate_attempt_split_bindings DROP COLUMN seed")
    connection.execute("UPDATE schema_meta SET schema_version=60 WHERE singleton=1")
    connection.commit()
    assert table_sql(connection) != expected
    connection.close()
    from tessera_control.store import _connect

    assert migrate_database(old.state / "tessera.sqlite3", _connect) == LATEST_SCHEMA_VERSION
    connection = old.db()
    assert table_sql(connection) == expected
    connection.close()


def test_binding_requires_declared_metrics_on_locked_partitions(world):
    lock = _lock(world, _body(["a"], ["b"]))
    path = "/experiment-attempts/{}/split-binding"
    a1, a2 = world.admitted_attempt(), world.admitted_attempt()
    base = {"expected_revision": 1, "split_lock_id": lock["id"], "seed": 1}
    world.post(path.format(a1), base, status=422)
    world.post(path.format(a1), {**base, "metrics": []}, status=422)
    dup = [METRICS[0], {**METRICS[0], "unit": "eV"}]
    world.post(path.format(a1), {**base, "metrics": dup}, status=422)
    bad = [{"name": "rmse", "partition": "nope", "unit": "u"}]
    world.post(path.format(a1), {**base, "metrics": bad}, status=409)
    two = [{"name": "zz", "partition": "train", "unit": "u"}, METRICS[0]]
    bound = world.post(path.format(a2), {**base, "metrics": two})
    assert [m["name"] for m in bound["metrics"]] == ["rmse", "zz"]
    assert world.get(path.format(a2))["metrics"] == bound["metrics"]
