"""CLI coverage for the no-kubectl Astra installation plan renderer."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path

from tessera_control.cli import main, parser
from tessera_provisioner.astra_installation import (
    InstallationFinding,
    InstallationIdentity,
    InstallationInspection,
    TargetIdentity,
    inspection_json,
)


def _arguments(*extra: str) -> list[str]:
    return [
        "astra-installation",
        "plan",
        "--mode",
        "create",
        "--installation-name",
        "astra-control",
        "--profile",
        "astra-phase1",
        "--kubeconfig-reference-sha256",
        "c" * 64,
        "--context",
        "named-target",
        "--server",
        "https://cluster.example.test:6443",
        "--ca-sha256",
        "d" * 64,
        "--cluster-uid",
        "cluster-uid",
        "--namespace",
        "astra-system",
        "--namespace-uid",
        "namespace-uid",
        "--desired-spec-sha256",
        "a" * 64,
        "--approval-expires-at",
        "2026-09-19T00:00:00Z",
        *extra,
    ]


def _run(monkeypatch, *arguments: str) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["tessera", *arguments])
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = main()
    return result, stdout.getvalue(), stderr.getvalue()


def test_offline_plan_rendering_is_deterministic_and_never_spawns_a_command(monkeypatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("offline plan renderer must not execute a command")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    first = _run(monkeypatch, *_arguments())
    second = _run(monkeypatch, *_arguments())

    assert first == second
    status, stdout, stderr = first
    assert status == 0 and stderr == ""
    rendered = json.loads(stdout)
    assert rendered["digest"]
    assert rendered["inspection"]["verification"] == "offline_unverified"
    assert rendered["inspection"]["source"] == "offline-input"


def test_snapshot_must_match_explicit_identity_and_mutating_conflicts_fail_closed(
    tmp_path: Path, monkeypatch
) -> None:
    identity = InstallationIdentity("astra-control", "astra-phase1")
    target = TargetIdentity(
        "c" * 64,
        "named-target",
        "https://cluster.example.test:6443",
        "d" * 64,
        "cluster-uid",
        "astra-system",
        "namespace-uid",
    )
    inspection = InstallationInspection(
        identity,
        target,
        None,
        findings=(InstallationFinding("storage", "incompatible", "astra-outputs"),),
    )
    snapshot = tmp_path / "inspection.json"
    snapshot.write_text(inspection_json(inspection))

    status, stdout, stderr = _run(monkeypatch, *_arguments("--inspection-snapshot", str(snapshot)))
    assert status == 1 and stdout == ""
    assert "incompatible resource conflicts" in stderr
    assert "offline_unverified" in stderr

    observe_arguments = _arguments()
    observe_arguments[observe_arguments.index("create")] = "observe"
    desired_index = observe_arguments.index("--desired-spec-sha256")
    del observe_arguments[desired_index : desired_index + 2]
    observe_arguments.extend(("--inspection-snapshot", str(snapshot)))
    status, stdout, stderr = _run(monkeypatch, *observe_arguments)
    assert status == 0 and stderr == ""
    assert json.loads(stdout)["inspection"]["source"] == "offline-snapshot"


def test_namespace_cli_help_exposes_the_narrow_non_installer_boundary() -> None:
    help_text = parser()._subparsers._group_actions[0].choices["astra-namespace"].format_help()

    assert (
        "inspect-existing" in help_text and "plan-create" in help_text and "reconcile" in help_text
    )
    assert "never install Astra" in help_text
