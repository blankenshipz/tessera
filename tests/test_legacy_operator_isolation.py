"""Compatibility-only legacy fixtures: fail closed before real local authority I/O."""

import os
import socket
import subprocess
from pathlib import Path

import pytest

from tessera_operator import upgrade_support as support

_ORIGINAL_RUN = subprocess.run
_GUARDED_TESTS = {
    "test_guard_blocks_canonical_config_before_access",
    "test_guard_blocks_process_and_network_without_dispatch",
    "test_inspect_fixed_context_private_references",
    "test_rejects_insecure_kubeconfig_before_kubectl",
    "test_kubectl_error_redacts_output",
    "test_kubectl_rejects_oversized_output",
    "test_backup_verification_rejects_changed_snapshot_and_object",
    "test_online_target_backup_private_mac_custody",
    "test_target_stream_refuses_owner_header",
    "test_recreate_conditional_pod_delete_and_new_uid",
    "test_backup_rejects_foreign_pod_before_work",
    "test_post_recreation_compares_record_backend_prefix",
    "test_delete_posts_uid_and_resource_version_over_verified_local_api",
}


@pytest.fixture(autouse=True)
def legacy_io_guard(monkeypatch, request):
    # Do not change unrelated legacy tests that intentionally exercise local
    # subprocesses. Only the affected dependency-seam cases get these tripwires.
    if request.node.originalname not in _GUARDED_TESTS:
        yield []
        return
    forbidden = (str(support.KUBECONFIG), str(support.SOURCE))
    attempts = []

    def blocked(kind):
        def deny(*args, **kwargs):
            attempts.append(kind)
            pytest.fail("legacy sandbox blocked " + kind)

        return deny

    for name in ("open", "stat", "lstat"):
        original = getattr(Path, name)

        def guarded(path, *args, _original=original, _name=name, **kwargs):
            value = str(path)
            if any(value == root or value.startswith(root + "/") for root in forbidden):
                return blocked("real authority path " + _name)()
            return _original(path, *args, **kwargs)

        monkeypatch.setattr(Path, name, guarded)
    original_open = os.open

    def guarded_open(path, *args, **kwargs):
        value = str(path)
        if any(value == root or value.startswith(root + "/") for root in forbidden):
            return blocked("real authority os.open")()
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)
    monkeypatch.setattr(subprocess, "run", blocked("unfaked subprocess.run"))
    monkeypatch.setattr(subprocess, "Popen", blocked("unfaked subprocess.Popen"))
    monkeypatch.setattr(socket.socket, "connect", blocked("network connect"))
    monkeypatch.setattr(support.httpx, "Client", blocked("unfaked HTTP client"))
    yield attempts


def test_guard_blocks_canonical_config_before_access(legacy_io_guard):
    with pytest.raises(pytest.fail.Exception, match="real authority path"):
        support._kubeconfig_identity()
    assert legacy_io_guard == ["real authority path lstat"]


def test_guard_blocks_process_and_network_without_dispatch(legacy_io_guard):
    with pytest.raises(pytest.fail.Exception, match="subprocess.run"):
        support._docker("synthetic")
    with socket.socket() as connection:
        with pytest.raises(pytest.fail.Exception, match="network connect"):
            connection.connect(("127.0.0.1", 9))
    assert legacy_io_guard == ["unfaked subprocess.run", "network connect"]


def test_unrelated_cases_keep_original_subprocess_seam(legacy_io_guard):
    assert subprocess.run is _ORIGINAL_RUN
    assert legacy_io_guard == []
