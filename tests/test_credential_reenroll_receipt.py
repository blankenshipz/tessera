"""The re-enrolment receipt file is written atomically and never overwrites an existing one."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import sys
import types

import pytest
from test_credential_reenroll import (  # noqa: F401
    NEW,
    argv,
    enroll_expired,
    no_secret,
    run,
    store,
)

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
    assert caught.value.args[0] == "receipt_exists"
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
    assert "stored_receipt_exists" in err and "IS stored" in err and "NOT written" in err
    assert (receipts / FINAL).read_bytes() == b"older\n"
    assert NEW.decode() not in out + err


def _link_unsupported(code):
    def link(source, target):
        raise OSError(code, os.strerror(code))

    return link


def _renameat2_available():
    if not sys.platform.startswith("linux"):
        return False
    try:
        return hasattr(ctypes.CDLL(None, use_errno=True), "renameat2")
    except OSError:
        return False


def _fail_inside_receipt_write(monkeypatch, failure):
    """Run the real _write_receipt with a failure injected only inside it (not in the store)."""
    real = cr._write_receipt

    def failing(directory, receipt):
        with pytest.MonkeyPatch.context() as inner:
            if failure == "fsync":

                def boom(fd):
                    raise OSError("disk full")

                inner.setattr(cr.os, "fsync", boom)
            else:
                real_write = os.write

                def partial(fd, data):
                    real_write(fd, bytes(data)[:5])
                    raise OSError("interrupted")

                inner.setattr(cr.os, "write", partial)
                inner.setattr(cr.os, "fdopen", lambda fd, mode: _Stream(fd))
            return real(directory, receipt)

    monkeypatch.setattr(cr, "_write_receipt", failing)


def _assert_stored_exit(code, out, err, category):
    assert code == 3
    assert category in err and "IS stored" in err and "NOT written" in err
    assert json.loads(out)["version"] == 2  # the receipt is on stdout so the record is not lost
    assert "receipt:" not in out + err  # never claims a receipt file
    no_secret(out, err)


@pytest.mark.parametrize("failure", ["fsync", "write"])
def test_cli_mid_write_failure_exits_stored_unwritable(store, monkeypatch, capsys, failure):  # noqa: F811
    path, receipts = store
    enroll_expired(path)
    _fail_inside_receipt_write(monkeypatch, failure)
    code, out, err, _ = run(monkeypatch, capsys, argv(path, receipts))
    _assert_stored_exit(code, out, err, "stored_receipt_unwritable")
    assert os.listdir(receipts) == []  # no partial final file, no temp


@pytest.mark.parametrize("code", [errno.EPERM, errno.ENOTSUP, errno.EMLINK])
def test_no_hard_links_falls_back_to_noreplace_rename(tmp_path, monkeypatch, code):
    if not _renameat2_available():
        pytest.skip("renameat2 is not available on this platform")
    monkeypatch.setattr(cr.os, "link", _link_unsupported(code))
    try:
        path = cr._write_receipt(tmp_path, RECEIPT)
    except cr.ReenrollRefused as refused:
        if refused.category == "receipt_link_unsupported":
            pytest.skip("this filesystem does not support RENAME_NOREPLACE")
        raise
    assert path == tmp_path / FINAL
    assert os.listdir(tmp_path) == [FINAL]
    assert path.read_bytes() == EXPECTED
    assert path.stat().st_mode & 0o777 == 0o600


def test_fallback_refuses_an_existing_receipt(tmp_path, monkeypatch):
    if not _renameat2_available():
        pytest.skip("renameat2 is not available on this platform")
    monkeypatch.setattr(cr.os, "link", _link_unsupported(errno.EPERM))
    final = tmp_path / FINAL
    final.write_bytes(b"older receipt\n")
    with pytest.raises(cr.ReenrollRefused) as caught:
        cr._write_receipt(tmp_path, RECEIPT)
    assert caught.value.category == "receipt_exists"
    assert final.read_bytes() == b"older receipt\n"
    assert os.listdir(tmp_path) == [FINAL]


def test_fallback_existing_receipt_exits_stored_exists(store, monkeypatch, capsys):  # noqa: F811
    path, receipts = store
    enroll_expired(path)
    (receipts / FINAL).write_bytes(b"older\n")
    monkeypatch.setattr(cr.os, "link", _link_unsupported(errno.EPERM))

    def noreplace(source, target):  # the seam answers as renameat2 would for an existing target
        assert os.path.exists(target)
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST))

    monkeypatch.setattr(cr, "_rename_noreplace", noreplace)
    code, out, err, _ = run(monkeypatch, capsys, argv(path, receipts))
    _assert_stored_exit(code, out, err, "stored_receipt_exists")
    assert os.listdir(receipts) == [FINAL]
    assert (receipts / FINAL).read_bytes() == b"older\n"


def _seam_unsupported(source, target):
    raise cr._NoReplaceUnsupported


def test_neither_link_nor_noreplace_rename_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(cr.os, "link", _link_unsupported(errno.EPERM))
    monkeypatch.setattr(cr, "_rename_noreplace", _seam_unsupported)
    with pytest.raises(cr.ReenrollRefused) as caught:
        cr._write_receipt(tmp_path, RECEIPT)
    assert caught.value.category == "receipt_link_unsupported"
    assert os.listdir(tmp_path) == []


def test_neither_link_nor_noreplace_rename_exits_stored(store, monkeypatch, capsys):  # noqa: F811
    path, receipts = store
    enroll_expired(path)
    monkeypatch.setattr(cr.os, "link", _link_unsupported(errno.ENOTSUP))
    monkeypatch.setattr(cr, "_rename_noreplace", _seam_unsupported)
    code, out, err, _ = run(monkeypatch, capsys, argv(path, receipts))
    _assert_stored_exit(code, out, err, "stored_receipt_link_unsupported")
    assert os.listdir(receipts) == []


def test_unexpected_link_errno_does_not_fall_back(tmp_path, monkeypatch):
    monkeypatch.setattr(cr.os, "link", _link_unsupported(errno.EXDEV))

    def never(source, target):
        raise AssertionError("no fallback for an unexpected errno")

    monkeypatch.setattr(cr, "_rename_noreplace", never)
    with pytest.raises(cr.ReenrollRefused) as caught:
        cr._write_receipt(tmp_path, RECEIPT)
    assert caught.value.category == "receipt_unwritable"
    assert os.listdir(tmp_path) == []


def test_noreplace_unsupported_errnos_map_to_unsupported(tmp_path, monkeypatch):
    if not _renameat2_available():
        pytest.skip("renameat2 is not available on this platform")

    class Fake:
        argtypes = restype = None

        def __call__(self, *args):
            ctypes.set_errno(errno.EINVAL)
            return -1

    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: type("L", (), {"renameat2": Fake()})())
    source = tmp_path / "a"
    source.write_bytes(b"x")
    with pytest.raises(cr._NoReplaceUnsupported):
        cr._rename_noreplace(source, tmp_path / "b")
    assert source.exists()  # never a plain rename that could overwrite


def _native_noreplace_supported():
    return sys.platform.startswith("linux") or sys.platform == "darwin"


@pytest.mark.skipif(not _native_noreplace_supported(), reason="no native no-replace rename")
def test_rename_noreplace_real_end_to_end(tmp_path):
    source = tmp_path / "src"
    source.write_bytes(b"new")
    target = tmp_path / "dst"
    try:
        cr._rename_noreplace(source, target)
    except cr._NoReplaceUnsupported:
        pytest.skip("filesystem does not support no-replace rename")
    assert target.read_bytes() == b"new" and not source.exists()

    # an existing target is refused and never replaced
    other = tmp_path / "other"
    other.write_bytes(b"other")
    with pytest.raises(FileExistsError):
        cr._rename_noreplace(other, target)
    assert target.read_bytes() == b"new" and other.read_bytes() == b"other"

    # a symlink target (even a dangling one) is refused, not followed or replaced
    outside = tmp_path / "outside"
    link = tmp_path / "link"
    link.symlink_to(outside)
    with pytest.raises(FileExistsError):
        cr._rename_noreplace(other, link)
    assert link.is_symlink() and not outside.exists() and other.exists()


@pytest.mark.parametrize(
    ("platform", "symbol", "flag"),
    [("linux", "renameat2", cr._RENAME_NOREPLACE), ("darwin", "renamex_np", cr._RENAME_EXCL)],
)
def test_rename_noreplace_dispatches_by_platform(tmp_path, monkeypatch, platform, symbol, flag):
    calls = []

    def backend(name):
        def call(*args):
            calls.append((name, args))
            return 0

        call.argtypes = call.restype = None
        return call

    lib = types.SimpleNamespace(renameat2=backend("renameat2"), renamex_np=backend("renamex_np"))
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: lib)
    monkeypatch.setattr(sys, "platform", platform)
    cr._rename_noreplace(tmp_path / "a", tmp_path / "b")
    assert [name for name, _ in calls] == [symbol]  # the other backend is never called
    assert calls[0][1][-1] == flag


def test_rename_noreplace_unknown_platform_is_unsupported(tmp_path, monkeypatch):
    lib = types.SimpleNamespace(renameat2=lambda *a: 0, renamex_np=lambda *a: 0)
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: lib)
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(cr._NoReplaceUnsupported):
        cr._rename_noreplace(tmp_path / "a", tmp_path / "b")
