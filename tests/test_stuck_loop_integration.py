"""The stuck-campaign work source against the real control routes, driving the standing loop.

In-process, no network, no model call: the loop's candidate is a finding the detector recorded.
Checks the two halves agree on the wire and that nothing acts on the campaign.
"""

from __future__ import annotations

import pytest
from test_campaign_stuck_api import World

from tessera_agent.stuck_work_source import StuckWorkSource


class Control:
    """``control.call`` over the in-process app, authenticated as an agent with read scope."""

    def __init__(self, world: World) -> None:
        self.world = world

    def call(self, method, path, body=None, key=None):
        headers = self.world.agent(["campaign:read"])
        response = self.world.client.request(method, path, json=body, headers=headers)
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}")
        return response.json()


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_an_open_finding_is_offered_once_and_an_acknowledged_one_is_not(world):
    (found,) = world.scan().json()["opened"]
    source = StuckWorkSource(Control(world), world.project)
    (item,) = source.observe()
    assert item["id"] == f"{found['id']}:{found['occurrences']}"
    assert item["code"] == "campaign_blocked" and item["kind"] == "campaign_stuck"
    assert item["suggested_action"] == found["suggested_action"]
    assert item["receipt_sha256"] == found["receipt_sha256"]
    assert world.ack(found["id"]).status_code == 200
    assert source.observe() == []


def test_reading_the_source_changes_nothing_on_the_campaign(world):
    world.scan()
    before = world.sql("SELECT status, stop_reason, updated_at FROM campaigns")
    events = world.sql("SELECT COUNT(*) FROM events")
    StuckWorkSource(Control(world), world.project).observe()
    assert world.sql("SELECT status, stop_reason, updated_at FROM campaigns") == before
    assert world.sql("SELECT COUNT(*) FROM events") == events


def test_a_failing_control_yields_no_work(world):
    class Down:
        def call(self, *args, **kwargs):
            raise RuntimeError("down")

    assert StuckWorkSource(Down(), world.project).observe() == []
