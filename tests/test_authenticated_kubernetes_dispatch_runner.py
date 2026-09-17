"""Unit boundaries for the opt-in authenticated Kubernetes live runner."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def runner_module():
    path = Path(__file__).parents[1] / "scripts" / "check_authenticated_kubernetes_dispatch_e2e.py"
    spec = importlib.util.spec_from_file_location("authenticated_kubernetes_dispatch_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_recovery_modes_use_only_the_deterministic_sleep_write_workload(runner_module) -> None:
    assert runner_module._workload("success", 30) == ("/hello", "--write", "/outputs/result.txt")
    for mode in ("cancel", "restart", "lost-report"):
        assert runner_module._workload(mode, 17) == (
            "/hello",
            "--sleep-write",
            "17",
            "/outputs/result.txt",
        )


def test_cancellation_reads_the_grant_identity_from_the_control_package(runner_module) -> None:
    assert runner_module._grant_id({"package": {"grant_id": "grant-1"}}) == "grant-1"
    with pytest.raises(runner_module.DispatchE2EError, match="grant identity"):
        runner_module._grant_id({"grant_id": "wrong-level"})


def test_node_image_refuses_an_unnamed_or_missing_node_resident_reference(
    monkeypatch: pytest.MonkeyPatch, runner_module
) -> None:
    class Completed:
        stdout = "docker.io/library/tessera-e2e-hello@sha256:" + "a" * 64 + "\n"

    monkeypatch.setattr(runner_module, "_run", lambda _argv: Completed())
    with pytest.raises(runner_module.DispatchE2EUnavailable, match="node-resident"):
        runner_module._node_image("docker.io/library/tessera-e2e-hello@sha256:" + "b" * 64)


def test_cli_classifies_passed_fixture_without_replacing_operational_state(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], runner_module
) -> None:
    monkeypatch.setattr(
        runner_module,
        "_main",
        lambda _args: {"state": "cancel_requested", "cancellation": {"state": "unknown"}},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "runner",
            "--run",
            "--kubeconfig",
            "/tmp/fixture-kubeconfig",
            "--image",
            "docker.io/library/tessera-e2e-hello@sha256:" + "a" * 64,
        ],
    )

    assert runner_module.main() == 0

    assert json.loads(capsys.readouterr().out) == {
        "cancellation": {"state": "unknown"},
        "fixture_verdict": "passed",
        "state": "cancel_requested",
    }


@pytest.mark.parametrize(
    ("argv", "expected_verdict"),
    [([], "skipped"), (["--run"], "failed")],
)
def test_cli_classifies_nonexecuted_fixture_outcomes_without_subprocesses(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    runner_module,
    argv: list[str],
    expected_verdict: str,
) -> None:
    monkeypatch.setattr(sys, "argv", ["runner", *argv])

    assert runner_module.main() == 1

    result = json.loads(capsys.readouterr().out)
    assert result["fixture_verdict"] == expected_verdict
    assert result["state"] == "not_started"


def test_cli_classifies_unavailable_fixture_without_subprocesses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], runner_module
) -> None:
    monkeypatch.setattr(
        runner_module,
        "_main",
        lambda _args: (_ for _ in ()).throw(runner_module.DispatchE2EUnavailable("image absent")),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "runner",
            "--run",
            "--kubeconfig",
            "/tmp/fixture-kubeconfig",
            "--image",
            "docker.io/library/tessera-e2e-hello@sha256:" + "a" * 64,
        ],
    )

    assert runner_module.main() == 1

    assert json.loads(capsys.readouterr().out) == {
        "fixture_verdict": "unavailable",
        "message": "image absent",
        "state": "unavailable",
    }
