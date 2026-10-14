import hashlib
import sqlite3
from dataclasses import replace

import pytest

from tessera_control.salad_custody import CustodyBinding, SaladCustody


@pytest.fixture
def store(tmp_path):
    tmp_path.chmod(0o700)
    clock = [100.0]
    s = SaladCustody(tmp_path / "custody.db", clock=lambda: clock[0])
    b = CustodyBinding("tenant", "project", "operation", 1, "artifact")
    s.register(b, byte_cap=10, file_cap=2)
    return s, b, clock


def ticket(s, b, data=b"abc", name="result.txt"):
    return s.issue(
        b, name=name, checksum=hashlib.sha256(data).hexdigest(), size=len(data), expires=200
    )


def test_retained_restart_no_overwrite(store, tmp_path):
    s, b, clock = store
    t = ticket(s, b)
    s.upload(t, b, b"abc")
    s.db.close()
    s = SaladCustody(tmp_path / "custody.db", clock=lambda: clock[0])
    with pytest.raises(ValueError, match="unavailable"):
        s.upload(t, b, b"abc")
    m = s.seal(b)
    assert m["complete"] and m["quarantine"] and m["writers_closed"]
    assert s.read_retained(b, "result.txt") == b"abc"
    assert s.seal(b) == m
    with pytest.raises(ValueError, match="closed"):
        ticket(s, b, name="another")


@pytest.mark.parametrize(
    "field,value",
    [
        ("project", "other"),
        ("operation", "other"),
        ("epoch", 2),
        ("artifact", "other"),
        ("kind", "checkpoint"),
        ("tenant", "other"),
    ],
)
def test_exact_ticket_binding(store, field, value):
    s, b, _ = store
    t = ticket(s, b)
    with pytest.raises(ValueError, match="unavailable"):
        s.upload(t, replace(b, **{field: value}), b"abc")
    s.upload(t, b, b"abc")


@pytest.mark.parametrize(
    "name", ["../escape", "/absolute", "a//b", "a/./b", "a/../b", "a\\b", "x\x00"]
)
def test_paths_are_logical_only(store, name):
    s, b, _ = store
    with pytest.raises(ValueError, match="scope"):
        ticket(s, b, name=name)


def test_caps_checksum_expiry_and_quiescence(store):
    s, b, clock = store
    t = ticket(s, b)
    with pytest.raises(ValueError, match="integrity"):
        s.upload(t, b, b"xyz")
    ticket(s, b, b"1234567", "checkpoint")
    with pytest.raises(ValueError, match="cap"):
        ticket(s, b, b"", "third")
    clock[0] = 200
    with pytest.raises(ValueError, match="unavailable"):
        s.upload(t, b, b"abc")
    m = s.seal(b)
    assert not m["complete"] and m["files"] == []
    clock[0] = 100
    with pytest.raises(ValueError, match="unavailable"):
        s.upload(t, b, b"abc")


def test_closure_survives_failed_inventory_verification(store):
    s, b, _ = store
    s.upload(ticket(s, b), b, b"abc")
    with s.db:
        s.db.execute("UPDATE files SET data=?", (b"bad",))
    with pytest.raises(ValueError, match="integrity"):
        s.seal(b)
    with pytest.raises(ValueError, match="closed"):
        ticket(s, b, name="new")


def test_symlink_private_database_defenses(tmp_path):
    tmp_path.chmod(0o700)
    target = tmp_path / "target"
    target.write_bytes(b"")
    link = tmp_path / "linked"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="unsafe"):
        SaladCustody(link)
    tmp_path.chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        SaladCustody(tmp_path / "new")


def test_upload_transaction_rolls_back_after_crash(store):
    s, b, _ = store
    t = ticket(s, b)
    # A failed durable byte transaction consumes neither inventory nor ticket.
    s.db.execute(
        "CREATE TRIGGER fail_insert BEFORE INSERT ON files BEGIN SELECT RAISE(ABORT,'crash'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        s.upload(t, b, b"abc")
    s.db.execute("DROP TRIGGER fail_insert")
    s.upload(t, b, b"abc")
    assert s.seal(b)["complete"]


def test_operation_sealing_closes_checkpoints_and_future_inventories(store):
    s, b, _ = store
    checkpoint = replace(b, artifact="checkpoint", kind="checkpoint")
    s.register(checkpoint, byte_cap=10, file_cap=1)
    t = ticket(s, checkpoint)
    manifest = s.seal_operation(b)
    assert len(manifest["inventories"]) == 2 and not manifest["complete"]
    with pytest.raises(ValueError, match="unavailable"):
        s.upload(t, checkpoint, b"abc")
    with pytest.raises(ValueError, match="closed"):
        s.register(replace(b, artifact="late"), byte_cap=1, file_cap=1)
