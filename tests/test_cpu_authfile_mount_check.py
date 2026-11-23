"""The CPU runtime's mount-time auth-file check is the worker startup check (one helper)."""

from __future__ import annotations

import os

import pytest
from test_image_build_worker import _operation

from tessera_worker import private_auth_file
from tessera_worker.cpu import CpuError, DockerCpuAdapter
from tessera_worker.private_auth_file import AuthFileRefused, check_private_auth_file
from tessera_worker.service import DevelopmentWorkerError, private_registry_auth_file


def _adapter(operation):
    return DockerCpuAdapter(
        frozenset({operation.image}), egress_proxy_image="proxy@sha256:" + "d" * 64
    )


def _break(kind, tmp_path, auth, monkeypatch):
    if kind == "symlink":
        real = tmp_path / "real.json"
        real.write_text("{}")
        real.chmod(0o600)
        auth.unlink()
        auth.symlink_to(real)
        return "auth_file_symlink"
    if kind == "wrong_owner":
        other = os.geteuid() + 1
        monkeypatch.setattr(private_auth_file.os, "geteuid", lambda: other)
        return "auth_file_wrong_owner"
    if kind == "hardlink":
        os.link(auth, tmp_path / "second.json")
        return "auth_file_hardlinked"
    if kind == "group_bits":
        auth.chmod(0o640)
        return "auth_file_group_or_world_access"
    if kind == "world_bits":
        auth.chmod(0o604)
        return "auth_file_group_or_world_access"
    if kind == "directory":
        auth.unlink()
        auth.mkdir()
        return "auth_file_not_regular"
    auth.unlink()
    return "auth_file_unavailable"


KINDS = ["symlink", "wrong_owner", "hardlink", "group_bits", "world_bits", "directory", "missing"]


@pytest.mark.parametrize("kind", KINDS)
def test_each_rejected_case_is_refused_at_mount_and_at_startup(kind, tmp_path, monkeypatch):
    operation, _out, auth = _operation(tmp_path)
    code = _break(kind, tmp_path, auth, monkeypatch)
    with pytest.raises(AuthFileRefused) as raised:
        check_private_auth_file(auth)
    assert raised.value.code == code
    with pytest.raises(CpuError, match=code):
        _adapter(operation).create_arguments(operation)
    with pytest.raises(DevelopmentWorkerError, match=code):
        private_registry_auth_file(auth)


def test_a_private_file_passes_both_checks(tmp_path):
    operation, _out, auth = _operation(tmp_path)
    assert private_registry_auth_file(auth) == auth
    assert any(str(auth) in item for item in _adapter(operation).create_arguments(operation))


def test_the_two_checks_share_one_helper():
    from tessera_worker import cpu, service

    assert cpu.check_private_auth_file is service.check_private_auth_file


@pytest.mark.parametrize("kind", KINDS)
def test_refusal_messages_exclude_the_path_and_contents(kind, tmp_path, monkeypatch):
    marker = "marker-7f3c91d2"
    secret = "secret-content-4be8"
    directory = tmp_path / marker
    directory.mkdir()
    moved = directory / "auth.json"
    moved.write_text(secret)
    moved.chmod(0o600)
    _break(kind, directory, moved, monkeypatch)
    errors = []
    for call in (check_private_auth_file, private_registry_auth_file):
        with pytest.raises(Exception) as raised:  # noqa: B017 - either closed refusal type
            call(moved)
        errors.append(raised.value)
    for error in errors:
        chain = []
        while error is not None:
            chain.append(error)
            error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
        for link in chain:
            for text in (str(link), repr(link)):
                assert marker not in text and secret not in text
