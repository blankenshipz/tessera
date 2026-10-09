"""Authenticated local HTTP boundaries; no provider identity or native effect proof."""

# ruff: noqa: F811

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_agent_authority import prepared  # noqa: F401
from test_runtime_bindings import (  # noqa: F401
    allocate,
    claim_and_native_return,
    claim_body,
    fresh_ws_frame,
    post,
    runtime,
)

from tessera_control.api import create_app
from tessera_control.store import Store, _connect


@pytest.mark.parametrize("runtime", [2], indirect=True)
@pytest.mark.parametrize("change", ["same-root", "same-request-new-sequence", "old-sequence"])
def test_returned_root_cannot_be_rekeyed_or_retried(runtime, change):
    r = runtime
    first, receipt = claim_and_native_return(r, r["allocation"], [])
    if change == "same-root":
        allocation = r["allocation"]
    elif change == "same-request-new-sequence":
        allocation = fresh_ws_frame(r, r["allocation"]["request_ref"])
    else:
        allocation = fresh_ws_frame(r, sequence=1)
    post(
        r["client"],
        r["publisher"],
        "/runtime-operations",
        allocation,
        key="different-retry-key",
        expected=409,
    )
    post(
        r["client"],
        r["publisher"],
        f"/runtime-operations/{first['operation_ref']}/claim",
        claim_body(r, first),
        key="new-old-claim-key",
        expected=409,
    )
    assert receipt["remaining_native_operations"] == 1


@pytest.mark.parametrize("runtime", [2], indirect=True)
@pytest.mark.parametrize(
    "fence", ["policy", "financial", "enrollment", "epoch", "context", "expiry"]
)
def test_returned_fresh_root_still_rechecks_current_authority(runtime, fence, monkeypatch):
    import sqlite3

    import tessera_control.runtime_bindings as bindings

    r = runtime
    claim_and_native_return(r, r["allocation"], [])
    if fence in {"policy", "financial", "enrollment"}:
        kind = {
            "policy": "runtime-policies",
            "financial": "runtime-financial-assertions",
            "enrollment": "runtime-enrollments",
        }[fence]
        ref = r[fence][
            {
                "policy": "policy_ref",
                "financial": "attestation_ref",
                "enrollment": "enrollment_ref",
            }[fence]
        ]
        post(
            r["client"], r["owner"], f"/{kind}/{ref}/revoke", {"expected_revision": 1}, expected=200
        )
    elif fence == "expiry":
        future = datetime.now(UTC) + timedelta(minutes=6)

        class ExpiredClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return future

        monkeypatch.setattr(bindings, "datetime", ExpiredClock)
    else:
        c = _connect(Store(r["state"]).database_path)
        try:
            if fence == "epoch":
                c.execute(
                    "UPDATE agent_authority_grants SET epoch=epoch+1 WHERE id=?",
                    (r["parent"]["id"],),
                )
            else:
                with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                    c.execute(
                        "UPDATE external_agent_contexts SET payload_json='{}' WHERE id=?",
                        (r["enrollment_body"]["context_ref"],),
                    )
            c.commit()
        finally:
            c.close()
    if fence == "context":
        # Do not drop immutable-context triggers to manufacture an upstream change.
        post(
            r["client"],
            r["owner"],
            f"/projects/{r['project']}/runtime-enrollments",
            {**r["enrollment_body"], "context_digest": "0" * 64},
            expected=403,
        )
        fresh = post(r["client"], r["publisher"], "/runtime-operations", fresh_ws_frame(r))
        assert (
            fresh["binding"]["scope"]["context_digest"]
            == r["enrollment"]["scope"]["context_digest"]
        )
        return
    post(r["client"], r["publisher"], "/runtime-operations", fresh_ws_frame(r), expected=403)


def test_schema54_to55_preserves_history_rollback_repeat_and_readiness(tmp_path):
    import sqlite3

    from tessera_control.migrations import migrate_v54_to_v55
    from tessera_control.store import initialize_state, migrate_state

    state = tmp_path / "control"
    initialize_state(state)
    database = state / "tessera.sqlite3"
    tables = (
        "runtime_native_entries",
        "runtime_native_settlements",
        "runtime_native_claims",
        "runtime_native_root_fences",
        "runtime_native_operations",
        "runtime_authority_grants",
        "runtime_binding_revocations",
        "runtime_financial_assertions",
        "runtime_enrollments",
        "runtime_policy_profiles",
    )
    with sqlite3.connect(database) as c:
        for table in tables:
            c.execute(f"DROP TABLE {table}")
        c.execute("UPDATE schema_meta SET schema_version=54")
        history = {
            t: c.execute(f"SELECT * FROM {t}").fetchall()
            for t in ("principals", "credentials", "events", "idempotency", "control_recovery")
        }
    assert TestClient(create_app(state)).get("/health/ready").status_code == 503
    with sqlite3.connect(database) as c:
        c.execute("BEGIN IMMEDIATE")
        migrate_v54_to_v55(c)
        c.rollback()
        assert c.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == 54
        assert not c.execute(
            "SELECT 1 FROM sqlite_master WHERE name='runtime_native_claims'"
        ).fetchone()
    assert migrate_state(state) == 55
    assert migrate_state(state) == 55
    with sqlite3.connect(database) as c:
        assert all(
            c.execute(f"SELECT * FROM {t}").fetchall() == rows for t, rows in history.items()
        )
        assert all(c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0 for t in tables)
    assert TestClient(create_app(state)).get("/health/ready").status_code == 200


@pytest.mark.parametrize("kind", ["policy", "enrollment", "financial"])
def test_agent_cannot_issue(runtime, kind):
    r = runtime
    suffix = {
        "policy": "runtime-policies",
        "enrollment": "runtime-enrollments",
        "financial": "runtime-financial-assertions",
    }[kind]
    post(
        r["client"],
        r["agent"],
        f"/projects/{r['project']}/{suffix}",
        r[kind + "_body"],
        expected=403,
    )


def test_subject_can_read_not_allocate_or_claim_and_owner_cannot_proxy(runtime):
    r = runtime
    post(r["client"], r["agent"], "/runtime-bindings/resolve", r["resolve"], expected=200)
    post(r["client"], r["agent"], "/runtime-operations", r["allocation"], expected=403)
    post(r["client"], r["owner"], "/runtime-bindings/resolve", r["resolve"], expected=403)
    op = allocate(r)
    post(
        r["client"],
        r["agent"],
        f"/runtime-operations/{op['operation_ref']}/claim",
        claim_body(r, op),
        expected=403,
    )
    assert (
        r["client"]
        .post("/runtime-bindings/resolve", json=r["resolve"], headers={"Idempotency-Key": "unauth"})
        .status_code
        == 401
    )


def test_worker_and_control_audiences_are_distinct(runtime):
    r = runtime
    post(
        r["client"], r["publisher"], "/worker/runtime-bindings/resolve", r["resolve"], expected=401
    )
    post(
        r["client"],
        {"Authorization": "Bearer worker-token"},
        "/runtime-bindings/resolve",
        r["resolve"],
        expected=401,
    )


def test_missing_bridge_fails_closed_without_reservation(runtime):
    r = runtime
    client = TestClient(create_app(r["state"]))
    post(client, r["publisher"], "/runtime-operations", r["allocation"], expected=503)
    post(
        client,
        r["owner"],
        f"/projects/{r['project']}/runtime-enrollments",
        r["enrollment_body"],
        expected=503,
    )


def test_financial_original_age_and_closed_request_shape(runtime):
    r = runtime
    path = f"/projects/{r['project']}/runtime-financial-assertions"
    old = {
        **r["financial_body"],
        "observed_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    }
    post(r["client"], r["owner"], path, old, expected=403)
    post(r["client"], r["owner"], path, {**r["financial_body"], "guard": True}, expected=422)
    record = (
        r["client"]
        .get(
            f"/runtime-financial-assertions/{r['financial']['attestation_ref']}",
            headers=r["publisher"],
        )
        .json()
    )
    assert record["observed_at"] == r["financial"]["observed_at"]
    assert record["asserted_at"] == r["financial"]["asserted_at"]


def test_credential_and_parent_epoch_rechecked(runtime):
    r = runtime
    op = allocate(r)
    c = _connect(Store(r["state"]).database_path)
    try:
        c.execute(
            "UPDATE agent_authority_grants SET epoch=epoch+1 WHERE id=?", (r["parent"]["id"],)
        )
        c.commit()
    finally:
        c.close()
    post(
        r["client"],
        r["publisher"],
        f"/runtime-operations/{op['operation_ref']}/claim",
        claim_body(r, op),
        expected=403,
    )


def test_changed_idempotency_body_and_root_key_do_not_reset(runtime):
    r = runtime
    op = post(r["client"], r["publisher"], "/runtime-operations", r["allocation"], key="allocate")
    post(
        r["client"],
        r["publisher"],
        "/runtime-operations",
        {**r["allocation"], "sequence": 2},
        key="allocate",
        expected=409,
    )
    post(
        r["client"],
        r["publisher"],
        "/runtime-operations",
        r["allocation"],
        key="new-key",
        expected=409,
    )
    assert op["state"] == "allocated"


def test_wrong_project_and_subject_publisher_identity_are_rejected(runtime):
    r = runtime
    other = post(r["client"], r["owner"], "/projects", {"name": "other"})
    post(
        r["client"],
        r["owner"],
        f"/projects/{other['id']}/runtime-enrollments",
        r["enrollment_body"],
        expected=403,
    )
    post(
        r["client"],
        r["owner"],
        f"/projects/{r['project']}/runtime-enrollments",
        {
            **r["enrollment_body"],
            "publisher_credential_id": r["enrollment_body"]["subject_credential_id"],
        },
        expected=422,
    )


def test_revoked_subject_credential_denies_claim(runtime):
    r = runtime
    op = allocate(r)
    c = _connect(Store(r["state"]).database_path)
    try:
        c.execute(
            "UPDATE credentials SET revoked_at=? WHERE id=?",
            (datetime.now(UTC).isoformat(), r["enrollment_body"]["subject_credential_id"]),
        )
        c.commit()
    finally:
        c.close()
    post(
        r["client"],
        r["publisher"],
        f"/runtime-operations/{op['operation_ref']}/claim",
        claim_body(r, op),
        expected=403,
    )


def test_parent_budget_spent_after_allocation_denies_reservation(runtime):
    r = runtime
    op = allocate(r)
    c = _connect(Store(r["state"]).database_path)
    try:
        c.execute(
            "UPDATE agent_authority_grants SET reserved_cents=100 WHERE id=?", (r["parent"]["id"],)
        )
        c.commit()
    finally:
        c.close()
    post(
        r["client"],
        r["publisher"],
        f"/runtime-operations/{op['operation_ref']}/claim",
        claim_body(r, op),
        expected=403,
    )


def test_worker_publisher_uses_real_worker_authentication(runtime):
    r = runtime
    enrollment = post(
        r["client"],
        r["owner"],
        f"/projects/{r['project']}/runtime-enrollments",
        {
            **r["enrollment_body"],
            "publisher_credential_id": r["worker"]["credential_id"],
            "publisher_audience": "worker",
        },
    )
    financial = post(
        r["client"],
        r["owner"],
        f"/projects/{r['project']}/runtime-financial-assertions",
        {**r["financial_body"], "enrollment_ref": enrollment["enrollment_ref"]},
    )
    allocation = {
        **r["allocation"],
        "enrollment_ref": enrollment["enrollment_ref"],
        "financial_ref": financial["attestation_ref"],
    }
    headers = {"Authorization": "Bearer worker-token"}
    op = post(r["client"], headers, "/worker/runtime-operations", allocation)
    body = claim_body({**r, "allocation": allocation}, op)
    claim = post(
        r["client"], headers, f"/worker/runtime-operations/{op['operation_ref']}/claim", body
    )
    assert claim["actor_credential_id"] == r["worker"]["credential_id"]
    settled = post(
        r["client"],
        headers,
        f"/worker/runtime-operations/{op['operation_ref']}/settle",
        {
            "claim_ref": claim["claim_ref"],
            "expected_claim_revision": 2,
            "disposition": "native-returned",
        },
        expected=200,
    )
    assert settled["state"] == "native-returned"
    post(
        r["client"],
        headers,
        f"/worker/runtime-operations/{op['operation_ref']}/settle",
        {"claim_ref": claim["claim_ref"], "expected_claim_revision": 2, "disposition": "uncertain"},
        expected=409,
    )
