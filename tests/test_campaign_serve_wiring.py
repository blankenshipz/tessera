"""`serve` starts and stops the in-process campaign runner via the app lifespan."""

from __future__ import annotations

import sys
import types

import pytest
from fastapi.testclient import TestClient

from tessera_control import campaign_runner, cli
from tessera_control.api import create_app
from tessera_control.store import initialize_state


class FakeHandle:
    def __init__(self, events):
        self.events = events

    def stop(self, timeout=10.0):
        self.events.append("stop")


@pytest.fixture
def events(monkeypatch):
    log: list = []

    def fake_start(state_dir, interval_seconds, **_):
        log.append(("start", state_dir, interval_seconds))
        return FakeHandle(log)

    monkeypatch.setattr(campaign_runner, "start_background", fake_start)
    return log


def test_runner_starts_with_lifespan_and_stops_on_shutdown(tmp_path, events):
    state = tmp_path / "state"
    initialize_state(state)
    app = create_app(state, campaign_runner_interval=0.5)
    assert events == []
    with TestClient(app) as client:
        assert events == [("start", state, 0.5)]
        assert client.get("/health/live").status_code == 200
    assert events[-1] == "stop" and len(events) == 2


def test_no_interval_means_no_runner(tmp_path, events):
    state = tmp_path / "state"
    initialize_state(state)
    with TestClient(create_app(state)) as client:
        assert client.get("/health/live").status_code == 200
    assert events == []


def _serve(monkeypatch, *flags):
    captured = {}

    def fake_create_app(state_dir, deployment=None, **kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(state=types.SimpleNamespace())

    monkeypatch.setattr(cli, "create_app", fake_create_app)
    # The state directory is a placeholder: skip the trust-load audit write.
    monkeypatch.setattr("tessera_control.provider_trust.record_at_start", lambda *a, **k: None)
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr(cli, "require_sqlite_runtime", lambda: None)
    monkeypatch.setattr(
        sys, "argv", ["tessera", "serve", "--state-dir", "/x", "--port", "9", *flags]
    )
    return cli.main(), captured


def test_cli_serve_passes_tick_interval(monkeypatch):
    code, captured = _serve(monkeypatch)
    assert code == 0 and captured["campaign_runner_interval"] == 2.0
    _, captured = _serve(monkeypatch, "--campaign-tick-seconds", "0.5")
    assert captured["campaign_runner_interval"] == 0.5


def test_cli_serve_can_disable_runner_and_bounds_interval(monkeypatch):
    _, captured = _serve(monkeypatch, "--no-campaign-runner")
    assert captured["campaign_runner_interval"] is None
    for bad in ("0.1", "61"):
        code, _ = _serve(monkeypatch, "--campaign-tick-seconds", bad)
        assert code == 2


def _profiles_document():
    from salad_campaign_fixture import profile_data

    env = type("E", (), {"owner": {"org_id": "o"}, "project": {"id": "p"}, "worker": {"id": "w"}})
    return {"schema": "tessera.salad.campaign-profiles.v1", "profiles": [profile_data(env)]}


def _private_file(tmp_path, name, document):
    import json

    tmp_path.chmod(0o700)
    path = tmp_path / name
    path.write_text(json.dumps(document))
    path.chmod(0o600)
    return path


def test_cli_serve_registers_salad_profiles_from_the_alias(monkeypatch, tmp_path):
    from tessera_control.campaign_providers import get_provider

    path = _private_file(tmp_path, "profiles.json", _profiles_document())
    code, captured = _serve(monkeypatch, "--salad-campaign-profiles", str(path))
    assert code == 0 and "salad_profiles" not in captured
    assert get_provider("salad").profiles.get("gpu-main") is not None
    shared = tmp_path / "shared.json"
    shared.write_text(path.read_text())
    shared.chmod(0o644)
    code, _ = _serve(monkeypatch, "--salad-campaign-profiles", str(shared))
    assert code == 2


def test_cli_serve_loads_the_campaign_providers_file(monkeypatch, tmp_path):
    from tessera_control.campaign_providers import get_provider

    document = {
        "schema": "tessera.campaign-providers.v1",
        "local_docker": {"worker_principal_id": "worker-ld"},
        "self_hosted": [{"id": "lab", "worker_principal_id": "worker-lab"}],
        "salad_profiles": _profiles_document(),
    }
    path = _private_file(tmp_path, "providers.json", document)
    code, _ = _serve(monkeypatch, "--campaign-providers", str(path))
    assert code == 0
    assert get_provider("lab").descriptor.worker_principal_id == "worker-lab"
    assert get_provider("local-docker").descriptor.worker_principal_id == "worker-ld"
    assert get_provider("salad").profiles.get("gpu-main") is not None
    # Both sources of Salad profiles, a foreign schema or a group-readable file: exit 2.
    alias = _private_file(tmp_path, "alias.json", _profiles_document())
    code, _ = _serve(
        monkeypatch, "--campaign-providers", str(path), "--salad-campaign-profiles", str(alias)
    )
    assert code == 2
    wrong = _private_file(tmp_path, "wrong.json", {"schema": "nope"})
    code, _ = _serve(monkeypatch, "--campaign-providers", str(wrong))
    assert code == 2
    path.chmod(0o644)
    code, _ = _serve(monkeypatch, "--campaign-providers", str(path))
    assert code == 2


# -- the stuck-campaign scanner is independent of the runner -----------------------------------


@pytest.fixture
def scanner(monkeypatch):
    from tessera_control import campaign_stuck

    log: list = []

    class Handle:
        def stop(self, timeout=10.0):
            log.append("stop")

    def fake_start(database_path, interval_seconds):
        log.append(("start", database_path.name, interval_seconds))
        return Handle()

    monkeypatch.setattr(campaign_stuck, "start_background", fake_start)
    return log


def test_the_scanner_runs_without_the_campaign_runner(tmp_path, scanner):
    state = tmp_path / "state"
    initialize_state(state)
    app = create_app(state)  # no campaign_runner_interval: --no-campaign-runner
    assert scanner == []
    with TestClient(app) as client:
        assert scanner == [("start", "tessera.sqlite3", 300.0)]
        assert client.get("/health/live").status_code == 200
    assert scanner[-1] == "stop" and len(scanner) == 2


def test_the_scanner_interval_is_passed_and_zero_still_disables_it_in_the_scanner(
    tmp_path, scanner
):
    state = tmp_path / "state"
    initialize_state(state)
    with TestClient(create_app(state, campaign_stuck_scan_seconds=45.0)):
        assert scanner == [("start", "tessera.sqlite3", 45.0)]


def test_the_scanner_and_the_runner_both_start_and_both_stop(tmp_path, scanner, events):
    state = tmp_path / "state"
    initialize_state(state)
    with TestClient(create_app(state, campaign_runner_interval=0.5)):
        assert [e[0] for e in events if isinstance(e, tuple)] == ["start"]
        assert [e[0] for e in scanner if isinstance(e, tuple)] == ["start"]
    assert events[-1] == "stop" and scanner[-1] == "stop"


def test_a_zero_interval_starts_no_scan_thread(tmp_path):
    from tessera_control import campaign_stuck

    handle = campaign_stuck.start_background(tmp_path / "x.sqlite3", 0)
    assert handle._thread is None  # the real scanner: 0 disables it
    handle.stop()
