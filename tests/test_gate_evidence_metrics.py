"""Typed metric records and platform-computed replicate groups (synthetic fixtures)."""

from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from gate_evidence_fixture import GateWorld

from tessera_control import gate_evidence_stats as stats
from tessera_control.gate_evidence_metrics import load_replicate_group
from tessera_control.store import canonical_digest, utc_now

PART = [{"name": "test", "member_count": 120, "group_count": 0, "digest": "a" * 64}]


@pytest.fixture
def world(tmp_path: Path) -> GateWorld:
    return GateWorld(tmp_path)


def make_lock(world: GateWorld, partitions=PART, digest: str | None = None) -> str:
    ident = "lock_" + uuid.uuid4().hex
    with world.db() as c:
        c.execute(
            """INSERT INTO gate_split_locks (id,project_id,name,membership_digest,partitions_json,
            member_count,created_at,created_by_principal_id) VALUES (?,?,?,?,?,?,?,?)""",
            (
                ident,
                world.project["id"],
                "lock",
                digest or uuid.uuid4().hex + uuid.uuid4().hex,
                json.dumps(partitions),
                120,
                utc_now(),
                world.owner["id"],
            ),
        )
    return ident


DECLARED = [{"name": "rmse", "partition": "test", "unit": "kcal/mol"}]


def bind(
    world: GateWorld,
    attempt_id: str,
    lock_id: str,
    seed: int | None = 0,
    metrics: list[dict] | None | str = "default",
) -> None:
    metrics_json = (
        json.dumps(DECLARED if metrics == "default" else metrics, sort_keys=True)
        if metrics is not None
        else None
    )
    with world.db() as c:
        digest = c.execute(
            "SELECT membership_digest FROM gate_split_locks WHERE id=?", (lock_id,)
        ).fetchone()[0]
        c.execute(
            """INSERT INTO gate_attempt_split_bindings (attempt_id,project_id,lock_id,
            membership_digest,attempt_state_at_binding,bound_at,bound_by_principal_id,seed,
            metrics_json) VALUES (?,?,?,?,'admitted',?,?,?,?)""",
            (
                attempt_id,
                world.project["id"],
                lock_id,
                digest,
                utc_now(),
                world.owner["id"],
                seed,
                metrics_json,
            ),
        )


def set_state(world: GateWorld, attempt_id: str, state: str) -> None:
    with world.db() as c:
        c.execute(
            "UPDATE research_experiment_admitted_attempts SET state=? WHERE id=?",
            (state, attempt_id),
        )


def seeded(world, lock, number, seed, *, outcome="succeeded", variant=None, record=True):
    attempt = world.admitted_attempt(variant)
    bind(world, attempt, lock, seed)
    measurement = world.human_measurement(attempt, number)
    world.set_outcome(attempt, outcome, None if outcome == "succeeded" else "synthetic failure")
    rid = None
    if record:
        rid = world.post(
            f"/experiment-attempts/{attempt}/metric-records",
            metric_body(measurement["id"], seed),
        )["id"]
    return attempt, measurement, rid


def metric_body(measurement_id, seed, **kw):
    return {"measurement_id": measurement_id, **kw}


def group_body(lock, **kw):
    return {
        "experiment_variant_id": kw.pop("variant_id"),
        "metric": "rmse",
        "split_lock_id": lock,
        "partition": "test",
        **kw,
    }


def group_path(world):
    return f"/projects/{world.project['id']}/replicate-groups"


# ---- statistics ----


def test_t_interval_matches_hand_computation():
    r = stats.aggregate([1, 2, 3], "t_interval_95", seed_material="x")
    assert (r["n"], r["mean"], r["std"], r["statistics_version"]) == (3, 2.0, 1.0, 1)
    half = 4.302653 / math.sqrt(3)
    assert r["ci_low"] == pytest.approx(2 - half, abs=1e-9)
    assert r["ci_high"] == pytest.approx(2 + half, abs=1e-9)
    assert half == pytest.approx(2.4842, abs=1e-4)


def test_t_table_uses_lower_df_and_normal_tail():
    assert stats.t_critical_975(35) == stats.t_critical_975(30)
    assert stats.t_critical_975(500) == 1.959964


def test_bootstrap_is_deterministic_and_bounded():
    a = stats.aggregate([1, 2, 3, 4], "percentile_bootstrap_95", seed_material="s")
    b = stats.aggregate([1, 2, 3, 4], "percentile_bootstrap_95", seed_material="s")
    c = stats.aggregate([1, 2, 3, 4], "percentile_bootstrap_95", seed_material="t")
    assert a == b
    assert a["ci_low"] <= a["mean"] <= a["ci_high"]
    assert 1 <= a["ci_low"] and a["ci_high"] <= 4
    assert c["ci_low"] <= c["mean"] <= c["ci_high"]


def test_zero_variance_collapses_ci():
    for m in ("t_interval_95", "percentile_bootstrap_95"):
        r = stats.aggregate([0.5, 0.5, 0.5], m, seed_material="z")
        assert r["std"] == 0 and r["ci_low"] == r["ci_high"] == r["mean"] == 0.5


@pytest.mark.parametrize("values", [[1.0], [], [1.0, float("nan")], [1.0, float("inf")]])
def test_aggregate_rejects_bad_input(values):
    with pytest.raises(ValueError):
        stats.aggregate(values, "t_interval_95", seed_material="x")


# ---- record_metric ----


def test_record_metric_reads_value_from_measurement(world):
    lock = make_lock(world)
    attempt, m, _ = seeded(world, lock, 0.25, 7, record=False)
    revision = world.revision(attempt)
    rec = world.post(f"/experiment-attempts/{attempt}/metric-records", metric_body(m["id"], 7))
    assert rec["value"] == 0.25 and rec["measurement_source"] == "human_entered"
    assert rec["sample_count"] == 120 and rec["lock_id"] == lock
    assert rec["variant_id"] == world.baseline["id"]
    assert rec["scientific_validity"] == "not_assessed"
    assert world.revision(attempt) == revision
    listed = world.get(f"/experiment-attempts/{attempt}/metric-records")["metric_records"]
    assert [r["id"] for r in listed] == [rec["id"]]
    with world.db() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM events WHERE event_type='gate.metric_recorded'"
            ).fetchone()[0]
            == 1
        )


def test_caller_cannot_supply_value(world):
    lock = make_lock(world)
    attempt, m, _ = seeded(world, lock, 1.0, 1, record=False)
    world.post(
        f"/experiment-attempts/{attempt}/metric-records",
        metric_body(m["id"], 1, value=99.0),
        status=422,
    )


def test_measurement_of_other_attempt_conflicts(world):
    lock = make_lock(world)
    attempt, _, _ = seeded(world, lock, 1.0, 1, record=False)
    other = world.admitted_attempt()
    other_m = world.human_measurement(other, 2.0)
    world.post(
        f"/experiment-attempts/{attempt}/metric-records", metric_body(other_m["id"], 1), status=409
    )


def test_unbound_attempt_conflicts(world):
    attempt = world.admitted_attempt()
    m = world.human_measurement(attempt, 1.0)
    r = world.post(
        f"/experiment-attempts/{attempt}/metric-records", metric_body(m["id"], 1), status=409
    )
    assert "pre-registered split lock" in json.dumps(r)


def test_caller_cannot_supply_partition_unit_metric_or_seed(world):
    lock = make_lock(world)
    attempt, m, _ = seeded(world, lock, 1.0, 1, record=False)
    path = f"/experiment-attempts/{attempt}/metric-records"
    for extra in ({"partition": "test"}, {"unit": "eV"}, {"metric": "x"}, {"seed": 1}):
        world.post(path, {**metric_body(m["id"], 1), **extra}, status=422)


def test_declared_partition_missing_from_lock_conflicts(world):
    lock = make_lock(world)
    attempt = world.admitted_attempt()
    bind(world, attempt, lock, 1, [{"name": "rmse", "partition": "valid", "unit": "u"}])
    m = world.human_measurement(attempt, 1.0)
    world.post(
        f"/experiment-attempts/{attempt}/metric-records", metric_body(m["id"], 1), status=409
    )


def test_val_declared_measurement_is_recorded_as_val(world):
    lock = make_lock(
        world,
        [
            {"name": "test", "member_count": 120, "group_count": 0, "digest": "a" * 64},
            {"name": "val", "member_count": 30, "group_count": 0, "digest": "b" * 64},
        ],
    )
    attempt = world.admitted_attempt()
    bind(world, attempt, lock, 1, [{"name": "rmse", "partition": "val", "unit": "eV"}])
    m = world.human_measurement(attempt, 1.0)
    rec = world.post(f"/experiment-attempts/{attempt}/metric-records", metric_body(m["id"], 1))
    assert rec["partition"] == "val" and rec["unit"] == "eV" and rec["sample_count"] == 30


def test_undeclared_measurement_name_conflicts(world):
    lock = make_lock(world)
    attempt = world.admitted_attempt()
    bind(world, attempt, lock, 1)
    m = world.human_measurement(attempt, 1.0, name="mae")
    r = world.post(
        f"/experiment-attempts/{attempt}/metric-records", metric_body(m["id"], 1), status=409
    )
    assert "not pre-registered" in json.dumps(r)


def test_binding_without_metrics_conflicts(world):
    lock = make_lock(world)
    attempt = world.admitted_attempt()
    bind(world, attempt, lock, 1, None)
    m = world.human_measurement(attempt, 1.0)
    r = world.post(
        f"/experiment-attempts/{attempt}/metric-records", metric_body(m["id"], 1), status=409
    )
    assert "predates metric pre-registration" in json.dumps(r)


@pytest.mark.parametrize("value", ["text", None, True, [1]])
def test_non_numeric_measurement_conflicts(world, value):
    lock = make_lock(world)
    attempt = world.admitted_attempt()
    bind(world, attempt, lock)
    m = world.human_measurement(attempt, 1.0)
    with world.db() as c:
        c.execute(
            "UPDATE research_admitted_attempt_measurements SET value_json=? WHERE id=?",
            (json.dumps({"number": value}), m["id"]),
        )
    world.post(
        f"/experiment-attempts/{attempt}/metric-records", metric_body(m["id"], 1), status=409
    )


def test_duplicates_conflict(world):
    lock = make_lock(world)
    attempt, m, _ = seeded(world, lock, 1.0, 1)
    world.post(
        f"/experiment-attempts/{attempt}/metric-records", metric_body(m["id"], 2), status=409
    )
    a2 = world.admitted_attempt()
    bind(world, a2, lock, 1)  # direct SQL bypasses the binding check: defence in depth
    m2 = world.human_measurement(a2, 3.0)
    world.post(f"/experiment-attempts/{a2}/metric-records", metric_body(m2["id"], 1), status=409)
    with world.db() as c:
        c.execute("UPDATE gate_attempt_split_bindings SET seed=2 WHERE attempt_id=?", (a2,))
    world.post(f"/experiment-attempts/{a2}/metric-records", metric_body(m2["id"], 2))


def test_agent_forbidden_and_cross_project_not_found(world):
    lock = make_lock(world)
    attempt, m, _ = seeded(world, lock, 1.0, 1, record=False)
    token = "a" * 43
    body = {
        "kind": "agent",
        "audience": "control",
        "project_id": world.project["id"],
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
    }
    world.experiments.issue_credential(
        world.owner, "agent-cred", canonical_digest(body), body, token
    )
    agent = {"Authorization": f"Bearer {token}"}
    world.post(
        f"/experiment-attempts/{attempt}/metric-records",
        metric_body(m["id"], 1),
        status=403,
        headers=agent,
    )
    other = world.post("/projects", {"name": "other"})
    # Project-scoped principal of another project cannot see the attempt.
    body2 = body | {"project_id": other["id"]}
    token2 = "b" * 43
    world.experiments.issue_credential(
        world.owner, "agent-cred2", canonical_digest(body2), body2, token2
    )
    world.get(
        f"/experiment-attempts/{attempt}/metric-records",
        status=404,
        headers={"Authorization": f"Bearer {token2}"},
    )


def test_record_idempotent_replay(world):
    lock = make_lock(world)
    attempt, m, _ = seeded(world, lock, 1.0, 1, record=False)
    path = f"/experiment-attempts/{attempt}/metric-records"
    first = world.post(path, metric_body(m["id"], 1), key="same-key")
    again = world.post(path, metric_body(m["id"], 1), key="same-key")
    assert first == again
    assert len(world.get(path)["metric_records"]) == 1


# ---- replicate groups ----


def test_group_aggregates_all_seeds_and_sources(world):
    lock = make_lock(world)
    ids = []
    for seed, number in enumerate([1.0, 2.0, 3.0]):
        _, m, rid = seeded(world, lock, number, seed)
        ids.append((m["id"], rid))
    world.mark_derived(ids[2][0])
    # Provenance is copied at record time: a later change does not alter recorded sources.
    g = world.post(group_path(world), group_body(lock, variant_id=world.baseline["id"]))
    assert g["n"] == 3 and g["mean"] == 2.0 and g["std"] == 1.0
    assert g["imputation"] == "none" and g["scientific_validity"] == "not_assessed"
    assert g["sources"] == ["human_entered"]
    assert g["min_sample_count"] == 120 and g["excluded_attempts"] == []
    assert [x["seed"] for x in g["members"]] == [0, 1, 2]
    assert g["ci_low"] <= g["mean"] <= g["ci_high"]
    fetched = world.get(f"/replicate-groups/{g['id']}")
    assert fetched["result_digest"] == g["result_digest"] and len(fetched["members"]) == 3
    with world.db() as c:
        assert load_replicate_group(c, world.project["id"], g["id"])["n"] == 3
        assert load_replicate_group(c, "project_other", g["id"]) is None


def test_group_sources_reflect_derived(world):
    lock = make_lock(world)
    for seed, number in enumerate([1.0, 2.0]):
        attempt = world.admitted_attempt()
        bind(world, attempt, lock, seed)
        m = world.human_measurement(attempt, number)
        if seed == 1:
            world.mark_derived(m["id"])
        world.set_outcome(attempt, "succeeded")
        world.post(f"/experiment-attempts/{attempt}/metric-records", metric_body(m["id"], seed))
    g = world.post(group_path(world), group_body(lock, variant_id=world.baseline["id"]))
    assert g["sources"] == ["derived", "human_entered"]


def test_group_refuses_while_bound_attempt_running(world):
    lock = make_lock(world)
    seeded(world, lock, 1.0, 0)
    seeded(world, lock, 2.0, 1)
    running = world.admitted_attempt()
    bind(world, running, lock, 2)
    set_state(world, running, "running")
    r = world.post(group_path(world), group_body(lock, variant_id=world.baseline["id"]), status=409)
    assert running in json.dumps(r)


def test_never_dispatched_seed_blocks_until_cancelled_with_reason(world):
    lock = make_lock(world)
    seeded(world, lock, 1.0, 0)
    seeded(world, lock, 3.0, 1)
    idle = world.admitted_attempt()
    bind(world, idle, lock, 2)  # pre-registered, state stays 'admitted'
    r = world.post(group_path(world), group_body(lock, variant_id=world.baseline["id"]), status=409)
    assert idle in r["detail"]  # aggregating only the seeds that ran is optional stopping
    # A reason is mandatory for cancellation.
    world.post(
        f"/experiment-admitted-attempts/{idle}/resolve",
        {"expected_revision": world.revision(idle), "outcome": "cancelled"},
        status=422,
    )
    world.post(
        f"/experiment-admitted-attempts/{idle}/resolve",
        {
            "expected_revision": world.revision(idle),
            "outcome": "cancelled",
            "reason": "Seed withdrawn before dispatch",
        },
    )
    g = world.post(group_path(world), group_body(lock, variant_id=world.baseline["id"]))
    assert g["n"] == 2
    assert g["excluded_attempts"] == [
        {"attempt_id": idle, "outcome": "cancelled", "reason": "Seed withdrawn before dispatch"}
    ]


def test_legacy_binding_without_metric_declarations_blocks_or_is_missing(world):
    lock = make_lock(world)
    seeded(world, lock, 1.0, 0)
    seeded(world, lock, 3.0, 1)
    legacy = world.admitted_attempt()
    bind(world, legacy, lock, 2, metrics=None)  # pre-v61 binding: declared nothing
    r = world.post(group_path(world), group_body(lock, variant_id=world.baseline["id"]), status=409)
    assert legacy in r["detail"]
    set_state(world, legacy, "succeeded")
    world.set_outcome(legacy, "succeeded")
    r = world.post(group_path(world), group_body(lock, variant_id=world.baseline["id"]), status=409)
    assert legacy in r["detail"] and "no metric record" in r["detail"]


def test_attempt_bound_for_other_metric_does_not_affect_group(world):
    lock = make_lock(world)
    seeded(world, lock, 1.0, 0)
    seeded(world, lock, 3.0, 1)
    other = world.admitted_attempt()
    bind(world, other, lock, 2, [{"name": "mae", "partition": "test", "unit": "u"}])
    set_state(world, other, "running")
    g = world.post(group_path(world), group_body(lock, variant_id=world.baseline["id"]))
    assert g["n"] == 2 and g["excluded_attempts"] == []


def test_group_refuses_succeeded_attempt_without_metric(world):
    lock = make_lock(world)
    seeded(world, lock, 1.0, 0)
    seeded(world, lock, 2.0, 1)
    missing, _, _ = seeded(world, lock, 3.0, 2, record=False)
    r = world.post(group_path(world), group_body(lock, variant_id=world.baseline["id"]), status=409)
    assert missing in json.dumps(r) and "record every succeeded seed" in json.dumps(r)


def test_failed_attempt_excluded_not_imputed(world):
    lock = make_lock(world)
    seeded(world, lock, 1.0, 0)
    seeded(world, lock, 3.0, 1)
    failed, _, _ = seeded(world, lock, 100.0, 2, outcome="failed")
    g = world.post(group_path(world), group_body(lock, variant_id=world.baseline["id"]))
    assert g["n"] == 2 and g["mean"] == 2.0
    assert g["excluded_attempts"] == [
        {"attempt_id": failed, "outcome": "failed", "reason": "synthetic failure"}
    ]


def test_group_needs_two_seeds(world):
    lock = make_lock(world)
    seeded(world, lock, 1.0, 0)
    world.post(group_path(world), group_body(lock, variant_id=world.baseline["id"]), status=409)


def test_group_refuses_mixed_units(world):
    lock = make_lock(world)
    seeded(world, lock, 1.0, 0)
    attempt = world.admitted_attempt()
    bind(world, attempt, lock, 1, [{"name": "rmse", "partition": "test", "unit": "eV"}])
    m = world.human_measurement(attempt, 2.0)
    world.set_outcome(attempt, "succeeded")
    world.post(f"/experiment-attempts/{attempt}/metric-records", metric_body(m["id"], 1))
    r = world.post(group_path(world), group_body(lock, variant_id=world.baseline["id"]), status=409)
    assert "units" in json.dumps(r)


def test_group_ignores_other_variant_and_cross_project(world):
    lock = make_lock(world)
    seeded(world, lock, 1.0, 0)
    seeded(world, lock, 2.0, 1)
    seeded(world, lock, 50.0, 0, variant=world.intervention)
    g = world.post(group_path(world), group_body(lock, variant_id=world.baseline["id"]))
    assert g["n"] == 2
    other = world.post("/projects", {"name": "other"})
    world.post(
        f"/projects/{other['id']}/replicate-groups",
        group_body(lock, variant_id=world.baseline["id"]),
        status=404,
    )


def test_group_bootstrap_and_replay(world):
    lock = make_lock(world)
    for seed, number in enumerate([1.0, 2.0, 4.0]):
        seeded(world, lock, number, seed)
    body = group_body(lock, variant_id=world.baseline["id"], ci_method="percentile_bootstrap_95")
    a = world.post(group_path(world), body, key="k1")
    assert world.post(group_path(world), body, key="k1") == a
    b = world.post(group_path(world), body)
    assert (a["ci_low"], a["ci_high"], a["result_digest"]) == (
        b["ci_low"],
        b["ci_high"],
        b["result_digest"],
    )


def test_group_requires_human(world):
    lock = make_lock(world)
    token = "c" * 43
    body = {
        "kind": "agent",
        "audience": "control",
        "project_id": world.project["id"],
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
    }
    world.experiments.issue_credential(world.owner, "ac", canonical_digest(body), body, token)
    world.post(
        group_path(world),
        group_body(lock, variant_id=world.baseline["id"]),
        status=403,
        headers={"Authorization": f"Bearer {token}"},
    )


def test_caller_cannot_relabel_metric_or_seed(world):
    lock = make_lock(world)
    attempt, m, _ = seeded(world, lock, 1.0, 5, record=False)
    path = f"/experiment-attempts/{attempt}/metric-records"
    world.post(path, metric_body(m["id"], 5, metric="other"), status=422)
    world.post(path, {**metric_body(m["id"], 5), "seed": 9}, status=422)
    rec = world.post(path, metric_body(m["id"], 5))
    assert rec["metric"] == "rmse" == m["name"] and rec["seed"] == 5


def test_metric_name_comes_from_measurement(world):
    lock = make_lock(world)
    attempt = world.admitted_attempt()
    bind(world, attempt, lock, 3, [{"name": "mae", "partition": "test", "unit": "u"}])
    m = world.human_measurement(attempt, 1.0, name="mae")
    rec = world.post(f"/experiment-attempts/{attempt}/metric-records", metric_body(m["id"], 3))
    assert rec["metric"] == "mae" and rec["seed"] == 3


def test_binding_without_seed_conflicts(world):
    lock = make_lock(world)
    attempt = world.admitted_attempt()
    bind(world, attempt, lock, None)
    m = world.human_measurement(attempt, 1.0)
    r = world.post(
        f"/experiment-attempts/{attempt}/metric-records", metric_body(m["id"], 0), status=409
    )
    assert "predates seed pre-registration" in json.dumps(r)


def test_seed_cancelled_before_dispatch_can_never_be_packaged(world):
    from tessera_control.experiments import admitted_attempt_package_identity
    from tessera_control.store import Conflict

    idle = world.admitted_attempt()
    world.post(
        f"/experiment-admitted-attempts/{idle}/resolve",
        {
            "expected_revision": world.revision(idle),
            "outcome": "cancelled",
            "reason": "Seed withdrawn before dispatch",
        },
    )
    with world.db() as connection, pytest.raises(Conflict, match="already resolved"):
        admitted_attempt_package_identity(
            connection,
            {"org_id": world.owner["org_id"]},
            {"project_id": world.project["id"]},
            idle,
        )
