"""Readiness also waits for the first runner tick and for provider registration."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from tessera_control import campaign_providers, campaign_runner
from tessera_control.api import create_app
from tessera_control.store import initialize_state


@pytest.fixture
def state(tmp_path):
    path = tmp_path / "state"
    initialize_state(path)
    return path


@pytest.fixture
def gated_runner(monkeypatch):
    """A runner whose ticks are released by the test."""
    release = threading.Event()
    real_tick = campaign_runner.CampaignRunner.tick

    def tick(self, *args, **kwargs):
        release.wait(10)
        return real_tick(self, *args, **kwargs)

    monkeypatch.setattr(campaign_runner.CampaignRunner, "tick", tick)
    yield release
    release.set()


def test_unready_until_first_tick_then_ready(state, gated_runner):
    with TestClient(create_app(state, campaign_runner_interval=0.2)) as client:
        before = client.get("/health/ready")
        assert before.status_code == 503
        assert before.json()["reason"] == "runner_not_ticked"
        # The request gate is unaffected by the runner's tick.
        assert client.get("/health/live").status_code == 200
        gated_runner.set()
        for _ in range(100):
            after = client.get("/health/ready")
            if after.status_code == 200:
                break
            threading.Event().wait(0.05)
        assert after.status_code == 200 and after.json()["reason"] is None


def test_request_gate_does_not_wait_for_a_tick(state, gated_runner):
    from tessera_control.deployment import backend_status

    with TestClient(create_app(state, campaign_runner_interval=0.2)):
        assert backend_status(state)["status"] == "ready"


def test_runner_disabled_needs_no_tick(state):
    with TestClient(create_app(state, campaign_runner_interval=None)) as client:
        assert client.get("/health/ready").status_code == 200


def test_providers_unregistered_is_unready(state, monkeypatch):
    monkeypatch.setattr(campaign_providers, "_CONFIGURED", [])
    app = create_app(state, campaign_runner_interval=None)
    app.state.providers_required = True
    with TestClient(app) as client:
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["reason"] == "providers_unregistered"
        campaign_providers._CONFIGURED.append(True)
        assert client.get("/health/ready").status_code == 200
