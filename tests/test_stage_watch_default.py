"""The stage watcher is on by default for every local Docker stage (ADR 0125). Fakes only."""

from __future__ import annotations

import os
import socket
import sys
from types import SimpleNamespace

import pytest
from test_development_worker_service import _service, _unleased_delivery
from test_worker_poll_loop import (
    IMAGE,
    ListChannel,
    credential,
    restore_signals,  # noqa: F401  (autouse fixture)
    write_config,
)

from tessera_worker import cli, grant_poller
from tessera_worker.grant_poller import (
    DevelopmentGrantPoller,
    GrantPollerConfig,
    load_poller_config,
)
from tessera_worker.service import DevelopmentWorkerError
from tessera_worker.stage_watch_config import (
    DEFAULT_KILL_SWITCH_NAME,
    StageWatchConfig,
    default_stage_watch,
    resolve_stage_watch,
)


def _private(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _poller_config(tmp_path, **over):
    return GrantPollerConfig(
        control_url="http://127.0.0.1:8765",
        credential_file=credential(tmp_path),
        workspace_root=tmp_path / "ws",
        development_output_root=tmp_path / "out",
        allowed_images=(IMAGE,),
        poll_seconds=0.01,
        **over,
    )


class RecordingService:
    built: list = []

    def __init__(self, config, *, channel=None, stage_watch=None):
        self.config, self.channel, self.stage_watch = config, channel, stage_watch
        RecordingService.built.append(self)

    def run(self):
        return {"state": "collected"}


@pytest.fixture
def recording(monkeypatch):
    RecordingService.built = []
    monkeypatch.setattr(grant_poller, "DevelopmentWorkerService", RecordingService)
    return RecordingService.built


def _cycle(config, grants=1):
    poller = DevelopmentGrantPoller(
        config,
        channel=ListChannel([{"id": f"g{n}", "pinned_image": IMAGE} for n in range(grants)]),
        sleep=lambda _: None,
    )
    poller.step()
    return poller


# --- TASK 1: regression. A config file with a stage_watcher block made the whole poller exit 2.


def test_config_file_with_a_stage_watcher_block_is_accepted_by_the_poller(
    tmp_path, monkeypatch, capsys
):
    marker = tmp_path / "ws" / "stage-watch.kill"
    path = write_config(tmp_path, stage_watcher={"enabled": True, "kill_switch_path": str(marker)})
    seen = []

    def factory(config):
        poller = DevelopmentGrantPoller(config, channel=ListChannel([]), sleep=lambda _: None)
        seen.append(poller)
        return poller

    monkeypatch.setattr(cli, "DevelopmentGrantPoller", factory)
    monkeypatch.setattr(
        sys, "argv", ["tessera-worker", "development-poll", "--config", str(path), "--once"]
    )
    assert cli.main() == 0
    assert "poller_setup_unavailable" not in capsys.readouterr().out
    assert seen[0].stage_watch.startable and seen[0].stage_watch.kill_switch_path == str(marker)


# --- StageWatchConfig defaults and the closed mapping


def test_default_is_enabled_startable_with_a_marker_in_the_private_root(tmp_path):
    root = _private(tmp_path / "state")
    config, reason = default_stage_watch(root)
    assert reason is None and config.enabled and config.startable
    assert config.kill_switch_path == str(root / DEFAULT_KILL_SWITCH_NAME)
    assert not (root / DEFAULT_KILL_SWITCH_NAME).exists()  # never auto-created


def test_explicit_config_wins_including_a_disabled_one(tmp_path):
    root = _private(tmp_path / "state")
    off = StageWatchConfig()
    assert resolve_stage_watch(off, root) is off
    assert resolve_stage_watch(None, root).startable


def test_unusable_default_directory_means_no_watcher_and_a_stderr_line(tmp_path, capsys):
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    loose.chmod(0o755)
    config = resolve_stage_watch(None, loose)
    assert not config.enabled and not config.startable
    err = capsys.readouterr().err
    assert "stage watcher" in err and "no watcher will start" in err and str(loose) not in err
    assert not resolve_stage_watch(None, tmp_path / "missing").enabled


def test_default_marker_in_an_unusable_directory_does_not_stop_the_worker(tmp_path, capsys):
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    service = _service(tmp_path, state, grant)
    service.config.workspace_root.chmod(0o755)
    config = resolve_stage_watch(None, service.config.workspace_root)
    assert not config.enabled
    assert "no watcher will start" in capsys.readouterr().err


def test_from_mapping_is_closed_and_validated():
    assert StageWatchConfig.from_mapping({"enabled": False}) == StageWatchConfig()
    full = {
        "enabled": True,
        "kill_switch_path": "/var/lib/tessera/stage-watch.kill",
        "silent_seconds": 300,
        "heartbeat_seconds": 30,
        "max_reports": 50,
        "delivery_timeout_seconds": 1.5,
        "stats": True,
        "tick_budget_seconds": 0.5,
        "tick_max_posts": 2,
    }
    assert StageWatchConfig.from_mapping(full).startable
    for bad in (
        {"unknown": 1},
        {"enabled": "yes"},
        {"kill_switch_path": "relative/marker"},
        {"silent_seconds": 0},
        {"tick_budget_seconds": 60},
        [],
        "on",
    ):
        with pytest.raises(ValueError):
            StageWatchConfig.from_mapping(bad)


# --- poller configuration file


def test_poller_config_without_the_block_leaves_the_default(tmp_path):
    assert load_poller_config(write_config(tmp_path)).stage_watch is None


def test_poller_config_with_a_valid_block(tmp_path):
    marker = str(tmp_path / "k")
    config = load_poller_config(
        write_config(
            tmp_path,
            stage_watcher={"enabled": True, "kill_switch_path": marker, "silent_seconds": 120},
        )
    )
    assert (
        config.stage_watch.kill_switch_path == marker and config.stage_watch.silent_seconds == 120
    )
    off = load_poller_config(write_config(tmp_path, stage_watcher={"enabled": False}))
    assert off.stage_watch is not None and not off.stage_watch.enabled


@pytest.mark.parametrize(
    "block",
    [
        {"enabled": True, "unknown_key": 1},
        {"enabled": True, "kill_switch_path": "relative"},
        {"enabled": 1},
        "on",
        [],
    ],
)
def test_poller_config_refuses_a_bad_block_with_the_bounded_message(tmp_path, block):
    with pytest.raises(DevelopmentWorkerError) as refusal:
        load_poller_config(write_config(tmp_path, stage_watcher=block))
    assert str(refusal.value) == "Grant poller config is unavailable or invalid."


# --- poll cycle: services carry stage_watch


def test_default_poll_cycle_builds_services_with_the_default_marker(tmp_path, recording):
    poller = _cycle(_poller_config(tmp_path), grants=2)
    marker = str(poller.root / DEFAULT_KILL_SWITCH_NAME)
    assert len(recording) == 2
    assert all(
        s.stage_watch.startable and s.stage_watch.kill_switch_path == marker for s in recording
    )
    assert all(s.channel is poller.channel for s in recording)  # no second channel
    assert not os.path.exists(marker)


def test_explicit_poller_setting_reaches_every_service(tmp_path, recording):
    explicit = StageWatchConfig(enabled=True, kill_switch_path=str(tmp_path / "mine"), stats=True)
    _cycle(_poller_config(tmp_path, stage_watch=explicit), grants=2)
    assert [s.stage_watch for s in recording] == [explicit, explicit]


def test_opt_out_in_config_reaches_the_service_as_disabled(tmp_path, recording):
    config = load_poller_config(write_config(tmp_path, stage_watcher={"enabled": False}))
    _cycle(config)
    assert recording[0].stage_watch.enabled is False


def test_cli_flags_win_over_the_config_block_and_opt_out_is_explicit(
    tmp_path, monkeypatch, recording
):
    path = write_config(
        tmp_path, stage_watcher={"enabled": True, "kill_switch_path": str(tmp_path / "file")}
    )
    built = []

    def factory(config):
        built.append(config)
        return DevelopmentGrantPoller(config, channel=ListChannel([]), sleep=lambda _: None)

    monkeypatch.setattr(cli, "DevelopmentGrantPoller", factory)
    base = ["tessera-worker", "development-poll", "--config", str(path), "--once"]
    for extra in (
        ["--stage-watcher", "--stage-watcher-kill-switch", str(tmp_path / "flag")],
        ["--no-stage-watcher"],
        [],
    ):
        monkeypatch.setattr(sys, "argv", base + extra)
        assert cli.main() == 0
    assert built[0].stage_watch.kill_switch_path == str(tmp_path / "flag")
    assert built[1].stage_watch.enabled is False
    assert built[2].stage_watch.kill_switch_path == str(tmp_path / "file")


def test_cli_with_no_setting_leaves_the_default_and_conflict_is_refused(
    tmp_path, monkeypatch, capsys
):
    arguments = cli.parser().parse_args(
        ["development-poll", "--config", "x", "--stage-watcher", "--no-stage-watcher"]
    )
    with pytest.raises(ValueError):
        cli._stage_watch(arguments)
    assert cli._stage_watch(cli.parser().parse_args(["development-poll", "--config", "x"])) is None
    off = cli._stage_watch(
        cli.parser().parse_args(["development-poll", "--config", "x", "--no-stage-watcher"])
    )
    assert off is not None and not off.enabled


# --- the supervisor hook


def _attach(service, adapter):
    runtime = SimpleNamespace(supervisor=SimpleNamespace(watch_runner=None))
    service._attach_stage_watch(runtime, adapter)
    return runtime.supervisor.watch_runner


def _docker_adapter():
    return SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr=""))


def test_default_service_gives_the_supervisor_a_watch_runner(tmp_path):
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    service = _service(tmp_path, state, grant)
    assert service.stage_watch.startable
    assert service.stage_watch.kill_switch_path == str(service.root / DEFAULT_KILL_SWITCH_NAME)
    runner = _attach(service, _docker_adapter())
    assert runner is not None and type(runner).__name__ == "StageWatchRunner"


def test_opted_out_service_has_no_runner(tmp_path):
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    service = _service(tmp_path, state, grant)
    service.stage_watch = StageWatchConfig()
    assert _attach(service, _docker_adapter()) is None


def _drive_default(tmp_path, *, engaged):
    """A real supervisor + runner built from the default config over fakes."""
    from test_stage_watch_runner import (
        OPERATION,
        Clock,
        DockerAdapter,
        FakeChannel,
        FakeDocker,
        lease_for,
        make_supervisor,
    )

    from tessera_worker.stage_watch_runner import build_stage_watch_runner

    root = _private(tmp_path / "state")
    config, reason = default_stage_watch(root)
    assert reason is None
    marker = root / DEFAULT_KILL_SWITCH_NAME
    if engaged:
        marker.write_text("")
    clock, docker, channel = Clock(), FakeDocker(), FakeChannel()
    adapter = DockerAdapter(docker)
    runner = build_stage_watch_runner(config, state_dir=root, channel=channel, adapter=adapter)
    assert runner is not None
    with make_supervisor(tmp_path, adapter, clock) as supervisor:
        supervisor.watch_runner = runner
        supervisor.drive_once(OPERATION, lease_for(clock))
        clock.advance(61)
        supervisor.tick(OPERATION, lease_for(clock))
    return channel, docker, marker


def test_default_marker_absent_watches_and_reports_over_the_one_channel(tmp_path):
    channel, docker, marker = _drive_default(tmp_path, engaged=False)
    assert "heartbeat" in channel.kinds()
    assert all(body["kill_switch_engaged"] is False for _, body, _, _ in channel.posts)
    assert not marker.exists()
    # observe only: nothing but inspect reads reached Docker after setup
    assert not any(call[0] in {"kill", "stop", "rm", "update"} for call in docker.calls)


def test_default_marker_present_is_engaged_and_requests_nothing(tmp_path):
    channel, _, marker = _drive_default(tmp_path, engaged=True)
    assert marker.exists()
    assert all(body["kill_switch_engaged"] is True for _, body, _, _ in channel.posts)
    assert all("decision" not in path for path, *_ in channel.posts)


def test_remote_provider_and_non_docker_adapter_get_no_runner(tmp_path):
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    service = _service(tmp_path, state, grant)
    assert _attach(service, SimpleNamespace()) is None  # no Docker run seam


def test_watching_opens_no_new_connection(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the watcher must not open a connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    channel, _, _ = _drive_default(tmp_path, engaged=False)
    assert channel.posts  # delivered through the injected worker channel only


# --- explicit flags and tuning-only blocks stay enabled (ADR 0125 sections 3 and 4)


@pytest.mark.parametrize(
    ("extra", "marker", "stats"),
    [
        (["--stage-watcher-kill-switch", "/ops/kill"], "/ops/kill", False),
        (["--stage-watcher-stats"], None, True),
        (["--stage-watcher"], None, False),
    ],
)
def test_any_watcher_flag_alone_is_an_explicit_enabled_config(extra, marker, stats):
    config = cli._stage_watch(
        cli.parser().parse_args(["development-poll", "--config", "x", *extra])
    )
    assert config is not None and config.enabled
    assert config.kill_switch_path == marker and config.stats is stats


def test_the_kill_switch_flag_alone_reaches_the_poller_and_no_watcher_flag_conflicts(
    tmp_path, monkeypatch
):
    path = write_config(tmp_path)
    built = []

    def factory(config):
        built.append(config)
        return DevelopmentGrantPoller(config, channel=ListChannel([]), sleep=lambda _: None)

    monkeypatch.setattr(cli, "DevelopmentGrantPoller", factory)
    base = ["tessera-worker", "development-poll", "--config", str(path), "--once"]
    monkeypatch.setattr(sys, "argv", [*base, "--stage-watcher-kill-switch", str(tmp_path / "kill")])
    assert cli.main() == 0
    assert built[0].stage_watch.enabled
    assert built[0].stage_watch.kill_switch_path == str(tmp_path / "kill")
    with pytest.raises(ValueError):
        cli._stage_watch(
            cli.parser().parse_args(
                ["development-poll", "--config", "x", "--no-stage-watcher", "--stage-watcher-stats"]
            )
        )


def test_a_block_that_only_tunes_stays_enabled_and_takes_the_default_marker(tmp_path):
    assert StageWatchConfig.from_mapping({"silent_seconds": 120}).enabled
    assert not StageWatchConfig.from_mapping({"enabled": False, "silent_seconds": 120}).enabled
    for block in ({"silent_seconds": 120}, {"enabled": True, "silent_seconds": 120}):
        loaded = load_poller_config(write_config(tmp_path, stage_watcher=block))
        assert loaded.stage_watch.enabled and loaded.stage_watch.silent_seconds == 120
        root = _private(tmp_path / "ws")
        resolved = resolve_stage_watch(loaded.stage_watch, root)
        assert resolved.startable and resolved.silent_seconds == 120
        assert resolved.kill_switch_path == str(root / DEFAULT_KILL_SWITCH_NAME)


def test_an_enabled_config_without_a_marker_falls_back_to_the_default_marker(tmp_path, capsys):
    root = _private(tmp_path / "ws")
    resolved = resolve_stage_watch(StageWatchConfig(enabled=True, stats=True), root)
    assert resolved.startable and resolved.stats
    assert resolved.kill_switch_path == str(root / DEFAULT_KILL_SWITCH_NAME)
    given = StageWatchConfig(enabled=True, kill_switch_path=str(tmp_path / "mine"))
    assert resolve_stage_watch(given, root) is given
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    loose.chmod(0o755)
    assert not resolve_stage_watch(StageWatchConfig(enabled=True), loose).enabled
    assert "no watcher will start" in capsys.readouterr().err


def test_the_salad_executor_falls_back_to_the_default_marker_like_the_worker(tmp_path):
    from tessera_control.campaign_salad_executor import (
        DEFAULT_KILL_SWITCH_NAME as SALAD_MARKER,
    )
    from tessera_control.campaign_salad_executor import StageWatcherBlock, effective_stage_watcher

    assert SALAD_MARKER == DEFAULT_KILL_SWITCH_NAME
    for block in (
        StageWatcherBlock(enabled=True, silent_seconds=120),
        StageWatcherBlock(enabled=True),
    ):
        effective = effective_stage_watcher(block, tmp_path)
        assert effective is not None and effective.enabled
        assert effective.kill_switch_path == str(tmp_path / SALAD_MARKER)
    assert effective_stage_watcher(StageWatcherBlock(enabled=False), tmp_path) is None
