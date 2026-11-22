"""development-poll under the shared service loop (ADR 0038 loop contract). Fakes only."""

from __future__ import annotations

import json
import os
import signal
import stat
import sys

import pytest

from tessera_worker import cli
from tessera_worker.grant_poller import (
    CONFIG_SCHEMA,
    DevelopmentGrantPoller,
    GrantPollerConfig,
    load_poller_config,
)
from tessera_worker.service import DevelopmentWorkerError

IMAGE = "python@sha256:" + "a" * 64


class ListChannel:
    def __init__(self, grants):
        self.grants = grants

    def get(self, path):
        return {"grants": self.grants}


class FakeService:
    def __init__(self, config):
        self.config = config

    def run(self):
        return {"state": "collected"}


def credential(tmp_path):
    path = tmp_path / "cred"
    path.write_text("worker-token")
    path.chmod(0o600)
    return path


def write_config(tmp_path, **over):
    document = {
        "schema": CONFIG_SCHEMA,
        "control_url": "http://127.0.0.1:8765",
        "credential_file": str(credential(tmp_path)),
        "workspace_root": str(tmp_path / "ws"),
        "development_output_root": str(tmp_path / "out"),
        "allowed_images": [IMAGE],
        "poll_seconds": 0.01,
        **over,
    }
    path = tmp_path / "poll.json"
    path.write_text(json.dumps(document))
    path.chmod(0o600)
    return path


@pytest.fixture(autouse=True)
def restore_signals():
    saved = {n: signal.getsignal(n) for n in (signal.SIGTERM, signal.SIGINT)}
    yield
    for number, handler in saved.items():
        signal.signal(number, handler)


@pytest.fixture
def fake_poller(monkeypatch):
    """Build real pollers around a fake channel and service, one grant per cycle."""
    built = []
    grants = [{"id": f"g{n}", "pinned_image": IMAGE} for n in range(1, 4)]

    def factory(config):
        poller = DevelopmentGrantPoller(
            config,
            channel=ListChannel(grants),
            service_factory=FakeService,
            sleep=lambda _: None,
        )
        built.append(poller)
        return poller

    monkeypatch.setattr(cli, "DevelopmentGrantPoller", factory)
    return built


def run_cli(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["tessera-worker", "development-poll", *argv])
    code = cli.main()
    return code, capsys.readouterr().out


def test_config_file_carries_operator_container_limits(tmp_path):
    config = load_poller_config(
        write_config(tmp_path, memory_bytes=1024 * 1024 * 1024, scratch_bytes=64 * 1024 * 1024)
    )
    assert (config.memory_bytes, config.scratch_bytes) == (1024 * 1024 * 1024, 64 * 1024 * 1024)
    assert load_poller_config(write_config(tmp_path)).memory_bytes is None


def test_poller_hands_operator_limits_to_every_grant_service(tmp_path):
    limits = (1024 * 1024 * 1024, 64 * 1024 * 1024)
    config = load_poller_config(
        write_config(tmp_path, memory_bytes=limits[0], scratch_bytes=limits[1])
    )
    seen = []
    poller = DevelopmentGrantPoller(
        config,
        channel=ListChannel([{"id": "g9", "pinned_image": IMAGE}]),
        service_factory=lambda c: seen.append(c) or FakeService(c),
        sleep=lambda _: None,
    )
    poller.step()
    assert [(c.memory_bytes, c.scratch_bytes) for c in seen] == [limits]


def test_config_file_loads_into_the_poller_config(tmp_path):
    config = load_poller_config(write_config(tmp_path, max_grants=2))
    assert config.allowed_images == (IMAGE,) and config.max_grants == 2
    assert config.poll_seconds == 0.01


@pytest.mark.parametrize(
    "over",
    [
        {"schema": "other"},
        {"unknown_key": 1},
        {"allowed_images": []},
        {"poll_seconds": 0},
        {"memory_bytes": 2 * 1024 * 1024 * 1024},
        {"memory_bytes": "1073741824"},
        {"memory_bytes": True},
        {"scratch_bytes": 0},
        {"container_user": "root"},
        {"container_user": "1234"},
        {"container_user": 1234},
        {"container_user": "0:0"},
        {"container_user": "1000:0"},
        {"container_user": "4294967295:1"},
        {"allow_root_container_user": "yes"},
        {"container_user": "0:0", "allow_root_container_user": 1},
        {"credential_file": None},
    ],
)
def test_config_file_refusals(tmp_path, over):
    path = write_config(tmp_path, **{k: v for k, v in over.items() if v is not None})
    if over.get("credential_file", 1) is None:
        document = json.loads(path.read_text())
        del document["credential_file"]
        path.write_text(json.dumps(document))
    with pytest.raises(DevelopmentWorkerError, match="unavailable or invalid"):
        load_poller_config(path)


def test_config_file_must_be_a_private_regular_file(tmp_path):
    path = write_config(tmp_path)
    path.chmod(0o666)
    with pytest.raises(DevelopmentWorkerError):
        load_poller_config(path)
    link = tmp_path / "link.json"
    link.symlink_to(write_config(tmp_path))
    with pytest.raises(DevelopmentWorkerError):
        load_poller_config(link)
    with pytest.raises(DevelopmentWorkerError):
        load_poller_config(tmp_path / "missing.json")


def test_a_refused_configuration_exits_2_without_echoing_it(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path, allowed_images=[])
    code, out = run_cli(monkeypatch, capsys, "--config", str(path), "--once")
    assert code == 2 and "worker-token" not in out and str(tmp_path) not in out
    missing = tmp_path / "nocred"
    config = write_config(tmp_path, credential_file=str(missing))
    code, _ = run_cli(monkeypatch, capsys, "--config", str(config), "--once")
    assert code == 2


def test_once_runs_a_single_cycle_and_writes_the_health_file(
    tmp_path, monkeypatch, capsys, fake_poller
):
    health = tmp_path / "health.json"
    code, out = run_cli(
        monkeypatch,
        capsys,
        "--config",
        str(write_config(tmp_path)),
        "--once",
        "--health-file",
        str(health),
    )
    assert code == 0
    summary = json.loads(out.strip().splitlines()[-1])
    assert summary["cycles"] == 1 and summary["grants"] == 3 and summary["errors"] == 0
    document = json.loads(health.read_text())
    assert document["name"] == "development-poll" and document["state"] == "stopped"
    assert stat.S_IMODE(health.stat().st_mode) == 0o600
    assert "worker-token" not in health.read_text()


def test_max_cycles_bounds_a_loop_that_otherwise_runs_forever(
    tmp_path, monkeypatch, capsys, fake_poller
):
    code, out = run_cli(
        monkeypatch, capsys, "--config", str(write_config(tmp_path)), "--max-cycles", "3"
    )
    assert code == 0 and json.loads(out.strip().splitlines()[-1])["cycles"] == 3


def test_max_grants_stops_the_loop_across_cycles(tmp_path, monkeypatch, capsys, fake_poller):
    path = write_config(tmp_path, max_grants=2)
    code, out = run_cli(monkeypatch, capsys, "--config", str(path))
    summary = json.loads(out.strip().splitlines()[-1])
    assert code == 0 and summary["grants"] == 2


def test_sigterm_stops_after_the_current_step_with_exit_0(
    tmp_path, monkeypatch, capsys, fake_poller
):
    original = DevelopmentGrantPoller.step
    calls = []

    def step(self):
        calls.append(1)
        if len(calls) == 2:
            os.kill(os.getpid(), signal.SIGTERM)
        return original(self)

    monkeypatch.setattr(DevelopmentGrantPoller, "step", step)
    code, out = run_cli(monkeypatch, capsys, "--config", str(write_config(tmp_path)))
    assert code == 0 and len(calls) == 2
    assert json.loads(out.strip().splitlines()[-1])["cycles"] == 2


def test_flags_still_work_without_a_config_and_a_missing_set_is_a_parse_error(tmp_path):
    parsed = cli.parser().parse_args(
        [
            "development-poll",
            "--control-url=http://127.0.0.1:1",
            "--credential-file=c",
            "--workspace-root=w",
            "--development-output-root=o",
            "--allowed-image=a",
            "--once",
        ]
    )
    assert parsed.once and parsed.config is None
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["development-poll", "--control-url=http://127.0.0.1:1"])
    assert cli.parser().parse_args(["development-poll", "--config=x"]).config is not None


def test_step_keeps_no_history_beyond_counters(tmp_path):
    config = GrantPollerConfig(
        "http://127.0.0.1:1", credential(tmp_path), tmp_path / "ws", tmp_path / "out", (IMAGE,)
    )
    poller = DevelopmentGrantPoller(
        config,
        channel=ListChannel([{"id": "g1", "pinned_image": IMAGE}]),
        service_factory=FakeService,
        sleep=lambda _: None,
    )
    first = poller.step()
    assert first == [{"service": "development-poll", "grant_id": "g1", "grant_run": True}]
    assert poller.step() == [] and poller.grants_run == 1


def test_config_file_carries_the_operator_container_user(tmp_path):
    assert load_poller_config(write_config(tmp_path)).container_user is None
    config = load_poller_config(write_config(tmp_path, container_user="1000:1000"))
    assert (config.container_user, config.allow_root_container_user) == ("1000:1000", False)
    root = load_poller_config(
        write_config(tmp_path, container_user="0:0", allow_root_container_user=True)
    )
    assert root.container_user == "0:0"


def test_poller_hands_the_container_user_to_every_grant_service(tmp_path):
    config = load_poller_config(write_config(tmp_path, container_user="1000:1001"))
    seen = []
    DevelopmentGrantPoller(
        config,
        channel=ListChannel([{"id": "g9", "pinned_image": IMAGE}]),
        service_factory=lambda c: seen.append(c) or FakeService(c),
        sleep=lambda _: None,
    ).step()
    assert [c.container_user for c in seen] == ["1000:1001"]


def test_cli_container_user_flags_and_refusal(tmp_path, monkeypatch, capsys):
    base = [
        "--control-url",
        "http://127.0.0.1:1",
        "--credential-file",
        "c",
        "--workspace-root",
        "w",
        "--development-output-root",
        "o",
        "--allowed-image",
        IMAGE,
        "--once",
    ]
    parsed = cli.parser().parse_args(["development-poll", *base, "--container-user", "7:8"])
    assert cli._poll_config(parsed).container_user == "7:8"
    code, out = run_cli(monkeypatch, capsys, *base, "--container-user", "0:0")
    assert code == 2 and "refused" in out
    assert (
        cli._poll_config(
            cli.parser().parse_args(
                [
                    "development-poll",
                    *base,
                    "--container-user",
                    "0:0",
                    "--allow-root-container-user",
                ]
            )
        ).allow_root_container_user
        is True
    )
