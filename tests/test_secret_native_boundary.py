"""Native ABI/helper tests with fake OS functions, never the host's Keychain."""

import ctypes
import json
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest
from test_admin_configuration import command, post
from test_protected_secrets import MARKER, approved, enroll, request, resolver, service
from test_protected_secrets import f as protected_fixture

from tessera_control import secret_enrollment_cli as cli
from tessera_control.os_secret_store import DevelopmentMemoryStore, MacOSKeychain, SecretStoreError
from tessera_control.protected_secrets import ProtectedSecretStore
from tessera_control.recovery import backup_state, restore_state
from tessera_control.store import Conflict, Forbidden

f = protected_fixture


class FakeSecurity:
    def __init__(self):
        self.items = {}
        self.selected = None
        self.buffer = None
        self.denied = False

    def SecKeychainAddGenericPassword(self, keychain, slen, service, alen, name, size, value, item):
        assert keychain is None and service == MacOSKeychain.service and slen == len(service)
        assert alen == len(name)
        if self.denied:
            return -25293
        if name in self.items:
            return -25299
        self.items[name] = bytearray(value[:size])
        return 0

    def SecKeychainFindGenericPassword(self, kc, slen, service, alen, name, size, data, item):
        if self.denied:
            return -25293
        if name not in self.items:
            return -25300
        self.selected = name
        item._obj.value = 1
        if data is not None:
            value = self.items[name]
            self.buffer = ctypes.create_string_buffer(bytes(value))
            size._obj.value = len(value)
            data._obj.value = ctypes.addressof(self.buffer)
        return 0

    def SecKeychainItemFreeContent(self, attrs, data):
        assert not any(self.buffer.raw[:-1])
        return 0

    def SecKeychainItemDelete(self, item):
        del self.items[self.selected]
        return 0


def test_keychain_native_abi_exact_reference_version_add_no_overwrite_and_no_get_for_health():
    backend = MacOSKeychain.__new__(MacOSKeychain)  # No framework load or host-store access.
    backend.lib = FakeSecurity()
    backend.cf = SimpleNamespace(CFRelease=lambda item: None)
    ref = "credential-ref:" + "a" * 32
    assert not backend.exists(ref, 1)
    backend.put(ref, 1, bytearray(MARKER))
    assert backend.exists(ref, 1) and backend.lib.buffer is None
    with pytest.raises(SecretStoreError):
        backend.put(ref, 1, bytearray(MARKER + b"duplicate"))
    with backend.resolve(ref, 1) as value:
        assert value.contains_in(MARKER)
    assert not value.contains_in(MARKER)
    backend.lib.denied = True
    for operation in (backend.exists, backend.resolve, backend.delete):
        with pytest.raises(SecretStoreError):
            operation(ref, 1)
    with pytest.raises(SecretStoreError):
        backend.put(ref, 2, bytearray(MARKER))
    backend.lib.denied = False
    backend.delete(ref, 1)
    backend.delete(ref, 1)
    assert not backend.exists(ref, 1)


@pytest.mark.parametrize("confirmation", [b'{"commit":true}', b'{"commit":false}'])
def test_native_helper_protocol_only_metadata_and_wipes_prompt_buffer(
    f, monkeypatch, capsys, confirmation
):
    record = approved(f)
    req = request(record)
    reads = iter(
        [
            bytearray(
                json.dumps({"token": f["owner"]["Authorization"][7:], "request": req}).encode()
            ),
            bytearray(confirmation),
        ]
    )
    monkeypatch.setattr(cli, "bounded_read", lambda *a, **kw: next(reads))
    backend = DevelopmentMemoryStore()
    backend.status = "macos_keychain"  # Explicit stub of the native protocol, not real OS evidence.
    monkeypatch.setattr(cli, "platform_store", lambda: backend)
    value = bytearray(MARKER)
    monkeypatch.setattr(cli, "native_prompt", lambda state: value)
    if confirmation == b'{"commit":true}':
        cli.run(f["state"])
        assert backend.exists(record["reference"], 1)
    else:
        with pytest.raises(SecretStoreError):
            cli.run(f["state"])
        assert not backend._items
    assert not any(value)
    output = capsys.readouterr()
    assert MARKER.decode() not in output.out + output.err
    assert json.loads(output.out.splitlines()[0]) == {"phase": "confirm"}


def test_helper_main_discards_raw_error_text_and_validation_inputs(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["helper", "--state-dir", "/fixture/unused"])
    monkeypatch.setattr(cli.signal, "alarm", lambda seconds: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *args: None)
    # Do not alter the pytest process's financial/credential/OS resource configuration.
    import resource

    monkeypatch.setattr(resource, "setrlimit", lambda *args: None)

    def fail(*args):
        raise RuntimeError(MARKER.decode())

    monkeypatch.setattr(cli, "run", fail)
    assert cli.main() == 2
    output = capsys.readouterr()
    assert MARKER.decode() not in output.out + output.err and "Traceback" not in output.err


def test_actual_process_death_leaves_consumed_claim_no_replay(f):
    record = approved(f)
    req = request(record)
    script = (
        "import json,os,sys; from pathlib import Path; "
        "from tessera_control.protected_secrets import ProtectedSecretStore; "
        "payload=json.load(sys.stdin); store=ProtectedSecretStore(Path(sys.argv[1])); "
        "actor=store.authenticate(payload['token']); "
        "store.claim(actor,payload['request']); os._exit(23)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(f["state"])],
        input=json.dumps({"token": f["owner"]["Authorization"][7:], "request": req}),
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 23 and not result.stdout and not result.stderr
    store, actor = service(f)
    assert store.inspect(actor, f["project"], record["id"])["state"] == "enrolling"
    with pytest.raises(Conflict):
        store.claim(actor, req)


def test_missing_use_authority_and_store_denial_never_release_values(f):
    store, actor, backend, record, _ = enroll(f)

    def deny(db):
        raise Forbidden("Missing admitted execution lease.")

    target = resolver(store, actor, backend, record)
    target.admission = deny
    with pytest.raises(Forbidden):
        target.resolve(record["reference"], 1)
    backend.delete(record["reference"], 1)
    with pytest.raises(SecretStoreError):
        resolver(store, actor, backend, record).resolve(record["reference"], 1)
    assert not store.health(actor, request(record, "health"), backend)["available"]


def test_database_completion_failure_compensates_without_undoing_claim(f, monkeypatch):
    store, actor = service(f)
    record = approved(f)
    req = request(record)
    store.claim(actor, req)
    backend = DevelopmentMemoryStore()
    save = store._save

    def fail(db, live, state, action):
        if action == "enrollment.stored":
            raise RuntimeError("injected metadata commit failure")
        return save(db, live, state, action)

    monkeypatch.setattr(store, "_save", fail)
    with pytest.raises(RuntimeError):
        store.finish(actor, req, bytearray(MARKER), backend)
    assert not backend._items
    assert store.inspect(actor, f["project"], record["id"])["state"] == "enrolling"
    with pytest.raises(Conflict):
        store.finish(actor, req, bytearray(MARKER), backend)


def test_delete_denial_remains_pending_and_proposer_revocation_fences_entry(f):
    store, actor, backend, record, _ = enroll(f)
    record = command(f, record, "revoke")

    def deny(*args):
        raise SecretStoreError()

    backend.delete = deny
    with pytest.raises(SecretStoreError):
        store.purge(actor, request(record, "purge"), backend)
    assert store.inspect(actor, f["project"], record["id"])["cleanup_pending"]
    req = request(approved(f))
    store.claim(actor, req)
    assert post(f, f"/credentials/{f['human_session_credential']}/revoke", None).status_code == 200
    value = bytearray(MARKER)
    with pytest.raises(Forbidden):
        store.finish(actor, req, value, DevelopmentMemoryStore())
    assert not any(value)


def test_backup_retains_only_metadata_and_held_restore_cannot_resolve_or_purge(f, tmp_path):
    store, actor, backend, record, _ = enroll(f)
    backup, restored = tmp_path / "metadata-backup", tmp_path / "held"
    backup_state(f["state"], backup)
    restore_state(backup, restored)
    held = ProtectedSecretStore(restored)
    token = (restored / "owner.header").read_text().strip().split("Bearer ")[1]
    fresh_owner = held.authenticate(token)
    assert MARKER not in held.database_path.read_bytes()
    with pytest.raises(Forbidden):
        resolver(held, fresh_owner, backend, record).resolve(record["reference"], 1)
    with pytest.raises(Forbidden):
        held.purge(fresh_owner, request(record, "purge"), backend)
    # No effect on the original host store follows from restoration.
    assert backend.exists(record["reference"], 1)


@pytest.mark.parametrize("tombstone_first", [False, True])
def test_natural_expiry_purge_confirmation_and_tombstone_preserve_truth(f, tombstone_first):
    store, actor, backend, record, _ = enroll(f)
    with sqlite3.connect(store.database_path) as db:
        db.execute(
            "UPDATE operator_audit_events SET payload_json="
            "json_set(payload_json,'$.spec.expires_at','2000-01-01T00:00:00Z') "
            "WHERE event_type='admin.state'"
        )
    record = store.inspect(actor, f["project"], record["id"])
    if tombstone_first:
        record = command(f, record, "delete")
        assert record["cleanup_pending"]
    result = store.purge(actor, request(record, "purge"), backend)
    assert result["state"] == ("deleted" if tombstone_first else "expired")
    assert not result["cleanup_pending"] and not backend._items
