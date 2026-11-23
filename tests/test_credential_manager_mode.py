"""A service manager's credential files (root-owned 0400/0440) are accepted only where they belong.

systemd's LoadCredential= places root-owned read-only files under a root-owned directory. The
service user can read them, so the reasoning service and its readiness check must accept exactly
that shape under ``$CREDENTIALS_DIRECTORY`` and nothing wider: never world-readable, never writable
by group or other, never a link, never outside the directory.
"""

from __future__ import annotations

import os
import stat

import pytest

from tessera_agent import remote_reasoning_store as store
from tessera_agent.remote_reasoning_store import (
    manager_directory_ok,
    manager_file_ok,
    private_file,
)

ME = os.getuid()


SERVICE_GID = 4242
FOREIGN_GID = 100  # e.g. ``users``: every member could read a 0440 file in that group


def stat_of(mode, uid, *, kind=stat.S_IFREG, links=1, size=10, gid=0):
    return os.stat_result((kind | mode, 1, 1, links, uid, gid, size, 0, 0, 0))


@pytest.mark.parametrize(
    ("mode", "uid", "ok"),
    [
        (0o400, 0, True),
        (0o440, 0, True),
        (0o600, 0, False),  # root-owned but writable: not what a manager produces
        (0o444, 0, False),  # world-readable
        (0o404, 0, False),
        (0o640, 0, False),
        (0o660, 0, False),
        (0o4440, 0, False),  # setuid bit
        (0o440, 12345, False),  # owned by a stranger
        (0o400, 12345, False),
        (0o600, ME, True),  # the caller's own private file (the strict, original rule)
        (0o400, ME, True),
        (0o640, ME, False),
        (0o604, ME, False),
    ],
)
def test_the_file_modes_and_owners_that_are_accepted(mode, uid, ok):
    assert manager_file_ok(stat_of(mode, uid), caller_uid=ME) is ok


def test_a_manager_file_must_be_a_single_regular_file():
    assert not manager_file_ok(stat_of(0o440, 0, links=2), caller_uid=ME)
    assert not manager_file_ok(stat_of(0o440, 0, kind=stat.S_IFLNK), caller_uid=ME)
    assert not manager_file_ok(stat_of(0o440, 0, kind=stat.S_IFDIR), caller_uid=ME)


@pytest.mark.parametrize(
    ("mode", "uid", "ok"),
    [
        (0o550, 0, True),
        (0o500, 0, True),
        (0o700, 0, True),
        (0o750, 0, True),
        (0o770, 0, False),  # group-writable
        (0o755, 0, False),  # others can enter
        (0o555, 0, False),
        (0o700, ME, True),
        (0o550, 12345, False),
    ],
)
def test_the_directory_a_manager_places_credentials_in(mode, uid, ok):
    info = stat_of(mode, uid, kind=stat.S_IFDIR)
    assert manager_directory_ok(info, caller_uid=ME) is ok


# -- the group of a manager credential is pinned to root or the service's own group -------------


@pytest.mark.parametrize(
    ("mode", "gid", "ok"),
    [
        (0o440, 0, True),  # group root
        (0o440, SERVICE_GID, True),  # the service's own group
        (0o440, FOREIGN_GID, False),  # every member of ``users`` could read the key
        (0o400, 0, True),
        (0o400, SERVICE_GID, True),
        (0o400, FOREIGN_GID, False),  # the group is pinned even when it has no bits
    ],
)
def test_a_root_owned_manager_file_must_be_in_root_or_the_service_group(mode, gid, ok):
    info = stat_of(mode, 0, gid=gid)
    assert manager_file_ok(info, caller_uid=ME, service_gid=SERVICE_GID) is ok


@pytest.mark.parametrize(
    ("mode", "gid", "ok"),
    [
        (0o750, 0, True),
        (0o750, SERVICE_GID, True),
        (0o750, FOREIGN_GID, False),
        (0o550, FOREIGN_GID, False),
        (0o700, FOREIGN_GID, False),  # root-owned: the group is pinned whatever its bits
    ],
)
def test_a_root_owned_manager_directory_must_be_in_root_or_the_service_group(mode, gid, ok):
    info = stat_of(mode, 0, kind=stat.S_IFDIR, gid=gid)
    assert manager_directory_ok(info, caller_uid=ME, service_gid=SERVICE_GID) is ok


def test_the_service_group_defaults_to_the_effective_gid(monkeypatch):
    monkeypatch.setattr(os, "getegid", lambda: SERVICE_GID)
    assert manager_file_ok(stat_of(0o440, 0, gid=SERVICE_GID), caller_uid=ME)
    assert manager_directory_ok(stat_of(0o750, 0, kind=stat.S_IFDIR, gid=SERVICE_GID), ME)
    assert not manager_file_ok(stat_of(0o440, 0, gid=FOREIGN_GID), caller_uid=ME)
    assert not manager_directory_ok(stat_of(0o750, 0, kind=stat.S_IFDIR, gid=FOREIGN_GID), ME)


def test_the_callers_own_entries_are_judged_as_before_whatever_the_group():
    # The caller's private file and private directory grant the group nothing.
    assert manager_file_ok(stat_of(0o600, ME, gid=FOREIGN_GID), ME, service_gid=SERVICE_GID)
    assert not manager_file_ok(stat_of(0o640, ME, gid=SERVICE_GID), ME, service_gid=SERVICE_GID)
    directory = stat_of(0o700, ME, kind=stat.S_IFDIR, gid=FOREIGN_GID)
    assert manager_directory_ok(directory, ME, service_gid=SERVICE_GID)
    # ... but once the group may enter, it must be root's or the service's.
    directory = stat_of(0o750, ME, kind=stat.S_IFDIR, gid=FOREIGN_GID)
    assert not manager_directory_ok(directory, ME, service_gid=SERVICE_GID)


@pytest.mark.parametrize("other", [0o001, 0o002, 0o004, 0o005, 0o007])
def test_any_access_for_others_still_refuses_a_directory(other):
    for gid in (0, SERVICE_GID):
        info = stat_of(0o750 | other, 0, kind=stat.S_IFDIR, gid=gid)
        assert not manager_directory_ok(info, caller_uid=ME, service_gid=SERVICE_GID)
        info = stat_of(0o700 | other, ME, kind=stat.S_IFDIR, gid=gid)
        assert not manager_directory_ok(info, caller_uid=ME, service_gid=SERVICE_GID)


def test_a_manager_file_in_a_foreign_group_is_refused_by_private_file(credentials, monkeypatch):
    directory, secret, _ = credentials
    real = store._fstat

    def foreign(fd):
        info = real(fd)
        return os.stat_result((info.st_mode, 1, 1, 1, 0, FOREIGN_GID, info.st_size, 0, 0, 0))

    monkeypatch.setattr(store, "_fstat", foreign)
    with pytest.raises(ValueError):
        private_file(secret, credentials_dir=directory)


def test_a_manager_directory_in_a_foreign_group_is_refused_by_private_file(
    credentials, monkeypatch
):
    directory, secret, _ = credentials
    real = store._lstat

    def foreign(path, *a, **k):
        info = real(path, *a, **k)
        return os.stat_result(
            (stat.S_IFDIR | 0o750, info.st_ino, info.st_dev, 2, 0, FOREIGN_GID, 0, 0, 0, 0)
        )

    monkeypatch.setattr(store, "_lstat", foreign)
    with pytest.raises(ValueError):
        private_file(secret, credentials_dir=directory)


# -- the MCP agent-token reader applies the same rule ---------------------------------------------


_AGREEMENT_CASES = [
    # (file mode, file uid, file gid, links, dir mode, dir uid, dir gid, accepted)
    (0o440, 0, 0, 1, 0o750, 0, 0, True),
    (0o400, 0, 0, 1, 0o500, 0, 0, True),
    (0o440, 0, SERVICE_GID, 1, 0o750, 0, SERVICE_GID, True),
    (0o440, 0, FOREIGN_GID, 1, 0o750, 0, 0, False),
    (0o400, 0, FOREIGN_GID, 1, 0o500, 0, 0, False),
    (0o440, 0, 0, 1, 0o750, 0, FOREIGN_GID, False),
    (0o440, 0, 0, 1, 0o550, 0, FOREIGN_GID, False),
    (0o440, 0, 0, 2, 0o750, 0, 0, False),
    (0o444, 0, 0, 1, 0o750, 0, 0, False),
    (0o640, 0, 0, 1, 0o750, 0, 0, False),
    (0o600, 0, 0, 1, 0o750, 0, 0, False),
    (0o440, 0, 0, 1, 0o770, 0, 0, False),
    (0o440, 0, 0, 1, 0o755, 0, 0, False),
    (0o440, 0, 0, 1, 0o751, 0, 0, False),
    (0o440, 0, 0, 1, 0o700, 12345, 0, False),
    (0o440, 12345, 0, 1, 0o750, 0, 0, False),
]
CALLER = 1000


@pytest.mark.parametrize("case", _AGREEMENT_CASES)
def test_the_mcp_config_and_the_store_agree_on_manager_credentials(case, tmp_path, monkeypatch):
    from tessera_mcp.config import Config

    fmode, fuid, fgid, links, dmode, duid, dgid, accepted = case
    directory = tmp_path / "creds"
    directory.mkdir()
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(directory))
    monkeypatch.setattr(os, "getegid", lambda: SERVICE_GID)
    monkeypatch.setattr(os, "getuid", lambda: CALLER)
    dir_info = stat_of(dmode, duid, kind=stat.S_IFDIR, gid=dgid)
    file_info = stat_of(fmode, fuid, links=links, gid=fgid)
    real = os.lstat
    monkeypatch.setattr(
        os, "lstat", lambda p, *a, **k: dir_info if str(p) == str(directory) else real(p, *a, **k)
    )
    config = Config("http://localhost", directory / "agent-token", "project", "destination")
    store_says = manager_directory_ok(dir_info, CALLER) and manager_file_ok(file_info, CALLER)
    assert store_says is accepted
    assert config._manager_credential(file_info) is accepted


@pytest.fixture
def credentials(tmp_path, monkeypatch):
    """A directory with one credential, presented as root-owned (a test cannot chown)."""
    directory = tmp_path / "creds"
    directory.mkdir(mode=0o700)
    secret = directory / "profile"
    secret.write_bytes(b"synthetic")
    secret.chmod(0o440)
    outside = tmp_path / "outside"
    outside.write_bytes(b"synthetic")
    outside.chmod(0o440)
    real_fstat, real_lstat = os.fstat, os.lstat

    def as_root_file(fd):
        info = real_fstat(fd)
        return os.stat_result(
            (info.st_mode, info.st_ino, info.st_dev, 1, 0, 0, info.st_size, 0, 0, 0)
        )

    def as_root_dir(path, *a, **k):
        info = real_lstat(path, *a, **k)
        if str(path) == str(directory):
            mode = stat.S_IFDIR | 0o550
            return os.stat_result((mode, info.st_ino, info.st_dev, 2, 0, 0, 0, 0, 0, 0))
        return info

    monkeypatch.setattr(store, "_fstat", as_root_file)
    monkeypatch.setattr(store, "_lstat", as_root_dir)
    return directory, secret, outside


def test_a_root_owned_manager_file_is_read_from_the_credentials_directory(credentials):
    directory, secret, _ = credentials
    assert private_file(secret, credentials_dir=directory) == b"synthetic"


def test_the_same_file_is_refused_without_naming_the_directory_or_outside_it(credentials):
    directory, secret, outside = credentials
    with pytest.raises(ValueError):
        private_file(secret)  # no credentials_dir: the strict rule applies
    with pytest.raises(ValueError):
        private_file(outside, credentials_dir=directory)  # not inside the directory
    with pytest.raises(ValueError):
        private_file(directory / ".." / "outside", credentials_dir=directory)


def test_a_world_readable_or_wider_file_is_refused_even_inside(credentials):
    directory, secret, _ = credentials
    for mode in (0o444, 0o640, 0o660, 0o404):
        secret.chmod(mode)
        with pytest.raises(ValueError):
            private_file(secret, credentials_dir=directory)


def test_an_untrusted_directory_makes_the_file_untrusted(credentials, monkeypatch):
    directory, secret, _ = credentials
    real = store._lstat
    for mode in (0o770, 0o755):  # group-writable, or enterable by others

        def wide(path, *a, _mode=mode, **k):
            info = real(path, *a, **k)
            return os.stat_result(
                (stat.S_IFDIR | _mode, info.st_ino, info.st_dev, 2, 0, 0, 0, 0, 0, 0)
            )

        monkeypatch.setattr(store, "_lstat", wide)
        with pytest.raises(ValueError):
            private_file(secret, credentials_dir=directory)


def test_a_symlink_is_never_followed(credentials, tmp_path):
    directory, secret, _ = credentials
    link = directory / "link"
    link.symlink_to(secret)
    with pytest.raises(OSError):
        private_file(link, credentials_dir=directory)


def test_the_callers_own_private_file_still_reads_anywhere(tmp_path):
    path = tmp_path / "mine"
    path.write_bytes(b"x")
    path.chmod(0o600)
    assert private_file(path) == b"x"
    path.chmod(0o640)
    with pytest.raises(ValueError):
        private_file(path)


def test_an_oversized_manager_file_is_refused(credentials):
    directory, secret, _ = credentials
    secret.chmod(0o600)
    secret.write_bytes(b"x" * (64 * 1024 + 1))
    secret.chmod(0o440)
    with pytest.raises(ValueError):
        private_file(secret, credentials_dir=directory)


# -- the readiness key-file check sees the same shape ---------------------------------------------


def _present_as_root(monkeypatch, directory, name, mode, uid=0):
    real = os.lstat

    def fake(path, *a, **k):
        info = real(path, *a, **k)
        if str(path) == str(directory):
            return os.stat_result(
                (stat.S_IFDIR | 0o550, info.st_ino, info.st_dev, 2, 0, 0, 0, 0, 0, 0)
            )
        if str(path) == str(directory / name):
            return os.stat_result(
                (stat.S_IFREG | mode, info.st_ino, info.st_dev, 1, uid, 0, info.st_size, 0, 0, 0)
            )
        return info

    monkeypatch.setattr(os, "lstat", fake)


def test_readiness_accepts_the_manager_key_file_and_still_refuses_wider_ones(tmp_path, monkeypatch):
    from tessera_agent import first_call_readiness as fcr

    directory = tmp_path / "creds"
    directory.mkdir(mode=0o700)
    (directory / fcr.ANTHROPIC_API_KEY_FILE).write_bytes(b"synthetic-key-marker")
    for mode, expected in ((0o400, "ok"), (0o440, "ok"), (0o444, "fail"), (0o640, "fail")):
        with monkeypatch.context() as patch:
            _present_as_root(patch, directory, fcr.ANTHROPIC_API_KEY_FILE, mode)
            check, key = fcr._api_key_file(directory)
        assert check["status"] == expected, (mode, check)
        assert key["present"] is True
