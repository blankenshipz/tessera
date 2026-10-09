"""Unit boundaries for the opt-in authenticated Kubernetes live runner."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from tessera_control.models import ReviseTaskPlan


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


def test_runner_uses_the_exact_normal_kubernetes_plan_contract(runner_module) -> None:
    task = {"revision": 3, "title": "Exact", "purpose": "Normal API plan"}

    plan = runner_module._task_plan(task, "a" * 64, "success", 30, 90)
    assert ReviseTaskPlan.model_validate(plan).model_dump(mode="json") == plan
    assert plan == {
        "expected_revision": 3,
        "title": "Exact",
        "purpose": "Normal API plan",
        "budget_cents": 0,
        "max_duration_seconds": 90,
        "execution_spec": {
            "argv": ["/hello", "--write", "/outputs/result.txt"],
            "input_snapshot_digest": "a" * 64,
            "pinned_skill_digests": [],
            "output_paths": ["result.txt"],
            "profile": "kubernetes-opencode",
            "docker": None,
            "network_enabled": False,
            "max_output_bytes": 1024,
        },
    }


def test_legacy_runner_request_normalizes_without_changing_its_input(runner_module):
    task = {"revision": 3, "title": "Exact", "purpose": "Normal API plan"}
    current = runner_module._task_plan(task, "a" * 64, "success", 30, 90)
    legacy = current | {"execution_spec": dict(current["execution_spec"])}
    legacy["execution_spec"].pop("docker")
    assert ReviseTaskPlan.model_validate(legacy).model_dump(mode="json") == current
    assert "docker" not in legacy["execution_spec"]


@pytest.mark.parametrize("change", ["docker_on_kubernetes", "docker_without_pins", "extra"])
def test_runner_native_plan_keeps_closed_profile_fences(runner_module, change):
    task = {"revision": 3, "title": "Exact", "purpose": "Normal API plan"}
    plan = runner_module._task_plan(task, "a" * 64, "success", 30, 90)
    if change == "docker_on_kubernetes":
        plan["execution_spec"]["docker"] = {
            "target_id": "target",
            "target_digest": "b" * 64,
            "cpu_millicores": 100,
            "memory_bytes": 67108864,
            "pids_limit": 16,
            "scratch_bytes": 1048576,
        }
    elif change == "docker_without_pins":
        plan["execution_spec"]["profile"] = "ordinary-docker"
    else:
        plan["execution_spec"]["unapproved_target"] = "target"
    with pytest.raises(ValidationError):
        ReviseTaskPlan.model_validate(plan)


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


def test_run_returns_completed_process_for_all_successful_local_commands(
    monkeypatch: pytest.MonkeyPatch, runner_module
) -> None:
    expected = subprocess.CompletedProcess(("fixture",), 0, "fixture output\n", "")
    monkeypatch.setattr(runner_module.subprocess, "run", lambda *_args, **_kwargs: expected)

    result = runner_module._run(("fixture",))

    assert result is expected
    assert result.stdout.strip() == "fixture output"


def test_cleanup_deletes_only_exact_labeled_fixture_namespace(runner_module) -> None:
    labels = runner_module._fixture_labels("a" * 32)
    namespace = "tessera-e2e-cleanup-test"
    namespace_resource = {
        "kind": "Namespace",
        "metadata": {
            "name": namespace,
            "namespace": None,
            "uid": "namespace-uid",
            "labels": labels,
        },
    }

    class Live:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def _maybe_get(self, *_args):
            return namespace_resource

        def delete_namespace_uid_precondition(self, name, uid):
            self.calls.append(("delete_uid_precondition", name, uid))

        def _kubectl(self, *args, **kwargs):
            self.calls.append((*args, kwargs["namespace"]))
            if args[0] == "get":
                return subprocess.CompletedProcess(args, 0, json.dumps({"items": []}), "")
            return subprocess.CompletedProcess(args, 0, "namespace deleted\n", "")

    live = Live()
    assert runner_module._cleanup_fixture(live, namespace, "namespace-uid", labels) == {
        "status": "deletion_requested",
        "namespace_uid": "namespace-uid",
    }
    assert live.calls[-1] == ("delete_uid_precondition", namespace, "namespace-uid")


def test_cluster_rejects_existing_namespace_before_create(runner_module) -> None:
    class Live:
        def _maybe_get(self, *_args):
            return {"kind": "Namespace"}

        def _create(self, *_args):
            raise AssertionError("must not create in an existing namespace")

    with pytest.raises(runner_module.DispatchE2EError, match="existing fixture namespace"):
        runner_module._cluster(
            Live(), "tessera-e2e-existing", object(), runner_module._fixture_labels("a" * 32)
        )


@pytest.mark.parametrize(
    ("namespace_uid", "items", "reason"),
    [
        ("replacement-uid", [], "namespace_uid_mismatch"),
        (
            "namespace-uid",
            [{"metadata": {"labels": {"app.kubernetes.io/managed-by": "tessera"}}}],
            "unowned_resource_present",
        ),
    ],
)
def test_cleanup_refuses_uid_replacement_or_unowned_resource(
    runner_module, namespace_uid: str, items: list[dict[str, object]], reason: str
) -> None:
    labels = runner_module._fixture_labels("b" * 32)
    namespace = "tessera-e2e-cleanup-refusal"

    class Live:
        deleted = False

        def _maybe_get(self, *_args):
            return {
                "kind": "Namespace",
                "metadata": {
                    "name": namespace,
                    "namespace": None,
                    "uid": namespace_uid,
                    "labels": labels,
                },
            }

        def _kubectl(self, *args, **_kwargs):
            if args[0] == "delete":
                self.deleted = True
            return subprocess.CompletedProcess(args, 0, json.dumps({"items": items}), "")

    live = Live()
    assert runner_module._cleanup_fixture(live, namespace, "namespace-uid", labels) == {
        "status": "refused",
        "reason": reason,
    }
    assert not live.deleted


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
