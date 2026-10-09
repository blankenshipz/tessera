"""Real innocuous owned process/artifact reads, never actual research admission."""

import hashlib
import os
import shutil
import subprocess
from dataclasses import replace

import pytest

from tessera_agent.research_identity import (
    ArtifactReference,
    IdentityBlocked,
    OwnedResearchIdentity,
)


@pytest.fixture
def owned(tmp_path):
    root = tmp_path.resolve()
    root.chmod(0o700)
    executable = root / "owned-sleep"
    compiler = shutil.which("cc")
    assert compiler is not None, "Existing C compiler required for innocuous unsigned fixture"
    source = root / "fixture.c"
    source.write_text("#include <unistd.h>\nint main(void) { sleep(60); return 0; }\n")
    subprocess.run(
        [compiler, str(source), "-o", str(executable)], check=True, capture_output=True, timeout=30
    )
    executable.chmod(0o700)
    build = root / "approved-build.bin"
    build.write_bytes(b"nonsecret immutable build fixture")
    build.chmod(0o600)
    sdk = root / "mcp-1.26.0.dist-info"
    sdk.mkdir(mode=0o700)
    metadata = sdk / "METADATA"
    metadata.write_bytes(b"Metadata-Version: 2.1\nName: mcp\nVersion: 1.26.0\n\n")
    metadata.chmod(0o600)
    refs = tuple(
        ArtifactReference(
            kind, kind, str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest()
        )
        for kind, path in [("executable", executable), ("build", build), ("sdk-metadata", metadata)]
    )
    process = subprocess.Popen(
        [str(executable), "60"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={},
    )
    try:
        # Synchronize exec before enrolling the native path (Popen waits for exec).
        yield process, root, refs
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)


def exporter(owned):
    process, root, refs = owned
    return OwnedResearchIdentity(process, artifact_root=root, artifacts=refs)


def test_real_owned_identity_hashes_version_and_partial_scope(owned):
    identity = exporter(owned)
    proof = identity.observe("fresh-nonce-for-test-0001")
    assert proof["complete"] is False
    assert proof["process"]["pid"] == owned[0].pid
    assert proof["process"]["parent_pid"] == os.getpid()
    assert proof["process"]["live"] is True
    assert proof["process"]["started_at"]
    assert [row["sha256"] for row in proof["artifacts"]] == [ref.sha256 for ref in owned[2]]
    assert proof["artifacts"][2]["installed_version"] == "1.26.0"
    assert "launched_mcp_sdk_identity" in proof["missing"]
    assert "included_oauth_entitlement" in proof["missing"]
    assert "final_dispatch_hook_order" in proof["missing"]
    assert str(owned[1]) not in str(proof)
    with pytest.raises(IdentityBlocked, match="nonce_reused"):
        identity.observe("fresh-nonce-for-test-0001")


def test_exit_and_pid_handle_tamper_hold(owned):
    identity = exporter(owned)
    process = owned[0]
    process.pid += 1
    try:
        with pytest.raises(IdentityBlocked, match="handle_changed"):
            identity.observe("fresh-nonce-for-test-0002")
    finally:
        process.pid -= 1
    process.terminate()
    process.wait(timeout=5)
    with pytest.raises(IdentityBlocked, match="not_live"):
        identity.observe("fresh-nonce-for-test-0003")


@pytest.mark.parametrize("kind", ["executable", "build", "sdk-metadata"])
def test_approved_artifact_changes_hold(owned, kind):
    identity = exporter(owned)
    reference = next(ref for ref in owned[2] if ref.kind == kind)
    path = owned[1] / reference.relative_path
    # Replacing the executable leaves the old process mapped; either path/hash fails.
    replacement = path.with_name(path.name + ".replacement")
    replacement.write_bytes(b"changed nonsecret bytes")
    replacement.chmod(0o600)
    replacement.replace(path)
    with pytest.raises(IdentityBlocked):
        identity.observe("fresh-nonce-for-test-0004")


@pytest.mark.parametrize("change", ["symlink", "parent_symlink", "writable", "hardlink", "escape"])
def test_unsafe_artifact_paths_hold(owned, change):
    process, root, refs = owned
    build = root / refs[1].relative_path
    if change == "symlink":
        build.unlink()
        build.symlink_to(root / refs[2].relative_path)
    elif change == "parent_symlink":
        directory = root / "alias"
        directory.symlink_to(root / "mcp-1.26.0.dist-info", target_is_directory=True)
        refs = (*refs[:2], replace(refs[2], relative_path="alias/METADATA"))
    elif change == "writable":
        build.chmod(0o666)
    elif change == "hardlink":
        os.link(build, root / "second-name")
    else:
        refs = (refs[0], replace(refs[1], relative_path="../outside"), refs[2])
    with pytest.raises(IdentityBlocked):
        OwnedResearchIdentity(process, artifact_root=root, artifacts=refs)


def test_no_bare_pid_or_foreign_executable_admission(owned):
    process, root, refs = owned
    with pytest.raises(IdentityBlocked, match="not_live"):
        OwnedResearchIdentity(process.pid, artifact_root=root, artifacts=refs)
    with pytest.raises(IdentityBlocked, match="executable_reference_mismatch"):
        OwnedResearchIdentity(
            process,
            artifact_root=root,
            artifacts=(replace(refs[0], relative_path=refs[1].relative_path), *refs[1:]),
        )


def test_process_start_change_detected_without_pid_discovery(owned, monkeypatch):
    identity = exporter(owned)
    original = identity._metadata
    monkeypatch.setattr(identity, "_metadata", lambda: ("different start identity", original()[1]))
    with pytest.raises(IdentityBlocked, match="start_or_executable_changed"):
        identity.observe("fresh-nonce-for-test-0005")


def test_unowned_artifact_stat_rejected(owned, monkeypatch):
    identity = exporter(owned)
    original = os.fstat

    def foreign(fd):
        values = list(original(fd))
        values[4] = os.getuid() + 1
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", foreign)
    with pytest.raises(IdentityBlocked, match="ownership_or_type_denied"):
        identity.observe("fresh-nonce-for-test-0006")


@pytest.mark.parametrize("path", ["config.json", "runtime.py", "credentials.bin", "oauth/cache"])
def test_source_config_or_secret_references_rejected_without_open(owned, path):
    process, root, refs = owned
    with pytest.raises(IdentityBlocked, match="source_config_or_secret_reference_denied"):
        OwnedResearchIdentity(
            process,
            artifact_root=root,
            artifacts=(refs[0], replace(refs[1], relative_path=path), refs[2]),
        )
