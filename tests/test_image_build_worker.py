"""ADR 0123: the builder's fixed argv, its strict result parser and the worker's report hook."""

from __future__ import annotations

import copy
import json
import os

import pytest
from test_runtime import ApiChannel
from test_sandbox_egress_control import _journal, _world
from test_supervisor import FakeAdapter

from tessera_control.image_build_report import (
    image_build_failure_for_operation,
    image_build_for_operation,
)
from tessera_control.store import _connect
from tessera_worker.cpu import CpuError, CpuOperation, DockerCpuAdapter
from tessera_worker.image_build import (
    REGISTRY_AUTH_PATH,
    ImageBuildError,
    buildah_commands,
    builder_argv,
    parse_build_result,
)
from tessera_worker.runtime import CpuRuntime
from tessera_worker.supervisor import SupervisorError

BASE = "docker.io/library/alpine@sha256:" + "b" * 64
DIGEST = "c" * 64
POLICY = {
    "builder_image": "ghcr.io/org/builder@sha256:" + "a" * 64,
    "context_input": "ctx",
    "dockerfile": "docker/Dockerfile",
    "base_images": [BASE],
    "repository": "ghcr.io/org/img",
    "registry_host": "ghcr.io",
    "hosts": ["docker.io", "ghcr.io"],
    "push": True,
    "max_transfer_bytes": 10**9,
}
EGRESS = {
    "profile": "allowlist-proxy-v1",
    "destinations": [{"host": h, "port": 443} for h in POLICY["hosts"]],
    "max_egress_bytes": POLICY["max_transfer_bytes"],
}


def result(**changes):
    body = {
        "schema": "tessera.image-build.v1",
        "image": f"ghcr.io/org/img@sha256:{DIGEST}",
        "digest": DIGEST,
        "size_bytes": 12345,
        "pushed": True,
        "base_images": [BASE],
    }
    return body | changes


def raw(**changes):
    return json.dumps(result(**changes)).encode()


# argv ---------------------------------------------------------------------------------------


def test_argv_is_fixed_and_carries_only_policy_fields():
    argv = builder_argv(POLICY)
    assert argv == builder_argv(copy.deepcopy(POLICY))
    assert argv[0] == "tessera-image-build"
    assert argv[argv.index("--dockerfile") + 1] == "/inputs/ctx/docker/Dockerfile"
    assert argv[argv.index("--context") + 1] == "/inputs/ctx"
    assert argv[argv.index("--base-image") + 1] == BASE
    assert argv[argv.index("--auth-file") + 1] == REGISTRY_AUTH_PATH
    assert "--push" in argv and "--no-push" not in argv
    # The builder image is how it is run, never an argument; nothing else leaks in.
    assert not any("builder" in a for a in argv)
    assert not any(a.startswith(("-e", "--env", "--password", "--token")) for a in argv)


def test_non_push_build_has_no_auth_file():
    argv = builder_argv(POLICY | {"push": False})
    assert "--no-push" in argv and "--auth-file" not in argv
    assert REGISTRY_AUTH_PATH not in " ".join(argv)


@pytest.mark.parametrize(
    "change",
    [
        {"dockerfile": "../etc/passwd"},
        {"dockerfile": "a/../b"},
        {"dockerfile": "/abs/Dockerfile"},
        {"dockerfile": "a b"},
        {"dockerfile": "a;rm -rf"},
        {"dockerfile": "a//b"},
        {"dockerfile": "$(x)"},
        {"context_input": "Ctx"},
        {"context_input": "../x"},
        {"repository": "ghcr.io/Org/img"},
        {"repository": "ghcr.io/org/img@sha256:" + "a" * 64},
        {"repository": "ghcr.io/org/img --push"},
        {"repository": "img"},
        {"base_images": ["alpine:3"]},
        {"base_images": [BASE, BASE]},
        {"base_images": [BASE + "x"]},
        {"base_images": []},
        {"base_images": ["x@sha256:" + "B" * 64]},
        {"push": "yes"},
        {"max_transfer_bytes": 0},
        {"max_transfer_bytes": "5"},
        {"registry_host": "evil.example"},
        {"extra": 1},
    ],
)
def test_every_bad_field_is_refused(change):
    with pytest.raises(ImageBuildError) as error:
        builder_argv(POLICY | change)
    assert error.value.code == "policy_invalid"


def test_missing_field_and_non_dict_refused():
    for bad in ({k: v for k, v in POLICY.items() if k != "repository"}, None, [], "x"):
        with pytest.raises(ImageBuildError):
            builder_argv(bad)  # type: ignore[arg-type]


def test_buildah_commands_are_rootless_pinned_and_use_a_content_tag():
    commands = buildah_commands(POLICY, "d" * 64)
    assert commands[0] == ["buildah", "pull", "--quiet", BASE]
    bud = commands[1]
    assert {"--isolation=chroot", "--layers=false", "--pull=never"} <= set(bud)
    assert "--file=/inputs/ctx/docker/Dockerfile" in bud and bud[-1] == "/inputs/ctx"
    push = commands[2]
    assert push[-1] == "docker://ghcr.io/org/img:tessera-" + "d" * 12
    assert f"--authfile={REGISTRY_AUTH_PATH}" in push
    assert len(buildah_commands(POLICY | {"push": False})) == 2


# result parser -------------------------------------------------------------------------------


def test_parse_accepts_the_closed_row():
    row = parse_build_result(raw(), POLICY)
    assert row == {
        "image": f"ghcr.io/org/img@sha256:{DIGEST}",
        "digest": DIGEST,
        "size_bytes": 12345,
        "pushed": True,
        "base_images": [BASE],
    }
    assert parse_build_result(raw(seconds=3.5), POLICY)["seconds"] == 3.5
    local = parse_build_result(raw(pushed=False), POLICY | {"push": False})
    assert local["pushed"] is False


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"schema": "other"}, "result_invalid"),
        ({"extra": 1}, "result_invalid"),
        ({"digest": "C" * 64}, "result_invalid"),
        ({"digest": "c" * 63}, "result_invalid"),
        ({"size_bytes": 0}, "result_invalid"),
        ({"size_bytes": True}, "result_invalid"),
        ({"size_bytes": "5"}, "result_invalid"),
        ({"seconds": -1}, "result_invalid"),
        ({"pushed": "true"}, "result_invalid"),
        ({"image": f"ghcr.io/org/img@sha256:{'d' * 64}"}, "result_invalid"),
        ({"image": f"ghcr.io/org/other@sha256:{DIGEST}"}, "result_mismatch"),
        ({"image": f"evil.example/org/img@sha256:{DIGEST}"}, "result_mismatch"),
        ({"image": f"ghcr.io/org/img:latest@sha256:{DIGEST}"}, "result_mismatch"),
        ({"pushed": False}, "result_mismatch"),
        ({"base_images": ["x/y@sha256:" + "e" * 64]}, "result_mismatch"),
        ({"base_images": []}, "result_invalid"),
        ({"size_bytes": 10**9 + 1}, "result_mismatch"),
    ],
)
def test_parse_refuses(changes, code):
    with pytest.raises(ImageBuildError) as error:
        parse_build_result(raw(**changes), POLICY)
    assert error.value.code == code


@pytest.mark.parametrize("body", [b"", b"nope", b"[]", b"null", b'{"schema":1}', b"\xff"])
def test_parse_refuses_malformed_documents(body):
    with pytest.raises(ImageBuildError) as error:
        parse_build_result(body, POLICY)
    assert error.value.code == "result_invalid"


def test_parse_refuses_oversized_and_missing_keys():
    with pytest.raises(ImageBuildError) as error:
        parse_build_result(b" " * (64 * 1024 + 1), POLICY)
    assert error.value.code == "result_too_large"
    partial = result()
    del partial["size_bytes"]
    with pytest.raises(ImageBuildError):
        parse_build_result(json.dumps(partial).encode(), POLICY)


# worker hook --------------------------------------------------------------------------------


def _build_world(tmp_path):
    spec = {"argv": builder_argv(POLICY), "egress": EGRESS, "build": POLICY}
    return _world(tmp_path / "w", spec)


class BuildAdapter(FakeAdapter):
    """A fake whose egress journal is the ordinary empty-ish one the proxy would report."""

    def egress_summary(self, operation):
        journal = _journal(
            [
                {
                    "host": "ghcr.io",
                    "port": 443,
                    "decision": "allow",
                    "connections": 1,
                    "bytes_up": 10,
                    "bytes_down": 90,
                }
            ]
        )
        return journal["journal_sha256"], journal["summary"]


def _runtime(tmp_path, state, worker, *, auth=True, repositories=("ghcr.io/org",)):
    inputs = tmp_path / "inputs"
    inputs.mkdir(mode=0o700, exist_ok=True)
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}")
    auth_file.chmod(0o400)
    adapter = BuildAdapter()
    runtime = CpuRuntime(
        tmp_path / "runtime",
        inputs,
        ApiChannel(state),
        adapter,
        worker_id=worker["id"],
        registry_auth_file=auth_file if auth else None,
        build_repositories=repositories,
    )
    return runtime, adapter


def _finish(runtime, adapter, lease, body: bytes | None):
    started = runtime.start(lease["id"], lease["epoch"])
    out = runtime.directory / "build-output"
    assert out.is_dir() and oct(out.stat().st_mode & 0o777) == "0o700"
    if body is not None:
        (out / "image-build.json").write_bytes(body)
    adapter.state = "exited"
    runtime.report(runtime.tick())
    return started


def _recorded(state, operation_id):
    with _connect(state / "tessera.sqlite3") as connection:
        return (
            image_build_for_operation(connection, operation_id),
            image_build_failure_for_operation(connection, operation_id),
        )


def test_package_carries_build_and_operation_gets_custody(tmp_path):
    state, _, worker, _, _, lease, op = _build_world(tmp_path)
    assert op["package"]["build"] == POLICY
    runtime, adapter = _runtime(tmp_path, state, worker)
    with runtime:
        runtime.start(lease["id"], lease["epoch"])
        operation = runtime.operation
        assert operation.build_directory == str(runtime.directory / "build-output")
        assert operation.registry_auth_file == str(tmp_path / "auth.json")
        assert operation.network_profile == "allowlist-proxy-v1"


def test_success_reports_the_row_once(tmp_path):
    state, _, worker, _, _, lease, op = _build_world(tmp_path)
    runtime, adapter = _runtime(tmp_path, state, worker)
    with runtime:
        _finish(runtime, adapter, lease, raw())
        operation_id = runtime.operation.operation_id
        runtime.report(runtime.tick())  # replay: already delivered, no second post
    event, failure = _recorded(state, operation_id)
    assert failure is None
    assert event["image"] == f"ghcr.io/org/img@sha256:{DIGEST}" and event["pushed"] is True
    assert event["via_control"] is False


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (None, "result_missing"),
        (b"{not json", "result_invalid"),
        (raw(digest="d" * 64), "result_invalid"),
        (
            raw(image=f"ghcr.io/org/img@sha256:{'d' * 64}", digest="d" * 64, pushed=False),
            "result_mismatch",
        ),
        (b" " * (64 * 1024 + 1), "result_too_large"),
    ],
)
def test_missing_or_invalid_result_reports_a_failure_never_success(tmp_path, body, code):
    state, _, worker, _, _, lease, _ = _build_world(tmp_path)
    runtime, adapter = _runtime(tmp_path, state, worker)
    with runtime:
        _finish(runtime, adapter, lease, body)
        operation_id = runtime.operation.operation_id
    event, failure = _recorded(state, operation_id)
    assert event is None and failure["failed"] == code


def test_symlinked_result_is_unsafe(tmp_path):
    state, _, worker, _, _, lease, _ = _build_world(tmp_path)
    runtime, adapter = _runtime(tmp_path, state, worker)
    with runtime:
        runtime.start(lease["id"], lease["epoch"])
        target = tmp_path / "elsewhere.json"
        target.write_bytes(raw())
        os.symlink(target, runtime.directory / "build-output" / "image-build.json")
        adapter.state = "exited"
        runtime.report(runtime.tick())
        operation_id = runtime.operation.operation_id
    assert _recorded(state, operation_id)[1]["failed"] == "result_unsafe"


def test_nonzero_exit_reports_failure_even_with_a_result(tmp_path):
    state, _, worker, _, _, lease, _ = _build_world(tmp_path)
    runtime, adapter = _runtime(tmp_path, state, worker)
    with runtime:
        runtime.start(lease["id"], lease["epoch"])
        (runtime.directory / "build-output" / "image-build.json").write_bytes(raw())
        runtime._report_image_build(2)
        operation_id = runtime.operation.operation_id
    assert _recorded(state, operation_id)[1]["failed"] == "build_exit_nonzero"


def test_pushing_build_without_registry_auth_is_refused_before_anything_starts(tmp_path):
    state, _, worker, _, _, lease, _ = _build_world(tmp_path)
    runtime, adapter = _runtime(tmp_path, state, worker, auth=False)
    with runtime, pytest.raises(SupervisorError, match="no registry auth configured"):
        runtime.start(lease["id"], lease["epoch"])
    assert "start" not in adapter.calls and "prepare" not in adapter.calls


@pytest.mark.parametrize("repositories", [(), ("ghcr.io/other",), ("ghcr.io/or",), ("quay.io",)])
def test_pushing_to_an_unregistered_repository_is_refused_before_auth_is_mounted(
    tmp_path, repositories
):
    state, _, worker, _, _, lease, _ = _build_world(tmp_path)
    runtime, adapter = _runtime(tmp_path, state, worker, repositories=repositories)
    with runtime, pytest.raises(SupervisorError, match="not registered on this worker"):
        runtime.start(lease["id"], lease["epoch"])
    assert "start" not in adapter.calls and "prepare" not in adapter.calls
    assert runtime.operation is None


@pytest.mark.parametrize("registered", ["ghcr.io", "ghcr.io/org", "ghcr.io/org/img"])
def test_a_registered_host_or_prefix_admits_the_push(tmp_path, registered):
    state, _, worker, _, _, lease, _ = _build_world(tmp_path)
    runtime, _ = _runtime(tmp_path, state, worker, repositories=(registered,))
    with runtime:
        runtime.start(lease["id"], lease["epoch"])
        assert runtime.operation.registry_auth_file is not None


def test_a_leftover_result_with_a_clean_exit_and_no_new_file_is_result_missing(tmp_path):
    state, _, worker, _, _, lease, _ = _build_world(tmp_path)
    runtime, adapter = _runtime(tmp_path, state, worker)
    stale = runtime.directory / "build-output"
    stale.mkdir(mode=0o700)
    (stale / "image-build.json").write_bytes(raw())  # an earlier operation's valid result
    with runtime:
        _finish(runtime, adapter, lease, None)  # exits 0, writes nothing new
        operation_id = runtime.operation.operation_id
    event, failure = _recorded(state, operation_id)
    assert event is None and failure["failed"] == "result_missing"


def test_non_pushing_build_needs_no_auth(tmp_path):
    policy = POLICY | {"push": False}
    spec = {"argv": builder_argv(policy), "egress": EGRESS, "build": policy}
    state, _, worker, _, _, lease, _ = _world(tmp_path / "w", spec)
    runtime, adapter = _runtime(tmp_path, state, worker, auth=False)
    with runtime:
        runtime.start(lease["id"], lease["epoch"])
        assert runtime.operation.registry_auth_file is None


def test_package_whose_argv_is_not_derived_from_the_policy_is_refused(tmp_path):
    spec = {"argv": ["sh", "-c", "id"], "egress": EGRESS, "build": POLICY}
    state, _, worker, _, _, lease, _ = _world(tmp_path / "w", spec)
    runtime, adapter = _runtime(tmp_path, state, worker)
    with runtime, pytest.raises(SupervisorError):
        runtime.start(lease["id"], lease["epoch"])
    assert "prepare" not in adapter.calls


# adapter mounts -----------------------------------------------------------------------------


def _operation(tmp_path, **extra):
    out = tmp_path / "out"
    out.mkdir(mode=0o700, exist_ok=True)
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    auth.chmod(0o400)
    return (
        CpuOperation(
            operation_id="a" * 32,
            image="img@sha256:" + "a" * 64,
            argv=tuple(builder_argv(POLICY)),
            plan_digest="b" * 64,
            max_duration_seconds=60,
            network_profile="allowlist-proxy-v1",
            egress=EGRESS,
            build_directory=str(out),
            registry_auth_file=str(auth),
            **extra,
        ),
        out,
        auth,
    )


def test_hardened_container_mounts_only_outputs_and_the_auth_file(tmp_path):
    operation, out, auth = _operation(tmp_path)
    adapter = DockerCpuAdapter(
        frozenset({operation.image}), egress_proxy_image="proxy@sha256:" + "d" * 64
    )
    arguments = adapter.create_arguments(operation)
    assert f"type=bind,src={out},dst=/outputs" in arguments
    assert f"type=bind,src={auth},dst={REGISTRY_AUTH_PATH},readonly" in arguments
    for flag in ("--read-only", "--cap-drop", "ALL", "no-new-privileges=true"):
        assert flag in arguments
    joined = " ".join(arguments)
    assert "docker.sock" not in joined and "--privileged" not in arguments
    assert operation.argv.count(REGISTRY_AUTH_PATH) == 1


def test_world_writable_auth_or_loose_output_refused(tmp_path):
    operation, out, auth = _operation(tmp_path)
    adapter = DockerCpuAdapter(
        frozenset({operation.image}), egress_proxy_image="proxy@sha256:" + "d" * 64
    )
    auth.chmod(0o666)
    with pytest.raises(CpuError):
        adapter.create_arguments(operation)
    auth.chmod(0o400)
    out.chmod(0o755)
    with pytest.raises(CpuError):
        adapter.create_arguments(operation)


def test_auth_without_build_directory_is_not_representable(tmp_path):
    with pytest.raises(ValueError):
        CpuOperation(
            operation_id="a" * 32,
            image="img@sha256:" + "a" * 64,
            argv=("x",),
            plan_digest="b" * 64,
            max_duration_seconds=60,
            registry_auth_file=str(tmp_path / "auth.json"),
        )
