"""The re-enrolment receipt file is written atomically and never overwrites an existing one."""

from __future__ import annotations

import json
import os

import pytest
from test_credential_reenroll import NEW, argv, enroll_expired, run, store  # noqa: F401

from tessera_control import credential_reenroll as cr

RECEIPT = {
    "schema": cr.RECEIPT_SCHEMA,
    "provider": "salad",
    "reference": "credential-ref:salad-test",
    "previous_version": 1,
    "version": 2,
    "expires_at": 1234,
    "validation": "ok",
    "health_before": "expired",
    "health_after": "ok",
    "at": "2026-01-01T00:00:00+00:00",
}
FINAL = "credential-reenroll-salad-credential-ref-salad-test-v2.json"
EXPECTED = (json.dumps(RECEIPT, indent=2, sort_keys=True) + "\n").encode()


def test_success_leaves_only_final_file(tmp_path):
    path = cr._write_receipt(tmp_path, RECEIPT)
    assert path == tmp_path / FINAL
    assert os.listdir(tmp_path) == [FINAL]
    assert path.read_bytes() == EXPECTED
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("failure", ["fsync", "write"])
def test_partial_failure_leaves_nothing(tmp_path, monkeypatch, failure):
    if failure == "fsync":

        def boom(fd):
            raise OSError("disk full")

        monkeypatch.setattr(cr.os, "fsync", boom)
    else:
        real_write = os.write

        def partial(fd, data):
            real_write(fd, bytes(data)[:5])
            raise OSError("interrupted")

        monkeypatch.setattr(cr.os, "write", partial)
        monkeypatch.setattr(
            cr.os,
            "fdopen",
            lambda fd, mode: _Stream(fd),
        )
    with pytest.raises(cr.ReenrollRefused) as caught:
        cr._write_receipt(tmp_path, RECEIPT)
    assert str(caught.value.args[0]) == "receipt_unwritable"
    assert "synthetic" not in repr(caught.value) and "salad-test" not in repr(caught.value)
    assert os.listdir(tmp_path) == []


class _Stream:
    def __init__(self, fd):
        self.fd = fd

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        os.close(self.fd)

    def write(self, text):
        os.write(self.fd, text.encode())

    def flush(self):
        pass

    def fileno(self):
        return self.fd


def test_existing_receipt_is_refused_and_unmodified(tmp_path):
    final = tmp_path / FINAL
    final.write_bytes(b"older receipt\n")
    with pytest.raises(cr.ReenrollRefused) as caught:
        cr._write_receipt(tmp_path, RECEIPT)
    assert caught.value.args[0] == "receipt_unwritable"
    assert final.read_bytes() == b"older receipt\n"
    assert os.listdir(tmp_path) == [FINAL]


def test_symlink_at_final_name_not_followed(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_bytes(b"keep")
    (tmp_path / FINAL).symlink_to(target)
    with pytest.raises(cr.ReenrollRefused):
        cr._write_receipt(tmp_path, RECEIPT)
    assert target.read_bytes() == b"keep"
    assert sorted(os.listdir(tmp_path)) == sorted([FINAL, "elsewhere"])


def test_symlink_at_temp_name_not_followed_or_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(cr.os, "urandom", lambda n: b"\x00" * n)
    target = tmp_path / "elsewhere"
    target.write_bytes(b"keep")
    temp_name = f".{FINAL}.{os.getpid()}.{'00' * 6}.tmp"
    (tmp_path / temp_name).symlink_to(target)
    with pytest.raises(cr.ReenrollRefused):
        cr._write_receipt(tmp_path, RECEIPT)
    assert target.read_bytes() == b"keep"
    assert (tmp_path / temp_name).is_symlink()
    assert not (tmp_path / FINAL).exists()


def test_full_flow_returns_receipt_path(store, monkeypatch, capsys):  # noqa: F811
    path, receipts = store
    enroll_expired(path)
    code, out, _, _ = run(monkeypatch, capsys, argv(path, receipts))
    assert code == 0
    (receipt_file,) = list(receipts.iterdir())
    assert str(receipt_file) in out
    assert receipt_file.stat().st_mode & 0o777 == 0o600
    assert json.loads(receipt_file.read_text())["version"] == 2


def test_full_flow_existing_receipt_exits_stored(store, monkeypatch, capsys):  # noqa: F811
    path, receipts = store
    enroll_expired(path)
    (receipts / FINAL).write_bytes(b"older\n")
    code, out, err, _ = run(monkeypatch, capsys, argv(path, receipts))
    assert code == 3
    assert (receipts / FINAL).read_bytes() == b"older\n"
    assert NEW.decode() not in out + err
