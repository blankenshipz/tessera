"""Isolated administration acceptance: zero secret imports or external effects."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tessera_control.admin import AdminStore
from tessera_control.admin_secrets import FakeAdminSecretBackend, UnavailableSecretBackend
from tessera_control.api import create_app
from tessera_control.recovery import backup_state, restore_state
from tessera_control.store import Forbidden, initialize_state


def future(minutes=30):
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()


@pytest.fixture
def f(tmp_path):
    state = tmp_path / "control"
    initialize_state(state)
    client = TestClient(create_app(state))
    name, token = (state / "owner.header").read_text().strip().split(": ", 1)
    owner = {name: token}
    project = client.post(
        "/projects", headers=owner | {"Idempotency-Key": "p"}, json={"name": "Admin"}
    ).json()["id"]
    result = {"state": state, "client": client, "owner": owner, "project": project, "n": 0}
    for kind in ("human_session", "agent", "worker"):
        token = kind + "_" + "x" * 48
        response = client.post(
            "/credentials",
            headers=owner | {"Idempotency-Key": kind},
            json={
                "kind": kind,
                "audience": "worker" if kind == "worker" else "control",
                "project_id": project,
                "expires_at": future(60),
                "token": token,
            },
        )
        assert response.status_code == 201, response.text
        result[kind] = {"Authorization": f"Bearer {token}"}
        result[kind + "_id"] = response.json()["principal_id"]
        result[kind + "_credential"] = response.json()["credential_id"]
    return result


def post(f, path, body, who="owner", key=None):
    f["n"] += 1
    return f["client"].post(
        path, headers=f[who] | {"Idempotency-Key": key or f"key-{f['n']}"}, json=body
    )


def create(f, spec, who="owner", key=None, status=201):
    response = post(f, f"/admin/projects/{f['project']}/records", {"spec": spec}, who, key)
    assert response.status_code == status, response.text
    return response.json()


def command(f, record, action, who="owner", key=None, status=200, **extra):
    response = post(
        f,
        f"/admin/projects/{f['project']}/records/{record['id']}/commands",
        {"expected_revision": record["revision"], "action": action, **extra},
        who,
        key,
    )
    assert response.status_code == status, response.text
    return response.json()


def workspace(f, who="owner"):
    response = f["client"].get(f"/admin/projects/{f['project']}/records", headers=f[who])
    assert response.status_code == 200, response.text
    return response.json()


def config(**extra):
    return {"kind": "configuration", "component": "execution_target", "provider": "salad", **extra}


def secret(**extra):
    return {
        "kind": "secret",
        "backend": "fake",
        "purpose": "salad_management",
        "provider": "salad",
        "audience": "server_proxy",
        "expires_at": future(),
        **extra,
    }


def grant(f, target, **extra):
    return {
        "kind": "delegation",
        "subject_id": f["agent_id"],
        "subject_role": "agent",
        "target_id": target["id"],
        "provider": "salad",
        "actions": ["configuration.read", "configuration.propose", "readiness.read"],
        "expires_at": future(15),
        **extra,
    }


def test_configure_review_verify_never_enables_or_dispatches(f):
    c = create(f, config(), "agent")
    assert c["verification"] == "unknown" and not c["enabled"]
    command(f, c, "verify", status=409)
    command(f, c, "approve", "agent", status=403)
    c = command(f, c, "approve")
    assert c["verification"] == "unknown"
    c = command(f, c, "verify")
    assert c["verification"] == "blocked" and not c["enabled"]
    command(f, c, "enable", status=403)
    w = workspace(f)
    assert w["control_status"] == "ready"
    assert not w["provider_enabled"]
    assert w["production_secret_backend"] == "unavailable"
    assert w["runtime_delegation"] == "unavailable"
    with sqlite3.connect(f["state"] / "tessera.sqlite3") as db:
        for table in ("execution_grants", "execution_leases", "external_operations"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_self_approval_and_subject_review_are_forbidden(f):
    c = create(f, config())
    command(f, c, "approve", status=403)
    # A local human session cannot become root admin by claiming a role.
    command(f, c, "approve", "human_session", status=403)
    d = create(f, grant(f, c))
    command(f, d, "approve", status=403)


def test_secret_lifecycle_versions_and_bound_configuration_staleness(f):
    s = create(f, secret())
    assert s["version"] == 1 and s["last_used_at"] is None
    assert s["health"] == "synthetic_only"
    c = create(f, config(management_secret_id=s["id"]), "agent")
    c = command(f, c, "approve")
    c = command(f, c, "verify")
    s = command(f, s, "rotate", expires_at=future(45))
    assert s["version"] == 2 and s["versions"][0]["state"] == "revoked"
    assert next(r for r in workspace(f)["records"] if r["id"] == c["id"])["verification"] == "stale"
    s = command(f, s, "revoke")
    command(f, c, "verify", status=409)
    command(f, s, "rotate", expires_at=future(), status=409)
    s = command(f, s, "delete")
    assert s["state"] == "deleted"
    assert not any(r["spec"]["kind"] == "secret" for r in workspace(f, "agent")["records"])


def test_expiry_is_enforced_without_background_job(f):
    s = create(f, secret())
    s = command(f, s, "expire")
    command(f, s, "rotate", expires_at=future(), status=409)
    create(f, config(management_secret_id=s["id"]), status=409)
    command(f, s, "delete")
    create(f, secret(expires_at=future(-1)), status=409)


def test_production_backend_unavailable_and_fake_has_no_import(f):
    create(f, secret(backend="production_unavailable"), status=403)
    assert workspace(f)["records"] == []
    with pytest.raises(Forbidden, match="unavailable"):
        UnavailableSecretBackend().materialize()
    buffer = FakeAdminSecretBackend().materialize()
    assert "synthetic-admin-marker" not in repr(buffer)
    with buffer:
        assert buffer.contains_in(b"synthetic-admin-marker-not-a-credential")
    assert not buffer.contains_in(b"synthetic-admin-marker-not-a-credential")


@pytest.mark.parametrize(
    "field", ["value", "plaintext", "token", "key", "secret_ref", "env", "path", "headers"]
)
def test_secret_values_are_rejected_and_not_retained(f, caplog, field):
    marker = "DO_NOT_LEAK_PRIVATE_SENTINEL"
    body = secret(**{field: marker})
    response = post(f, f"/admin/projects/{f['project']}/records", {"spec": body})
    assert response.status_code == 422
    assert marker not in response.text + caplog.text + json.dumps(workspace(f))
    with sqlite3.connect(f["state"] / "tessera.sqlite3") as db:
        assert marker not in "\n".join(db.iterdump())


@pytest.mark.parametrize(
    "change",
    [
        {"endpoint": "https://user:secret@host.test"},
        {"endpoint": "https://host.test/?token=private"},
        {"endpoint": "http://remote.test"},
        {"endpoint": "https://host.test/path"},
        {"provider_enabled": True},
        {"max_cpu": True},
        {"max_seconds": 0},
        {"management_secret_id": "raw-secret"},
        {"gateway_secret_id": "admin_" + "a" * 32},
    ],
)
def test_strict_configuration_rejects_transport_and_scope_bypass(f, change):
    create(f, config(**change), status=422)


def test_gateway_management_and_project_bindings_are_not_interchangeable(f):
    gateway = create(f, secret(purpose="ai_gateway", provider="ai_gateway", audience="gateway"))
    create(f, config(management_secret_id=gateway["id"]), status=409)
    create(f, secret(audience="gateway"), status=422)
    assert (
        f["client"].get(f"/admin/projects/{f['project']}/records", headers=f["worker"]).status_code
        == 401
    )
    create(f, secret(), "agent", status=403)


def test_delegated_scope_use_revocation_and_replay(f):
    target = create(f, config())
    other = create(f, config())
    d = create(f, grant(f, target), "agent")
    assert not d["effective"]
    command(f, target, "revise", "agent", status=403, configuration=config())
    d = command(f, d, "approve", key="review")
    assert d["effective"]
    assert command(f, {**d, "revision": 1}, "approve", key="review") == d
    target = command(f, target, "revise", "agent", configuration=config(max_cpu=2))
    command(f, other, "revise", "agent", status=403, configuration=config())
    command(f, target, "revise", "agent", status=403, configuration=config(provider="kubernetes"))
    command(f, target, "approve", "agent", status=403)
    target = command(f, target, "approve")
    command(f, target, "verify", "agent")
    d = command(f, d, "revoke")
    assert not d["effective"]
    command(f, {**d, "revision": 1}, "approve", key="review", status=409)
    command(f, target, "revise", "agent", status=403, configuration=config())


@pytest.mark.parametrize(
    "change,status",
    [
        ({"actions": ["secret.read"]}, 422),
        ({"actions": ["execution.start"]}, 422),
        ({"provider": "kubernetes"}, 403),
        ({"subject_role": "worker"}, 403),
        ({"subject_role": "proxy"}, 403),
        ({"subject_role": "monitor"}, 403),
        ({"subject_role": "human"}, 403),
        ({"expires_at": future(120)}, 403),
        ({"actions": ["configuration.read", "configuration.read"]}, 422),
    ],
)
def test_delegation_allowlists_and_lifetime(f, change, status):
    target = create(f, config())
    create(f, grant(f, target, **change), "agent", status=status)


def test_subject_credential_revocation_invalidates_delegation(f):
    target = create(f, config())
    d = create(f, grant(f, target), "agent")
    d = command(f, d, "approve")
    response = post(f, f"/credentials/{f['agent_credential']}/revoke", None)
    assert response.status_code == 200
    assert not next(r for r in workspace(f)["records"] if r["id"] == d["id"])["effective"]


def test_tenant_and_project_data_access(f):
    target = create(f, config())
    other = post(f, "/projects", {"name": "Other"}).json()["id"]
    response = post(f, f"/admin/projects/{other}/records", {"spec": config()}, "agent")
    assert response.status_code == 404
    response = post(
        f,
        f"/admin/projects/{other}/records/{target['id']}/commands",
        {"expected_revision": 1, "action": "revoke"},
    )
    assert response.status_code == 404
    store = AdminStore(f["state"])
    actor = store.authenticate(f["owner"]["Authorization"].removeprefix("Bearer "))
    with pytest.raises(Forbidden):
        store.workspace({**actor, "org_id": "another_org"}, f["project"])


def test_idempotency_revision_and_atomic_events(f):
    body = secret()
    s = create(f, body, key="create")
    assert create(f, body, key="create") == s
    create(f, secret(expires_at=future(40)), key="create", status=409)
    r = command(f, s, "rotate", expires_at=future(40))
    command(f, s, "revoke", status=409)
    create(f, body, key="create", status=409)
    audit = (
        f["client"]
        .get(f"/admin/projects/{f['project']}/audit", headers=f["owner"])
        .json()["events"]
    )
    assert len(audit) == 2
    assert all(a["correlation_id"] and a["actor_principal_id"] for a in audit)
    assert "expires_at" not in json.dumps(audit)
    with sqlite3.connect(f["state"] / "tessera.sqlite3") as db:
        assert (
            db.execute("SELECT COUNT(*) FROM events WHERE event_type='admin.changed'").fetchone()[0]
            == 2
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM operator_audit_events WHERE event_type='admin.state'"
            ).fetchone()[0]
            == 2
        )
    assert r["revision"] == 2


def test_held_restore_retains_metadata_without_reviving_authority(f, tmp_path):
    s = create(f, secret(), key="secret")
    target = create(f, config())
    d = create(f, grant(f, target), "agent")
    command(f, d, "approve")
    backup = tmp_path / "backup"
    restored = tmp_path / "restored"
    backup_state(f["state"], backup)
    restore_state(backup, restored)
    name, token = (restored / "owner.header").read_text().strip().split(": ", 1)
    f.update(client=TestClient(create_app(restored)), owner={name: token})
    w = workspace(f)
    assert w["held"] and w["control_status"] == "held"
    assert not any(r["effective"] for r in w["records"])
    command(f, s, "revoke", status=403)
    assert len(workspace(f)["records"]) == 3


def test_natural_secret_and_delegation_expiry_is_checked_at_read_and_use(f, monkeypatch):
    import tessera_control.admin as admin_module

    s = create(f, secret())
    target = create(f, config())
    d = create(f, grant(f, target), "agent")
    command(f, d, "approve")
    instant = datetime.now(UTC) + timedelta(minutes=35)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant

    monkeypatch.setattr(admin_module, "datetime", Clock)
    records = {r["id"]: r for r in workspace(f)["records"]}
    assert records[s["id"]]["state"] == "expired"
    assert records[s["id"]]["health"] == "expired"
    assert records[d["id"]]["state"] == "expired"
    assert not records[d["id"]]["effective"]
    command(f, target, "revise", "agent", configuration=config(), status=403)
    command(f, s, "rotate", expires_at=future(50), status=409)
    command(f, s, "delete")


def test_revoked_target_and_changed_provider_fence_delegations(f):
    target = create(f, config())
    d = create(f, grant(f, target), "agent")
    command(f, target, "revoke")
    command(f, d, "approve", status=409)
    target = create(f, config())
    d = create(f, grant(f, target), "agent")
    d = command(f, d, "approve")
    command(f, target, "revise", configuration=config(provider="kubernetes"))
    assert not next(r for r in workspace(f)["records"] if r["id"] == d["id"])["effective"]
    assert not any(r["id"] == target["id"] for r in workspace(f, "agent")["records"])


def test_verification_replay_cannot_hide_rotation(f):
    s = create(f, secret())
    target = create(f, config(management_secret_id=s["id"]), "agent")
    target = command(f, target, "approve")
    command(f, target, "verify", key="verify")
    command(f, s, "rotate", expires_at=future(40))
    command(f, target, "verify", key="verify", status=409)


def test_atomic_state_audit_event_and_idempotency_on_failure(f, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic transaction failure")

    monkeypatch.setattr(AdminStore, "_event", fail)
    with pytest.raises(RuntimeError, match="synthetic transaction failure"):
        create(f, config(), key="atomic")
    assert workspace(f)["records"] == []
    with sqlite3.connect(f["state"] / "tessera.sqlite3") as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM operator_audit_events WHERE event_type LIKE 'admin.%'"
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM idempotency WHERE idempotency_key='atomic'"
            ).fetchone()[0]
            == 0
        )


def test_http_cross_tenant_owner_cannot_read_or_review(f):
    from tessera_control.store import _token_hash, utc_now

    token = "isolated_" + "z" * 48
    with sqlite3.connect(f["state"] / "tessera.sqlite3") as db:
        db.execute("INSERT INTO organizations VALUES (?,?,?)", ("other_org", "Other", utc_now()))
        db.execute(
            "INSERT INTO principals VALUES (?,?,?,?,?,?)",
            ("other_owner", "other_org", "human", _token_hash(token), None, utc_now()),
        )
        db.execute(
            "INSERT INTO credentials VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "other_credential",
                "other_owner",
                "local_owner",
                "control",
                None,
                None,
                None,
                None,
                utc_now(),
                None,
            ),
        )
    f["other"] = {"Authorization": "Bearer " + token}
    target = create(f, config(), "agent")
    response = f["client"].get(f"/admin/projects/{f['project']}/records", headers=f["other"])
    assert response.status_code == 404
    command(f, target, "approve", "other", status=404)
    assert (
        f["client"]
        .get(
            f"/admin/projects/{f['project']}/records/{target['id']}/salad-setup", headers=f["other"]
        )
        .status_code
        == 404
    )
    create(f, grant(f, target, subject_id="other_owner", subject_role="human"), status=403)


@pytest.mark.parametrize(
    "component,provider",
    [
        ("control_plane", "none"),
        ("control_plane", "kubernetes"),
        ("execution_target", "kubernetes"),
        ("execution_target", "local_fixture"),
        ("artifact_store", "artifact_store"),
        ("monitor", "none"),
        ("model_gateway", "ai_gateway"),
    ],
)
def test_supported_component_metadata_is_never_runtime_configuration(f, component, provider):
    c = create(f, config(component=component, provider=provider), "human_session")
    c = command(f, c, "approve")
    c = command(f, c, "verify")
    assert c["verification"] == "blocked" and c["enabled"] is False
