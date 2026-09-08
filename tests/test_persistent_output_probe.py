from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from tessera_worker.persistent_output_probe import (
    CapacityFacts,
    MountFacts,
    PathFacts,
    PersistentOutputProbeFacts,
    _unescape_mount_path,
    inspect_persistent_output_storage,
    persistent_output_storage_contract,
)


def _facts(**changes: object) -> PersistentOutputProbeFacts:
    path = "/slots/output-1"
    node = PathFacts(True, False, 0o40700, 65532, 65532, 42, 99)
    values: dict[str, object] = {
        "platform_name": "Linux",
        "requested_path": path,
        "canonical_path": path,
        "initial_path": node,
        "final_path": node,
        "mount": MountFacts(
            "linux-mount-17",
            "linux-fs-0:42-ext4-_dev_loop7",
            path,
            42,
            frozenset({"rw", "noexec", "nosuid", "nodev"}),
        ),
        "nested_mounts": False,
        "capacity": CapacityFacts(1024, 8),
        "uid_65532_write_access": True,
        "supervisor_uid": 0,
    }
    values.update(changes)
    return PersistentOutputProbeFacts(**values)  # type: ignore[arg-type]


def test_returns_existing_canonical_contract_only_when_every_check_passes() -> None:
    assert persistent_output_storage_contract(_facts()) == {
        "contract_version": 1,
        "allocation_id": "linux-allocation-2a-63",
        "filesystem_id": "linux-fs-0:42-ext4-_dev_loop7",
        "mount_id": "linux-mount-17",
        "capacity_bytes": 1024,
        "capacity_inodes": 8,
        "noexec": True,
        "nosuid": True,
        "nodev": True,
        "write_uid": 65532,
        "write_access_verified": True,
        "supervisor_custody_verified": True,
        "no_nested_mounts_verified": True,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"initial_path": None},
        {"initial_path": replace(_facts().initial_path, is_directory=False)},
        {"initial_path": replace(_facts().initial_path, is_symlink=True)},
        {"final_path": replace(_facts().final_path, inode=100)},
        {"canonical_path": "/different-slot"},
        {"initial_path": replace(_facts().initial_path, mode=0o40755)},
        {"initial_path": replace(_facts().initial_path, uid=0)},
        {"initial_path": replace(_facts().initial_path, gid=0)},
        {"supervisor_uid": 1000},
        {"supervisor_uid": 65532},
        {"mount": None},
        {"mount": replace(_facts().mount, mountpoint="/other")},
        {"mount": replace(_facts().mount, device=43)},
        {"mount": replace(_facts().mount, mount_id="not/a-mount")},
        {"nested_mounts": True},
        {"capacity": CapacityFacts(0, 8)},
        {"capacity": CapacityFacts(1024, 0)},
        {"uid_65532_write_access": False},
    ],
)
def test_every_unsafe_or_missing_observation_fails_closed(changes: dict[str, object]) -> None:
    assert persistent_output_storage_contract(_facts(**changes)) is None


@pytest.mark.parametrize("flag", ["noexec", "nosuid", "nodev"])
def test_missing_required_mount_flags_fail_closed(flag: str) -> None:
    mount = _facts().mount
    assert mount is not None
    assert (
        persistent_output_storage_contract(_facts(mount=replace(mount, flags=mount.flags - {flag})))
        is None
    )


@pytest.mark.parametrize("platform_name", ["Darwin", "Windows", "FreeBSD", "LinuxDesktop"])
def test_non_linux_platforms_are_explicitly_non_admissible(platform_name: str) -> None:
    assert persistent_output_storage_contract(_facts(platform_name=platform_name)) is None


def test_mac_host_is_rejected_before_path_inspection() -> None:
    assert (
        inspect_persistent_output_storage(
            Path("/path-that-must-not-be-inspected"), platform_name="Darwin"
        )
        is None
    )


def test_linux_mount_path_escapes_are_decoded_without_shelling_out() -> None:
    assert _unescape_mount_path("/slots/output\\0401") == "/slots/output 1"


def test_pure_validation_is_immutable_and_never_needs_a_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facts = _facts()
    before = repr(facts)
    monkeypatch.setattr(
        "subprocess.run", lambda *args, **kwargs: pytest.fail("probe must not invoke subprocess")
    )
    assert persistent_output_storage_contract(facts) is not None
    assert repr(facts) == before
