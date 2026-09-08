from __future__ import annotations

import os
from pathlib import Path

import pytest

from tessera_worker.capability_probe import (
    CapabilityProbeError,
    CapabilityProbeFacts,
    evaluate_capability_probe,
)


def _facts(tmp_path: Path, **changes: object) -> CapabilityProbeFacts:
    runtime = tmp_path / "runtime"
    inputs = tmp_path / "inputs"
    runtime.mkdir(mode=0o700, exist_ok=True)
    inputs.mkdir(mode=0o700, exist_ok=True)
    values: dict[str, object] = {
        "runtime_profile_version": "local-cpu-v1",
        "expected_runtime_profile_version": "local-cpu-v1",
        "schema_version": "16",
        "expected_schema_version": "16",
        "runtime_root": runtime,
        "input_root": inputs,
    }
    values.update(changes)
    return CapabilityProbeFacts(**values)  # type: ignore[arg-type]


def test_wrong_versions_and_private_root_fail_closed(tmp_path: Path) -> None:
    facts = _facts(tmp_path, runtime_profile_version="other-v1", schema_version="15")
    report = evaluate_capability_probe(facts)
    assert not report.passed
    assert [check.passed for check in report.checks[:2]] == [False, False]

    os.chmod(facts.runtime_root, 0o755)
    assert not evaluate_capability_probe(facts).checks[2].passed
    facts.input_root.rmdir()
    assert not evaluate_capability_probe(facts).checks[3].passed


def test_symlink_root_is_not_private(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    facts = _facts(tmp_path, runtime_root=link)
    assert not evaluate_capability_probe(facts).checks[2].passed


def test_storage_is_canonicalized_but_declared_unverified(tmp_path: Path) -> None:
    declaration = {
        "contract_version": 1,
        "allocation_id": "alloc",
        "filesystem_id": "fs",
        "mount_id": "mount",
        "capacity_bytes": 100,
        "capacity_inodes": 2,
        "noexec": True,
        "nosuid": True,
        "nodev": True,
        "write_uid": 65532,
        "write_access_verified": True,
        "supervisor_custody_verified": True,
        "no_nested_mounts_verified": True,
    }
    report = evaluate_capability_probe(_facts(tmp_path, persistent_output_storage=declaration))
    check = report.checks[-1]
    assert check.passed and "declared/unverified" in check.detail
    declaration["capacity_bytes"] = 101
    changed = evaluate_capability_probe(_facts(tmp_path, persistent_output_storage=declaration))
    assert changed.checks[-1].passed


def test_malformed_storage_and_opencode_mismatch_fail(tmp_path: Path) -> None:
    report = evaluate_capability_probe(
        _facts(
            tmp_path,
            persistent_output_storage={"contract_version": 1},
            opencode_pinned_version="1.2.3",
            opencode_version_observer=lambda: "1.2.4",
        )
    )
    assert not report.checks[-2].passed
    assert not report.checks[-1].passed


def test_bounds_strict_fields_secrets_and_callback_failure(tmp_path: Path) -> None:
    with pytest.raises(CapabilityProbeError):
        CapabilityProbeFacts.from_mapping({"unknown": "value"})
    with pytest.raises(CapabilityProbeError):
        _facts(tmp_path, persistent_output_storage={"api_token": "never"})
    report = evaluate_capability_probe(
        _facts(
            tmp_path,
            opencode_pinned_version="1.2.3",
            opencode_version_observer=lambda: (_ for _ in ()).throw(RuntimeError()),
        )
    )
    assert not report.checks[-1].passed


def test_passing_report_is_deterministic_non_authoritative_and_side_effect_free(
    tmp_path: Path,
) -> None:
    facts = _facts(
        tmp_path,
        opencode_pinned_version="1.2.3",
        opencode_version_observer=lambda: "1.2.3",
    )
    before = sorted(path.name for path in tmp_path.iterdir())
    first = evaluate_capability_probe(facts)
    second = evaluate_capability_probe(facts)
    assert first == second
    assert first.passed
    assert first.authority_effect == "none"
    assert first.attestation_verified is False
    assert first.dispatch_enabled is False
    assert sorted(path.name for path in tmp_path.iterdir()) == before
