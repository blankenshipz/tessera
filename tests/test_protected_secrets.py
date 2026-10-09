"""Synthetic custody only: never instantiate/access the user's Keychain or providers."""

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_admin_configuration import command, create, future, post, secret, workspace
from test_admin_configuration import f as admin_fixture

from tessera_control.api import create_app
from tessera_control.deployment import DeploymentConfig
from tessera_control.os_secret_store import (
    DevelopmentMemoryStore,
    LinuxSecretService,
    MacOSKeychain,
    SecretStoreError,
    UnavailableOSStore,
    WindowsCredentialManager,
    platform_store,
)
from tessera_control.protected_secrets import ProtectedSecretStore, TrustedServerResolver
from tessera_control.secret_enrollment_cli import bounded_read, private_channel
from tessera_control.store import Conflict, Forbidden, NotFound, StoreError

MARKER = b"synthetic-protected-only-NEVER-A-REAL-KEY"


@pytest.fixture
def f(tmp_path):
    return admin_fixture.__wrapped__(tmp_path)


def proposal(f):
    return create(f, secret(backend="os_keychain"), "human_session")


def approved(f):
    return command(f, proposal(f), "approve")


def request(record, operation="enroll"):
    return dict(
        project_id=record["project_id"],
        record_id=record["id"],
        expected_revision=record["revision"],
        enrollment_id=record["enrollment_id"],
        operation=operation,
    )


def service(f):
    store = ProtectedSecretStore(f["state"])
    return store, store.authenticate(f["owner"]["Authorization"][7:])


def enroll(f, backend=None):
    backend = backend or DevelopmentMemoryStore()
    store, actor = service(f)
    req = request(approved(f))
    store.claim(actor, req)
    value = bytearray(MARKER)
    result = store.finish(actor, req, value, backend)
    assert not any(value)
    return store, actor, backend, result, req


def resolver(store, actor, backend, record, **extra):
    return TrustedServerResolver(
        store,
        backend,
        admission=lambda db: actor,
        project_id=record["project_id"],
        record_id=record["id"],
        purpose=extra.get("purpose", "salad_management"),
        audience=extra.get("audience", "server_proxy"),
    )


def test_enrollment_metadata_only_every_serialized_boundary(f, caplog):
    store, actor, backend, record, req = enroll(f)
    assert record["custody"] == "development_memory_NON_PRODUCTION"
    assert record["state"] == "active" and record["version"] == 1
    response = f["client"].get(
        f"/admin/projects/{f['project']}/records/{record['id']}/enrollment", headers=f["owner"]
    )
    assert response.status_code == 200
    audit = f["client"].get(f"/admin/projects/{f['project']}/audit", headers=f["owner"]).json()
    with sqlite3.connect(store.database_path) as db:
        serialized = "\n".join(db.iterdump())
    assert (
        MARKER.decode()
        not in serialized
        + json.dumps(workspace(f))
        + json.dumps(audit)
        + response.text
        + caplog.text
    )
    assert len(record["versions"][0]["fingerprint"]) == 64
    assert not workspace(f)["provider_enabled"]
    with resolver(store, actor, backend, record).resolve(record["reference"], 1) as value:
        assert value.contains_in(MARKER)
        assert MARKER.decode() not in repr(value)
        with pytest.raises(TypeError):
            json.dumps(value)
    assert not value.contains_in(MARKER)
    assert store.health(actor, request(record, "health"), backend)["available"]
    for action in ("claim", "finish"):
        with pytest.raises(Conflict):
            if action == "claim":
                store.claim(actor, req)
            else:
                store.finish(actor, req, bytearray(MARKER), backend)


@pytest.mark.parametrize(
    "value",
    [b"", b"short", b"x" * 16385, b"x" * 16 + b"\xff", b"x" * 16 + b"\n", b"x" * 16 + b"\0"],
)
def test_malformed_values_wiped_without_persistence(f, value):
    store, actor = service(f)
    req = request(approved(f))
    store.claim(actor, req)
    owned = bytearray(value)
    backend = DevelopmentMemoryStore()
    with pytest.raises(SecretStoreError):
        store.finish(actor, req, owned, backend)
    assert not any(owned) and not backend._items
    assert store.inspect(actor, req["project_id"], req["record_id"])["state"] == "enrolling"


def test_metadata_injection_wipes_input_and_does_not_echo(f, caplog):
    store, actor = service(f)
    owned = bytearray(MARKER)
    with pytest.raises(StoreError) as error:
        store.finish(actor, {"value": MARKER.decode()}, owned, DevelopmentMemoryStore())
    assert not any(owned) and MARKER.decode() not in str(error.value) + caplog.text
    response = post(
        f,
        f"/admin/projects/{f['project']}/records",
        {"spec": secret(backend="os_keychain", value=MARKER.decode())},
    )
    assert response.status_code == 422 and MARKER.decode() not in response.text


def test_independent_human_scope_and_no_secret_delegation(f):
    own = create(f, secret(backend="os_keychain"))
    command(f, own, "approve", status=403)
    create(f, secret(backend="os_keychain"), "agent", status=403)
    record = approved(f)
    store, actor = service(f)
    req = request(record)
    agent = store.authenticate(f["agent"]["Authorization"][7:])
    with pytest.raises(Forbidden):
        store.claim(agent, req)
    with pytest.raises(Forbidden):
        store.claim({**actor, "org_id": "other"}, req)
    other = post(f, "/projects", {"name": "other"}).json()["id"]
    with pytest.raises(NotFound):
        store.claim(actor, req | {"project_id": other})
    with pytest.raises(Conflict):
        store.claim(actor, req | {"enrollment_id": "0" * 32})
    create(f, secret(backend="os_keychain", audience="gateway"), "human_session", status=422)
    create(f, secret(backend="os_keychain", provider="ai_gateway"), "human_session", status=422)


def test_concurrent_claim_and_versioning(f):
    store, actor = service(f)
    req = request(approved(f))

    def claim():
        try:
            return store.claim(actor, req)["state"]
        except Conflict:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: claim(), range(2))) == ["blocked", "enrolling"]
    backend = DevelopmentMemoryStore()
    record = store.finish(actor, req, bytearray(MARKER), backend)
    human = store.authenticate(f["human_session"]["Authorization"][7:])
    body = {"action": "rotate", "expected_revision": record["revision"], "expires_at": future()}

    def rotate(key):
        try:
            return store.command(human, f["project"], record["id"], key, body)[1]["version"]
        except Conflict:
            return 0

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(rotate, ["rotate1", "rotate2"])) == [0, 2]
    current = store.inspect(actor, f["project"], record["id"])
    assert current["state"] == "proposed"
    with pytest.raises(Forbidden):
        resolver(store, actor, backend, record).resolve(record["reference"], 1)
    current = command(f, current, "approve")
    req2 = request(current, "import")
    store.claim(actor, req2)
    current = store.finish(actor, req2, bytearray(MARKER + b"-rotated"), backend)
    assert current["version"] == 2 and current["cleanup_pending"]
    current = store.purge(actor, request(current, "purge"), backend)
    assert not current["cleanup_pending"]
    assert not backend.exists(current["reference"], 1) and backend.exists(current["reference"], 2)


@pytest.mark.parametrize("terminal", ["revoke", "expire"])
def test_revoke_expire_delete_and_wrong_resolver_purpose(f, terminal):
    store, actor, backend, record, _ = enroll(f)
    for extra in ({"purpose": "ai_gateway"}, {"audience": "gateway"}):
        with pytest.raises(Forbidden):
            resolver(store, actor, backend, record, **extra).resolve(record["reference"], 1)
    with pytest.raises(Forbidden):
        resolver(store, actor, backend, record).resolve(record["reference"], 2)
    record = command(f, record, terminal)
    assert not store.health(actor, request(record, "health"), backend)["available"]
    with pytest.raises(Forbidden):
        resolver(store, actor, backend, record).resolve(record["reference"], 1)
    record = command(f, record, "delete")
    assert record["cleanup_pending"]
    record = store.purge(actor, request(record, "purge"), backend)
    assert record["state"] == "deleted" and not record["cleanup_pending"] and not backend._items
    with pytest.raises(Conflict):
        store.claim(actor, request(record))


@pytest.mark.parametrize(
    "backend", [UnavailableOSStore, WindowsCredentialManager, LinuxSecretService]
)
def test_unavailable_store_never_downgrades(f, backend):
    store, actor = service(f)
    req = request(approved(f))
    store.claim(actor, req)
    value = bytearray(MARKER)
    with pytest.raises(SecretStoreError):
        store.finish(actor, req, value, backend())
    assert not any(value)
    assert store.inspect(actor, f["project"], req["record_id"])["cleanup_pending"]


def test_partial_store_permission_denial_compensates_and_crash_is_not_success(f):
    class Partial(DevelopmentMemoryStore):
        def put(self, *args):
            super().put(*args)
            raise SecretStoreError()

    store, actor = service(f)
    req = request(approved(f))
    claim = store.claim(actor, req)
    backend = Partial()
    with pytest.raises(SecretStoreError):
        store.finish(actor, req, bytearray(MARKER), backend)
    assert not backend._items
    # Simulate death after OS write, before SQLite completion: durable claim survives.
    backend = DevelopmentMemoryStore()
    backend.put(claim["reference"], 1, bytearray(MARKER))
    restarted = ProtectedSecretStore(f["state"])
    with pytest.raises(Conflict):
        restarted.claim(actor, req)
    with pytest.raises(Forbidden):
        resolver(restarted, actor, backend, claim).resolve(claim["reference"], 1)
    claim = restarted.inspect(actor, f["project"], claim["id"])
    claim = command(f, claim, "revoke")
    restarted.purge(actor, request(claim, "purge"), backend)
    assert not backend._items


def test_stale_approval_expiry_auth_and_remote_topology(f):
    store, actor = service(f)
    record = approved(f)
    req = request(record)
    store.claim(actor, req)
    # Revocation while the native prompt is open fences the final write.
    current = store.inspect(actor, f["project"], record["id"])
    command(f, current, "revoke")
    with pytest.raises(Conflict):
        store.finish(actor, req, bytearray(MARKER), DevelopmentMemoryStore())
    remote = TestClient(
        create_app(f["state"], DeploymentConfig.remote(["testserver"], "https://testserver"))
    )
    response = remote.get(
        f"/admin/projects/{f['project']}/records/{record['id']}/enrollment", headers=f["owner"]
    )
    assert response.status_code == 403
    # Token bytes do not become a provider credential by changing the declared purpose.
    req = request(approved(f))
    store.claim(actor, req)
    with pytest.raises(Forbidden):
        store.finish(
            actor,
            req,
            bytearray(f["owner"]["Authorization"][7:].encode()),
            DevelopmentMemoryStore(),
        )


def test_natural_expiry_and_expired_enrollment_deadline(f):
    store, actor, backend, record, _ = enroll(f)
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(store.database_path) as db:
        db.execute(
            "UPDATE operator_audit_events SET payload_json="
            "json_set(payload_json,'$.spec.expires_at',?) WHERE event_type='admin.state'",
            (past,),
        )
    expired = store.inspect(actor, f["project"], record["id"])
    assert expired["state"] == "expired" and expired["cleanup_pending"]
    with pytest.raises(Forbidden):
        resolver(store, actor, backend, record).resolve(record["reference"], 1)
    req = request(approved(f))
    with sqlite3.connect(store.database_path) as db:
        db.execute(
            "UPDATE operator_audit_events SET payload_json="
            "json_set(payload_json,'$.enrollment_deadline',?) WHERE event_type='admin.state'",
            (past,),
        )
    with pytest.raises(Conflict):
        store.claim(actor, req)


def test_platform_detection_never_selects_memory_or_files(monkeypatch):
    import tessera_control.os_secret_store as module

    for platform, expected in [("win32", WindowsCredentialManager), ("linux", LinuxSecretService)]:
        monkeypatch.setattr(module.sys, "platform", platform)
        assert type(platform_store()) is expected
        with pytest.raises(SecretStoreError):
            MacOSKeychain()


def test_private_one_shot_channel_limits_permissions_and_timeout(tmp_path):
    path = tmp_path / "synthetic-input"
    path.write_bytes(MARKER)
    with path.open("rb") as handle, pytest.raises(SecretStoreError):
        private_channel(handle.fileno())  # Even a private file is not an inherited pipe.
    r, w = os.pipe()
    try:
        os.write(w, MARKER)
        os.close(w)
        w = None
        assert bounded_read(r, len(MARKER)) == bytearray(MARKER)
    finally:
        os.close(r)
        if w is not None:
            os.close(w)
    r, w = os.pipe()
    try:
        with pytest.raises(SecretStoreError):
            bounded_read(r, 16, seconds=0.001)
        os.write(w, b"x" * 17)
        with pytest.raises(SecretStoreError):
            bounded_read(r, 16)
    finally:
        os.close(r)
        os.close(w)


@pytest.mark.parametrize("prefix", ["salad_", "sld_", "ai_gateway_", "sk-"])
def test_provider_keys_rejected_from_general_token_creation(f, prefix):
    value = prefix + "x" * 48
    response = post(
        f,
        "/credentials",
        {
            "kind": "human_session",
            "audience": "control",
            "project_id": f["project"],
            "expires_at": future(),
            "token": value,
        },
    )
    assert response.status_code == 422 and value not in response.text
