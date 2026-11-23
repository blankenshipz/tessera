"""An image-build stage (ADR 0123): the provider's derived execution spec and the receipt."""

from __future__ import annotations

import dataclasses
import sys
import types

import pytest

from tessera_control import campaign_provider_worker as worker
from tessera_control.campaign_models import (
    BUILD_RUNTIME,
    DOCKER_CPU_RUNTIME,
    NormalInput,
    NormalStage,
    RetryPolicy,
    build_policy,
)
from tessera_control.campaign_receipts import image_build_entry
from tessera_control.runtime_packages import ImageBuildPlan
from tessera_control.store import canonical_digest

BUILDER = "registry.example.org/builder@sha256:" + "b" * 64
BASE = "registry.example.org/base@sha256:" + "a" * 64
CONTEXT_DIGEST = "c" * 64
REPOSITORY = "ghcr.io/example/app"
PLAN = {
    "context_input": "ctx",
    "dockerfile": "Dockerfile",
    "base_images": [BASE],
    "repository": REPOSITORY,
    "push": True,
    "max_transfer_bytes": 1024**3,
}


@pytest.fixture(autouse=True)
def builder_modules(monkeypatch):
    """Stand-ins for the worker's builder and the control report reader until they are real."""
    if "tessera_worker.image_build" not in sys.modules:
        try:
            import tessera_worker.image_build  # noqa: F401
        except ImportError:
            stub = types.ModuleType("tessera_worker.image_build")
            stub.builder_argv = lambda policy: ["tessera-image-build"]
            monkeypatch.setitem(sys.modules, "tessera_worker.image_build", stub)
    try:
        import tessera_control.image_build_report  # noqa: F401
    except ImportError:
        report = types.ModuleType("tessera_control.image_build_report")
        report.image_build_for_operation = lambda connection, operation_id: None
        monkeypatch.setitem(sys.modules, "tessera_control.image_build_report", report)


def _stage(**changes) -> NormalStage:
    stage = NormalStage(
        key="build",
        provider="local-docker",
        runtime=BUILD_RUNTIME,
        provider_config={},
        image=BUILDER,
        argv=("tessera-image-build",),
        env=None,
        egress=None,
        max_duration_seconds=600,
        max_exposure_cents=0,
        grant_ttl_seconds=3600,
        retry=RetryPolicy(),
        inputs=(
            NormalInput(
                kind="snapshot",
                name="ctx",
                snapshot_id="snap",
                digest=CONTEXT_DIGEST,
                data_class="public",
            ),
        ),
        outputs=(),
        metrics=(),
        stop_when=(),
        max_output_bytes=1024,
        build=dict(PLAN),
    )
    return dataclasses.replace(stage, **changes)


def _plain_stage() -> NormalStage:
    return _stage(runtime=DOCKER_CPU_RUNTIME, build=None, argv=("python", "-c", "print(1)"))


def _provider():
    return worker.WorkerGrantProvider(
        worker._descriptor("local-docker", "worker", build_repositories=("ghcr.io/example",))
    )


def test_build_stage_execution_spec_is_derived_and_digest_bound():
    stage = _stage()
    policy = build_policy(stage)
    spec = _provider().execution_spec(stage, "d" * 64)
    import tessera_worker.image_build as image_build

    assert spec["argv"] == list(image_build.builder_argv(policy))
    assert spec["profile"] == "local-cpu"
    assert spec["egress"]["profile"] == "allowlist-proxy-v1"
    assert spec["egress"]["max_egress_bytes"] == PLAN["max_transfer_bytes"]
    destinations = spec["egress"]["destinations"]
    assert [d["host"] for d in destinations] == policy["hosts"]
    assert {d["port"] for d in destinations} == {443}
    assert set(policy["hosts"]) == {"ghcr.io", "registry.example.org"}
    assert spec["build"] == ImageBuildPlan.model_validate(policy).model_dump(mode="json")
    assert "env" not in spec
    changed = _provider().execution_spec(
        _stage(build={**PLAN, "dockerfile": "other/Dockerfile"}), "d" * 64
    )
    assert canonical_digest(changed) != canonical_digest(spec)


def test_argv_comes_from_the_builder_never_from_the_stage(monkeypatch):
    import tessera_worker.image_build as image_build

    monkeypatch.setattr(
        image_build, "builder_argv", lambda policy: ["derived", policy["repository"]]
    )
    spec = _provider().execution_spec(_stage(argv=("sh", "-c", "evil")), "d" * 64)
    assert spec["argv"] == ["derived", REPOSITORY]


@pytest.mark.parametrize(
    "changes",
    [
        {"inputs": (dataclasses.replace(_stage().inputs[0], data_class="private_derived"),)},
        {"inputs": (dataclasses.replace(_stage().inputs[0], data_class=None),)},
        {"inputs": (dataclasses.replace(_stage().inputs[0], kind="artifact"),)},
        {"inputs": _stage().inputs + (dataclasses.replace(_stage().inputs[0], name="more"),)},
        {"env": {"A": "b"}},
        {"runtime": DOCKER_CPU_RUNTIME},
    ],
)
def test_non_public_or_extra_inputs_are_refused(changes):
    with pytest.raises(ValueError):
        _provider().execution_spec(_stage(**changes), "d" * 64)


def test_build_runtime_without_a_build_plan_is_refused():
    with pytest.raises(ValueError):
        _provider().execution_spec(_stage(build=None), "d" * 64)


def test_a_stage_without_build_is_unchanged():
    spec = _provider().execution_spec(_plain_stage(), "d" * 64)
    assert "build" not in spec
    assert spec["argv"] == ["python", "-c", "print(1)"]
    assert "egress" not in spec
    # the digest equals that of a spec built without the field at all
    from tessera_control.models import ExecutionSpec

    legacy = ExecutionSpec(
        argv=["python", "-c", "print(1)"],
        input_snapshot_digest="d" * 64,
        pinned_skill_digests=[],
        output_paths=[],
        profile="local-cpu",
        max_output_bytes=1024,
    ).model_dump(mode="json")
    assert canonical_digest(legacy) == canonical_digest(spec)


def test_build_runtime_is_a_local_docker_runtime_and_opt_in_for_self_hosted():
    assert BUILD_RUNTIME in worker._descriptor("local-docker", "w").runtimes
    item = worker.SelfHostedInstance(id="box", worker_principal_id="w")
    assert item.runtimes == [DOCKER_CPU_RUNTIME]
    item = worker.SelfHostedInstance(id="box", worker_principal_id="w", runtimes=[BUILD_RUNTIME])
    assert item.runtimes == [BUILD_RUNTIME]


def test_package_carries_the_build_plan():
    from tessera_control import runtime_packages

    spec = _provider().execution_spec(_stage(), "d" * 64)
    assert runtime_packages.ImageBuildPlan.model_validate(spec["build"]).repository == REPOSITORY


# -- receipt ----------------------------------------------------------------------------

IMAGE = f"{REPOSITORY}@sha256:" + "e" * 64


def _row(**changes):
    return {
        "image": IMAGE,
        "digest": "e" * 64,
        "size_bytes": 12345,
        "pushed": True,
        **changes,
    }


def _entry(monkeypatch, row):
    import tessera_control.image_build_report as report

    monkeypatch.setattr(report, "image_build_for_operation", lambda connection, op: row)
    return image_build_entry(None, {"operation_id": "op1"}, _stage())


def test_receipt_entry_with_a_reported_row(monkeypatch):
    entry = _entry(monkeypatch, _row())
    assert entry["evidence"] == "provider_reported"
    assert entry["produced_image"] == IMAGE
    assert entry["digest"] == "sha256:" + "e" * 64
    assert (entry["size_bytes"], entry["pushed"]) == (12345, True)
    assert entry["context_digest"] == CONTEXT_DIGEST
    assert entry["builder_image"] == BUILDER
    assert entry["base_images"] == [BASE]
    assert entry["repository_host"] == "ghcr.io"
    assert entry["via_control"] is False
    assert entry["dockerfile"] == "Dockerfile"


def test_receipt_entry_without_a_reported_row_is_declared_only(monkeypatch):
    entry = _entry(monkeypatch, None)
    assert entry["evidence"] == "declared_only"
    assert entry["produced_image"] is None
    assert entry["context_digest"] == CONTEXT_DIGEST
    assert entry["repository"] == REPOSITORY


@pytest.mark.parametrize(
    "row",
    [
        _row(image="ghcr.io/attacker/app@sha256:" + "e" * 64),
        _row(image="ghcr.io/example/app-evil@sha256:" + "e" * 64),
        _row(image=REPOSITORY + "@sha256:" + "e" * 64, digest="f" * 64),
        _row(size_bytes="big"),
        _row(pushed="yes"),
        "junk",
    ],
)
def test_a_worker_reported_image_outside_the_spec_is_not_accepted(monkeypatch, row):
    entry = _entry(monkeypatch, row)
    assert entry["evidence"] == "declared_only"
    assert entry["produced_image"] is None
    assert entry["digest"] is None


def test_a_stage_without_build_has_no_build_entry():
    assert image_build_entry(None, {"operation_id": "op1"}, _plain_stage()) is None


def test_repository_registration_matches_whole_path_segments():
    from tessera_control.image_build_models import repository_registered as registered

    assert registered("ghcr.io/example/app", ["ghcr.io/example"])
    assert registered("ghcr.io/example/app", ["ghcr.io"])
    assert registered("ghcr.io/example/app", ["ghcr.io/example/app"])
    assert not registered("ghcr.io/example2/app", ["ghcr.io/example"])
    assert not registered("ghcr.io/example/app", ["ghcr.io/exam"])
    assert not registered("ghcr.io/example/app", ["ghcr.i"])
    assert not registered("ghcr.io/example/app", [])
    assert not registered("ghcr.io/example/app", None)
    assert not registered("ghcr.io/example/app", [""])


def test_the_plan_refuses_an_unregistered_repository_and_an_empty_registry():
    for repositories in ((), ("ghcr.io/other",)):
        provider = worker.WorkerGrantProvider(
            worker._descriptor("local-docker", "worker", build_repositories=repositories)
        )
        with pytest.raises(ValueError, match="build_repository_not_registered"):
            provider.execution_spec(_stage(), "d" * 64)
    unpushed = _stage(build={**PLAN, "push": False})
    provider = worker.WorkerGrantProvider(worker._descriptor("local-docker", "worker"))
    assert provider.execution_spec(unpushed, "d" * 64)["build"]["push"] is False


def test_the_operator_file_registers_build_repositories_per_instance():
    try:
        worker.register_from_config(
            {
                "local_docker": {"worker_principal_id": "w", "build_repositories": ["ghcr.io/a"]},
                "self_hosted": [
                    {
                        "id": "box",
                        "worker_principal_id": "w2",
                        "runtimes": [BUILD_RUNTIME],
                        "build_repositories": ["quay.io/b"],
                    }
                ],
            }
        )
        from tessera_control.campaign_providers import get_provider

        assert get_provider("local-docker").descriptor.build_repositories == ("ghcr.io/a",)
        assert get_provider("box").descriptor.build_repositories == ("quay.io/b",)
        for bad in ("GHCR.io", "ghcr.io/", "ghcr.io/a b", "https://ghcr.io"):
            with pytest.raises(ValueError):
                worker.register_from_config(
                    {"local_docker": {"worker_principal_id": "w", "build_repositories": [bad]}}
                )
    finally:
        worker.register_from_config(None)
