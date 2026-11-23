"""Operator wiring of the image-build registry settings into the worker (ADR 0123).

Fakes only: nothing here builds an image, reads an auth file's content or touches a network.
"""

from __future__ import annotations

import builtins
import io
import json
import os
import pathlib

import pytest

from tessera_worker.cli import _poll_config, parser
from tessera_worker.grant_poller import (
    DevelopmentGrantPoller,
    GrantPollerConfig,
    load_poller_config,
)
from tessera_worker.service import (
    DevelopmentWorkerConfig,
    DevelopmentWorkerError,
    DevelopmentWorkerService,
)

IMAGE = "python@sha256:" + "a" * 64
REPOSITORIES = ("registry.example.org/team", "ghcr.io/example")


def _private(path, content="{}", mode=0o600):
    path.write_text(content)
    path.chmod(mode)
    return path


@pytest.fixture
def paths(tmp_path):
    credential = _private(tmp_path / "credential", "x")
    auth = _private(tmp_path / "auth.json", '{"auths": {}}', 0o400)
    for name in ("w", "o"):
        (tmp_path / name).mkdir(mode=0o700)
    return credential, auth


def _service_config(tmp_path, credential, **extra):
    return DevelopmentWorkerConfig(
        control_url="http://127.0.0.1:8765",
        grant_id="grant-1",
        credential_file=credential,
        workspace_root=tmp_path / "w",
        development_output_root=tmp_path / "o",
        allowed_image=IMAGE,
        **extra,
    )


def _poller_config(tmp_path, credential, **extra):
    return GrantPollerConfig(
        "http://127.0.0.1:8765",
        credential,
        tmp_path / "pw",
        tmp_path / "po",
        (IMAGE,),
        **extra,
    )


# -- refused by default ------------------------------------------------------------------------


def test_nothing_is_configured_by_default(tmp_path, paths):
    credential, _ = paths
    config = _service_config(tmp_path, credential)
    assert config.registry_auth_file is None and config.build_repositories == ()
    assert _poller_config(tmp_path, credential).build_repositories == ()
    assert DevelopmentWorkerService(config, channel=object()).registry_auth_file is None


@pytest.mark.parametrize("make", [_service_config, _poller_config])
def test_auth_file_and_repositories_come_together_or_not_at_all(tmp_path, paths, make):
    credential, auth = paths
    with pytest.raises(DevelopmentWorkerError):
        make(tmp_path, credential, registry_auth_file=auth)
    with pytest.raises(DevelopmentWorkerError):
        make(tmp_path, credential, build_repositories=REPOSITORIES)
    make(tmp_path, credential, registry_auth_file=auth, build_repositories=REPOSITORIES)


@pytest.mark.parametrize(
    "repositories",
    [
        ("GHCR.io/example",),  # not lowercase
        ("https://ghcr.io",),
        ("ghcr.io/a b",),
        ("ghcr.io/",),
        ("ghcr.io", "ghcr.io"),  # repeated
        ("",),
        tuple(f"ghcr.io/a{i}" for i in range(33)),
    ],
)
def test_malformed_repositories_are_refused(tmp_path, paths, repositories):
    credential, auth = paths
    with pytest.raises(DevelopmentWorkerError):
        _service_config(
            tmp_path, credential, registry_auth_file=auth, build_repositories=repositories
        )


def test_a_relative_auth_path_is_refused(tmp_path, paths):
    credential, _ = paths
    with pytest.raises(DevelopmentWorkerError):
        _service_config(
            tmp_path,
            credential,
            registry_auth_file="auth.json",
            build_repositories=REPOSITORIES,
        )


# -- the auth file must be private -------------------------------------------------------------


def _service(tmp_path, credential, auth):
    config = _service_config(
        tmp_path, credential, registry_auth_file=auth, build_repositories=REPOSITORIES
    )
    return DevelopmentWorkerService(config, channel=object())


def test_a_private_auth_file_is_accepted_and_never_read(tmp_path, paths, monkeypatch):
    credential, auth = paths
    opened = []
    auth_fds = set()
    reads = []

    def guard(real):
        def wrapper(target, *a, **k):
            if isinstance(target, int) or os.fspath(target) != str(auth):
                return real(target, *a, **k)
            opened.append(str(target))
            return real(target, *a, **k)

        return wrapper

    real_os_open = os.open

    def spy_os_open(target, *a, **k):
        fd = real_os_open(target, *a, **k)
        if os.fspath(target) == str(auth):
            opened.append(str(target))
            auth_fds.add(fd)
        return fd

    real_read = os.read

    def spy_read(fd, *a, **k):
        if fd in auth_fds:
            reads.append(fd)
        return real_read(fd, *a, **k)

    real_path_open = pathlib.Path.open

    def path_open(self, *a, **k):
        if str(self) == str(auth):
            opened.append(str(self))
        return real_path_open(self, *a, **k)

    monkeypatch.setattr(os, "open", spy_os_open)
    monkeypatch.setattr(os, "read", spy_read)
    monkeypatch.setattr(io, "open", guard(io.open))
    monkeypatch.setattr(builtins, "open", guard(builtins.open))
    monkeypatch.setattr(pathlib.Path, "open", path_open)
    service = _service(tmp_path, credential, auth)
    assert service.registry_auth_file == auth
    assert opened == [] and reads == []


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o666])
def test_a_group_or_world_accessible_auth_file_is_refused(tmp_path, paths, mode):
    credential, auth = paths
    auth.chmod(mode)
    with pytest.raises(DevelopmentWorkerError, match="private regular file"):
        _service(tmp_path, credential, auth)


def test_a_symlinked_hardlinked_or_missing_auth_file_is_refused(tmp_path, paths):
    credential, auth = paths
    link = tmp_path / "link.json"
    link.symlink_to(auth)
    with pytest.raises(DevelopmentWorkerError, match="private regular file"):
        _service(tmp_path, credential, link)
    twin = tmp_path / "twin.json"
    os.link(auth, twin)
    with pytest.raises(DevelopmentWorkerError, match="private regular file"):
        _service(tmp_path, credential, auth)
    with pytest.raises(DevelopmentWorkerError, match="unavailable"):
        _service(tmp_path, credential, tmp_path / "missing.json")


def test_a_directory_is_not_an_auth_file(tmp_path, paths):
    credential, _ = paths
    with pytest.raises(DevelopmentWorkerError, match="private regular file"):
        _service(tmp_path, credential, tmp_path)


# -- the poller config file and the command line ----------------------------------------------


def _document(tmp_path, credential, **extra):
    return {
        "schema": "tessera.worker.development-poll.v1",
        "control_url": "http://127.0.0.1:8765",
        "credential_file": str(credential),
        "workspace_root": str(tmp_path / "pw"),
        "development_output_root": str(tmp_path / "po"),
        "allowed_images": [IMAGE],
        **extra,
    }


def _write(tmp_path, document):
    return _private(tmp_path / "poll.json", json.dumps(document))


def test_the_poller_config_file_carries_the_registry_settings(tmp_path, paths):
    credential, auth = paths
    path = _write(
        tmp_path,
        _document(
            tmp_path,
            credential,
            registry_auth_file=str(auth),
            build_repositories=list(REPOSITORIES),
        ),
    )
    config = load_poller_config(path)
    assert config.registry_auth_file == auth
    assert config.build_repositories == REPOSITORIES


@pytest.mark.parametrize(
    "extra",
    [
        {"build_repositories": ["ghcr.io/example"]},  # no auth file
        {"registry_auth_file": "/x/auth.json"},  # no repositories
        {"registry_auth_file": "/x/auth.json", "build_repositories": "ghcr.io"},
        {"registry_auth_file": "/x/auth.json", "build_repositories": [1]},
        {"registry_auth_file": "", "build_repositories": ["ghcr.io"]},
    ],
)
def test_a_bad_registry_section_in_the_config_file_is_refused(tmp_path, paths, extra):
    credential, _ = paths
    path = _write(tmp_path, _document(tmp_path, credential, **extra))
    with pytest.raises(DevelopmentWorkerError, match="unavailable or invalid"):
        load_poller_config(path)


def test_the_command_line_flags_reach_both_configs(tmp_path, paths):
    credential, auth = paths
    common = [
        "--registry-auth-file",
        str(auth),
        "--build-repository",
        "registry.example.org/team",
        "--build-repository",
        "ghcr.io/example",
    ]
    poll = parser().parse_args(
        [
            "development-poll",
            "--control-url",
            "http://127.0.0.1:8765",
            "--credential-file",
            str(credential),
            "--workspace-root",
            str(tmp_path / "pw"),
            "--development-output-root",
            str(tmp_path / "po"),
            "--allowed-image",
            IMAGE,
            *common,
        ]
    )
    config = _poll_config(poll)
    assert config.registry_auth_file == auth and config.build_repositories == REPOSITORIES
    run = parser().parse_args(
        [
            "development-run",
            "--control-url",
            "http://127.0.0.1:8765",
            "--grant-id",
            "g",
            "--credential-file",
            str(credential),
            "--workspace-root",
            str(tmp_path / "w"),
            "--development-output-root",
            str(tmp_path / "o"),
            "--allowed-image",
            IMAGE,
            *common,
        ]
    )
    assert run.registry_auth_file == auth and run.build_repository == list(REPOSITORIES)
    bare = parser().parse_args(
        ["development-run", "--control-url", "u", "--grant-id", "g", "--credential-file", "c"]
        + ["--workspace-root", "w", "--development-output-root", "o", "--allowed-image", IMAGE]
    )
    assert bare.registry_auth_file is None and bare.build_repository is None


# -- the poller passes them on, and the runtime enforces them ----------------------------------


def test_the_poller_hands_the_settings_to_each_grants_service(tmp_path, paths):
    credential, auth = paths
    seen = []

    class Service:
        def run(self):
            return {"status": "done"}

    def factory(config):
        seen.append(config)
        return Service()

    config = _poller_config(
        tmp_path, credential, registry_auth_file=auth, build_repositories=REPOSITORIES
    )
    poller = DevelopmentGrantPoller(config, channel=object(), service_factory=factory)
    poller._run_one("grant-1", IMAGE)
    assert seen[0].registry_auth_file == auth
    assert seen[0].build_repositories == REPOSITORIES


def test_the_poller_refuses_an_unsafe_auth_file_at_startup(tmp_path, paths):
    credential, auth = paths
    auth.chmod(0o644)
    config = _poller_config(
        tmp_path, credential, registry_auth_file=auth, build_repositories=REPOSITORIES
    )
    with pytest.raises(DevelopmentWorkerError, match="private regular file"):
        DevelopmentGrantPoller(config, channel=object())


def test_the_service_gives_the_runtime_the_auth_file_and_repositories(tmp_path, paths, monkeypatch):
    """Both runtime constructions (a new lease and a recovery) receive the same settings."""
    import tessera_worker.service as service_module

    credential, auth = paths
    captured = []

    class Stop(Exception):
        pass

    class Runtime:
        def __init__(self, *args, **kwargs):
            captured.append(kwargs)
            raise Stop

    monkeypatch.setattr(service_module, "DevelopmentCpuRuntime", Runtime)
    service = _service(tmp_path, credential, auth)
    service._heartbeat = lambda: None
    service._lease_identity = lambda lease: ("lease-1", 1, "worker-1")
    service._watch = type("W", (), {"post": lambda *a, **k: {}})()
    service.adapter = object()
    with pytest.raises(Stop):
        service.run()
    assert captured[0]["registry_auth_file"] == auth
    assert captured[0]["build_repositories"] == REPOSITORIES


def test_a_recovered_runtime_gets_the_same_validated_auth_file(tmp_path, paths, monkeypatch):
    from tessera_worker import service as service_module

    credential, auth = paths
    seen = []

    class _Runtime:
        def __init__(self, *args, **keywords):
            seen.append(keywords.get("registry_auth_file"))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def recover(self, lease_id, epoch):
            return {}

        def start(self, lease_id, epoch):
            return {}

    monkeypatch.setattr(service_module, "DevelopmentCpuRuntime", _Runtime)
    monkeypatch.setattr(
        service_module.DevelopmentWorkerService, "_recovery_identity", lambda *_: ("l", 1, "w")
    )
    monkeypatch.setattr(
        service_module.DevelopmentWorkerService, "_attach_stage_watch", lambda *_: None
    )
    monkeypatch.setattr(service_module.DevelopmentWorkerService, "_drive", lambda *_: {})
    monkeypatch.setattr(service_module.DevelopmentWorkerService, "_heartbeat", lambda *_: None)
    monkeypatch.setattr(
        service_module.DevelopmentWorkerService,
        "_lease_identity",
        lambda *_: ("l", 1, "w"),
    )
    fresh_channel = type("C", (), {"post": lambda *a, **k: {}})()
    config = _service_config(
        tmp_path, credential, registry_auth_file=auth, build_repositories=REPOSITORIES
    )
    adapter = object()
    recovering = DevelopmentWorkerService(config, channel=fresh_channel, adapter=adapter)
    recovering._run()
    monkeypatch.setattr(
        service_module.DevelopmentWorkerService, "_recovery_identity", lambda *_: None
    )
    fresh = DevelopmentWorkerService(config, channel=fresh_channel, adapter=adapter)
    fresh._run()
    assert seen == [auth, auth]
    # A loose-mode file never reaches a runtime, recovered or fresh.
    auth.chmod(0o644)
    seen.clear()
    monkeypatch.setattr(
        service_module.DevelopmentWorkerService, "_recovery_identity", lambda *_: ("l", 1, "w")
    )
    with pytest.raises(DevelopmentWorkerError, match="private regular file"):
        DevelopmentWorkerService(config, channel=fresh_channel, adapter=adapter)
    assert seen == []


def test_the_recovery_runtime_gets_the_auth_file_and_repositories_before_any_lease(
    tmp_path, paths, monkeypatch
):
    import tessera_worker.service as service_module

    credential, auth = paths
    captured = []

    class Stop(Exception):
        pass

    class Runtime:
        def __init__(self, *args, **kwargs):
            captured.append(kwargs)
            raise Stop

    monkeypatch.setattr(service_module, "DevelopmentCpuRuntime", Runtime)
    service = _service(tmp_path, credential, auth)
    service._recovery_identity = lambda runtime_dir: ("lease-1", 1, "worker-1")
    service._heartbeat = lambda: pytest.fail("recovery must not heartbeat or lease first")
    service._watch = type("W", (), {"post": lambda *a, **k: pytest.fail("no lease request")})()
    service.adapter = object()
    with pytest.raises(Stop):
        service.run()
    assert captured[0]["registry_auth_file"] == auth
    assert captured[0]["build_repositories"] == REPOSITORIES
    assert captured[0]["worker_id"] == "worker-1"
