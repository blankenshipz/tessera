"""The recorded live path of a campaign Salad route in the public route view (read-only)."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from test_remote_execution_routing import setup_route

from tessera_control.campaign_route_live_path import (
    builtin_proxy_view,
    campaign_live_path,
)
from tessera_control.store import LOCAL_ORG_ID, _connect, initialize_state

NOW = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)
ORG = LOCAL_ORG_ID
WORKER_PRINCIPAL = "principal_worker_secret_marker"
GRANT = "grant_secret_marker"
FORBIDDEN = (WORKER_PRINCIPAL, GRANT, "credential", "token")


def _iso(delta_seconds: float) -> str:
    return (NOW + timedelta(seconds=delta_seconds)).isoformat().replace("+00:00", "Z")


def _insert(db, table, **values):
    """Insert one row, filling any other required column with a neutral placeholder."""
    for col in db.execute(f"PRAGMA table_info({table})").fetchall():
        if col["name"] in values or col["pk"] and col["type"] == "INTEGER":
            continue
        if col["notnull"] and col["dflt_value"] is None:
            values[col["name"]] = 1 if col["type"] == "INTEGER" else "x"
    names = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    db.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})", tuple(values.values()))


def seed(
    db,
    *,
    route_id="route_1",
    org=ORG,
    phase="executing",
    lease="held",  # held | expired | none
    worker="fresh",  # fresh | stale | retired | missing
    lifecycle=None,
    credential_revoked=None,
    credential_expires=None,
    registry_project="x",
    evidence=None,
    receipt=None,
    final=None,
    suffix="",
):
    cid, aid, lid = "campaign" + suffix, "attempt" + suffix, "lease" + suffix
    _insert(
        db,
        "campaigns",
        id=cid,
        org_id=org,
        project_id="x",
        spec_digest="a" * 64,
        status="running",
        budget_cap_cents=100,
        revision=1,
    )
    _insert(
        db,
        "campaign_stage_attempts",
        id=aid,
        campaign_id=cid,
        stage_key="train",
        attempt=1,
        status="running",
        hold_cents=0,
        revision=1,
        grant_id=GRANT + suffix,
    )
    _insert(
        db,
        "execution_grants",
        id=GRANT + suffix,
        worker_principal_id=WORKER_PRINCIPAL + suffix,
        currency="USD",
        status="active",
    )
    lease_id = None if lease == "none" else lid
    if lease_id:
        expires = _iso(120 if lease == "held" else -30)
        _insert(db, "execution_leases", id=lid, expires_at=expires, renewed_at=_iso(-10))
    _insert(
        db,
        "campaign_salad_attempts",
        attempt_id=aid,
        phase=phase,
        lease_id=lease_id,
        route_id=route_id,
        salad_evidence_digest=evidence,
        live_receipt_digest=receipt,
        guardian_final_json=final,
        revision=1,
        updated_at=_iso(0),
    )
    if worker != "missing":
        seen = {"fresh": -5, "stale": -300, "retired": -5}[worker]
        _insert(
            db,
            "worker_registry",
            id="worker" + suffix,
            org_id=org,
            worker_principal_id=WORKER_PRINCIPAL + suffix,
            lifecycle_state=lifecycle or ("retired" if worker == "retired" else "ready"),
            revision=1,
            last_seen_at=_iso(seen),
            credential_id="cred" + suffix,
            project_id=registry_project,
        )
        _insert(
            db,
            "credentials",
            id="cred" + suffix,
            principal_id=WORKER_PRINCIPAL + suffix,
            kind="worker",
            audience="worker",
            project_id=None,
            revoked_at=credential_revoked,
            expires_at=credential_expires or _iso(3600),
        )


@pytest.fixture
def db(tmp_path):
    database, _ = initialize_state(tmp_path / "state")
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def view(db, org=ORG, route="route_1"):
    return campaign_live_path(db, org, route, now=NOW)


def test_no_owning_attempt_is_none(db):
    assert view(db) is None
    seed(db)
    assert view(db, route="other_route") is None


def test_other_org_is_none(db):
    seed(db)
    assert view(db, org="org_someone_else") is None


def test_ready_when_fresh_heartbeat_and_lease_held(db):
    seed(db)
    v = view(db)
    assert v["readiness"] == "ready" and v["reasons"] == []
    assert v["dispatcher"] == "salad_executor_guardian"
    assert v["applies_to"] == "campaign_stage" and v["attempt_phase"] == "executing"
    assert v["executor"]["registered"] and v["executor"]["freshness"] == "fresh"
    assert v["executor"]["heartbeat_age_seconds"] == 5
    assert v["executor"]["lifecycle_state"] == "ready"
    assert v["lease"]["state"] == "held" and v["lease"]["expires_in_seconds"] == 120
    assert v["lease"]["renewed_age_seconds"] == 10


def test_stale_heartbeat_is_degraded(db):
    seed(db, worker="stale")
    v = view(db)
    assert v["readiness"] == "degraded" and v["reasons"] == ["executor_heartbeat_stale"]
    assert v["executor"]["freshness"] == "stale" and v["executor"]["heartbeat_age_seconds"] == 300


def test_retired_worker_is_not_ready(db):
    seed(db, worker="retired")
    v = view(db)
    assert v["readiness"] == "not_ready" and v["reasons"] == ["executor_retired"]


def test_missing_worker_is_not_ready(db):
    seed(db, worker="missing")
    v = view(db)
    assert v["readiness"] == "not_ready" and v["reasons"] == ["executor_not_registered"]
    assert v["executor"] == {"registered": False}


def test_expired_lease_is_degraded(db):
    seed(db, lease="expired")
    v = view(db)
    assert v["readiness"] == "degraded" and v["reasons"] == ["lease_expired"]
    assert v["lease"]["state"] == "expired" and v["lease"]["expires_in_seconds"] == 0


def test_awaiting_lease_without_lease_is_ready(db):
    seed(db, phase="awaiting_lease", lease="none")
    v = view(db)
    assert v["readiness"] == "ready" and "no_lease" not in v["reasons"]
    assert v["lease"] == {"state": "none"}


def test_active_phase_past_leased_without_lease_reports_no_lease(db):
    seed(db, phase="route_started", lease="none")
    v = view(db)
    assert v["reasons"] == ["no_lease"] and v["readiness"] == "degraded"


@pytest.mark.parametrize("phase", ["outcome_recorded", "awaiting_charge", "settled", "unknown"])
def test_settled_phases_are_finished(db, phase):
    seed(db, phase=phase, lease="none")
    assert view(db)["readiness"] == "finished"


@pytest.mark.parametrize(
    ("evidence", "receipt", "final", "expected"),
    [
        (None, None, None, (False, False, False)),
        ("e" * 64, None, None, (True, False, False)),
        (None, "r" * 64, None, (False, True, False)),
        ("e" * 64, "r" * 64, json.dumps({"state": "done"}), (True, True, True)),
    ],
)
def test_guardian_booleans_follow_recorded_columns(db, evidence, receipt, final, expected):
    seed(db, evidence=evidence, receipt=receipt, final=final)
    g = view(db)["guardian"]
    assert (g["evidence_recorded"], g["live_receipt_recorded"], g["final_recorded"]) == expected
    assert g["conformance"] == "not_attested_by_this_view"


@pytest.mark.parametrize("kw", [{}, {"worker": "stale"}, {"phase": "settled", "lease": "none"}])
def test_authorizes_nothing_and_leaks_no_identifiers(db, kw):
    seed(db, **kw)
    v = view(db)
    assert v["authorizes_nothing"] is True
    assert v["guardian"]["conformance"] == "not_attested_by_this_view"
    text = json.dumps(v).lower()
    for marker in FORBIDDEN:
        assert marker.lower() not in text


def test_builtin_proxy_view_never_enabled():
    from tessera_control.salad_proxy_contract import safety_projection

    built = builtin_proxy_view(safety_projection())
    assert built["state"] == "disabled"
    assert built["go_no_go"] == "no_go_live_activation_blocked"
    assert built["control_surface"] == "desktop_control_only"
    assert "enabled" not in json.dumps(built).lower().replace("disabled", "")
    assert built["proxy_safety"]["transport"] == "disabled"


# --- integration through RemoteExecutionStore.get ---------------------------------------------


def _snapshot(h):
    db = _connect(h.store.database_path)
    try:
        events = db.execute("SELECT COUNT(*), COALESCE(MAX(cursor), 0) FROM events").fetchone()
        ops = [tuple(r) for r in db.execute("SELECT * FROM external_operations ORDER BY 1")]
        return tuple(events), ops
    finally:
        db.close()


def test_plain_route_keeps_desktop_control_fields(tmp_path):
    h = setup_route(tmp_path)
    state = h.store.get(h.journey.actor, h.route["id"])
    assert state["control_surface"] == "desktop_control_only"
    assert state["go_no_go"] == "no_go_live_activation_blocked"
    assert state["proxy_safety"]["transport"] == "disabled"
    assert "live_path" not in state and "builtin_proxy" not in state


def test_campaign_route_reports_live_path_and_changes_nothing(tmp_path):
    h = setup_route(tmp_path)
    db = _connect(h.store.database_path)
    try:
        db.execute("PRAGMA foreign_keys = OFF")
        seed(db, route_id=h.route["id"])
    finally:
        db.close()
    before = _snapshot(h)
    state = h.store.get(h.journey.actor, h.route["id"])
    assert _snapshot(h) == before
    assert state["control_surface"] == "campaign_executor_guardian"
    # Kept for existing readers (the desktop renderer shows go_no_go); both still truthful.
    assert state["go_no_go"] == "no_go_live_activation_blocked"
    assert state["proxy_safety"]["transport"] == "disabled"
    proxy = state["builtin_proxy"]
    assert proxy["proxy_safety"]["transport"] == "disabled"
    assert proxy["go_no_go"] == "no_go_live_activation_blocked"
    assert state["live_path"]["dispatcher"] == "salad_executor_guardian"
    assert state["live_path"]["authorizes_nothing"] is True
    text = json.dumps(state)
    assert WORKER_PRINCIPAL not in text and GRANT not in text


def _store_refused_states():
    """Lifecycle states workers.require_ready_worker refuses (everything but ready)."""
    sql = db_schema_sql()
    start = sql.index("lifecycle_state TEXT NOT NULL CHECK (lifecycle_state IN")
    chunk = sql[start : sql.index(")", start + 60)]
    states = [p.strip(" '\n") for p in chunk.split("(")[-1].split(",")]
    return [s for s in states if s and s != "ready"]


def db_schema_sql():
    import inspect

    from tessera_control import workers

    return inspect.getsource(workers.create_worker_schema)


@pytest.mark.parametrize("state", [s for s in _store_refused_states() if s != "retired"])
def test_non_ready_lifecycle_with_fresh_heartbeat_is_not_ready(db, state):
    seed(db, lifecycle=state)
    v = view(db)
    assert v["executor"]["freshness"] == "fresh"
    assert v["readiness"] == "not_ready"
    assert v["reasons"] == [f"executor_not_ready:{state}"]


def test_refused_state_set_is_what_the_store_refuses():
    assert set(_store_refused_states()) == {"draining", "disconnected", "quarantined", "retired"}


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"credential_revoked": _iso(-5)}, "executor_credential_revoked"),
        ({"credential_expires": _iso(-5)}, "executor_credential_expired"),
        ({"credential_expires": "not-a-time"}, "executor_credential_expiry_unknown"),
        ({"registry_project": "other_project"}, "executor_project_mismatch"),
    ],
)
def test_credential_and_project_refusals_are_not_ready(db, kwargs, reason):
    seed(db, **kwargs)
    v = view(db)
    assert v["readiness"] == "not_ready" and v["reasons"] == [reason]


def test_ready_worker_with_active_credential_unchanged(db):
    seed(db)
    v = view(db)
    assert v["readiness"] == "ready" and v["reasons"] == []
