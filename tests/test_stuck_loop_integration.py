"""The stuck-campaign work source against the real control routes, driving the standing loop.

In-process, no network, no model call: the loop's candidate is a finding the detector recorded.
Checks the two halves agree on the wire and that nothing acts on the campaign.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest
from test_campaign_stuck_api import DESTINATION, World

from tessera_agent.standing_loop import SqliteCallLedger
from tessera_agent.stuck_loop_control import StuckControl
from tessera_agent.stuck_loop_runner import EXIT_HALTED, EXIT_STOPPED, load_policy
from tessera_agent.stuck_loop_runner import main as runner_main
from tessera_agent.stuck_work_source import StuckWorkSource
from tessera_control.store import utc_now
from tessera_mcp.client import ControlClient
from tessera_mcp.config import Config


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


# ---- the runner end to end: real client, real routes, in-process transport ------------------


class RecordingTransport(httpx.AsyncBaseTransport):
    """The in-process app as an httpx transport; records every request the runner sends."""

    def __init__(self, world: World) -> None:
        self.inner = httpx.ASGITransport(app=world.client.app)
        self.requests: list[tuple[str, str]] = []

    async def handle_async_request(self, request):
        self.requests.append((request.method, request.url.raw_path.decode()))
        return await self.inner.handle_async_request(request)


class Runner:
    """``tessera-stuck-loop`` against the in-process control with an agent of exact scopes."""

    def __init__(self, world, tmp_path, actions, ack_on_offer):
        self.world, self.dir = world, tmp_path / "runner"
        self.dir.mkdir()
        self.policy = self._private(
            "policy.json",
            json.dumps(
                {
                    "enabled": True,
                    "project_id": world.project,
                    "ack_on_offer": ack_on_offer,
                    "max_consecutive_failures": 2,
                    "base_backoff_seconds": 5,
                }
            ),
        )
        token = world.agent(actions)["Authorization"].removeprefix("Bearer ")
        config = Config("http://127.0.0.1", self._private("token", token), world.project,
                        DESTINATION)  # fmt: skip
        self.transport = RecordingTransport(world)
        self.control = StuckControl(ControlClient(config, self.transport), world.project)
        self.now = 1_000_000.0

    def _private(self, name, text):
        path = self.dir / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, text.encode())
        os.close(fd)
        return path

    def sleep(self, seconds):
        self.now += seconds

    def run(self, *extra):
        argv = ["--policy", str(self.policy), "--journal", str(self.dir / "journal.jsonl"),
                "--ledger", str(self.dir / "ledger.sqlite"),
                "--kill-switch", str(self.dir / "kill"), *extra]  # fmt: skip
        return runner_main(
            argv,
            control=self.control,
            environ={"TESSERA_PROJECT_ID": self.world.project},
            clock=lambda: self.now,
            sleep=self.sleep,
        )

    def events(self, name):
        lines = (self.dir / "journal.jsonl").read_text().splitlines()
        return [e for e in map(json.loads, lines) if e["event"] == name]

    def state(self, item_id):
        with SqliteCallLedger(self.dir / "ledger.sqlite") as ledger:
            return ledger.state(item_id)

    def paths(self):
        return {(method, path.split("?")[0]) for method, path in self.transport.requests}


def campaign_rows(world):
    return (
        world.sql("SELECT * FROM campaigns ORDER BY id"),
        world.sql("SELECT * FROM campaign_stages ORDER BY 1"),
        world.sql("SELECT * FROM events WHERE event_type NOT LIKE 'campaign.stuck%' ORDER BY 1"),
    )


def open_ids(world):
    return [f["id"] for f in world.listing().json()["findings"]]


def test_the_runner_offers_a_real_finding_on_a_receipt_with_only_the_read_scope(world, tmp_path):
    (found,) = world.scan().json()["opened"]
    before = campaign_rows(world)
    runner = Runner(world, tmp_path, ["campaign:read"], ack_on_offer=False)
    assert runner.run("--max-ticks", "2") == EXIT_STOPPED
    (offer,) = runner.events("stuck_offered")
    assert offer["finding_id"] == found["id"] and offer["occurrence"] == 1
    assert offer["receipt_sha256"] == found["receipt_sha256"]
    assert offer["policy_digest"] == load_policy(runner.policy).digest
    assert offer["acknowledged"] is False
    assert runner.state(f"{found['id']}:1") == "succeeded"
    assert open_ids(world) == [found["id"]]  # not acknowledged unless the policy says so
    assert campaign_rows(world) == before
    assert runner.paths() == {("GET", f"{world.base}")}

    # A recurrence (the condition clears, then returns) is new work; the old occurrence is not.
    world.sql("UPDATE campaigns SET status='running', stop_reason=NULL, updated_at=?", utc_now())
    world.sql("UPDATE campaign_stages SET updated_at=?", utc_now())
    assert world.scan().json()["resolved"] == [found["id"]]
    assert runner.run("--once") == EXIT_STOPPED  # a resolved finding is not offered
    assert len(runner.events("stuck_offered")) == 1
    world.sql("UPDATE campaigns SET status='blocked', stop_reason='budget_refused:s1'")
    (again,) = world.scan().json()["opened"]
    assert again["occurrences"] == 2
    assert runner.run("--once") == EXIT_STOPPED
    assert [e["item_id"] for e in runner.events("stuck_offered")] == [
        f"{found['id']}:1",
        f"{found['id']}:2",
    ]
    assert runner.paths() == {("GET", f"{world.base}")}


def test_the_runner_acknowledges_only_when_asked_with_read_and_cancel_scope(world, tmp_path):
    (found,) = world.scan().json()["opened"]
    before = campaign_rows(world)
    runner = Runner(world, tmp_path, ["campaign:read", "campaign:cancel"], ack_on_offer=True)
    assert runner.run("--max-ticks", "3") == EXIT_STOPPED
    (offer,) = runner.events("stuck_offered")
    assert offer["acknowledged"] is True
    assert open_ids(world) == []
    (acked,) = world.listing(status="acknowledged").json()["findings"]
    digest = load_policy(runner.policy).digest
    assert acked["id"] == found["id"]
    assert acked["note"] == f"offered to the operator by the agent loop {digest[:12]}"
    assert campaign_rows(world) == before
    assert runner.paths() == {
        ("GET", f"{world.base}"),
        ("POST", f"{world.base}/{found['id']}/acknowledge"),
    }
    assert [m for m, _ in runner.transport.requests].count("POST") == 1


def test_without_the_cancel_scope_the_ack_fails_closed_and_the_finding_stays_open(world, tmp_path):
    (found,) = world.scan().json()["opened"]
    before = campaign_rows(world)
    runner = Runner(world, tmp_path, ["campaign:read"], ack_on_offer=True)
    assert runner.run("--once") == EXIT_STOPPED
    item_id = f"{found['id']}:1"
    assert runner.state(item_id) == "failed_before_start"
    (finished,) = runner.events("call_finished")
    assert finished["outcome"] == "failed" and runner.events("stuck_offered") == []
    assert open_ids(world) == [found["id"]]
    assert runner.run("--max-ticks", "5") == EXIT_HALTED  # refused again, then the cap
    assert runner.events("backoff") and runner.events("halted")
    assert open_ids(world) == [found["id"]]
    assert campaign_rows(world) == before
    assert {m for m, _ in runner.paths()} == {"GET", "POST"}
    assert {p for _, p in runner.paths()} <= {world.base, f"{world.base}/{found['id']}/acknowledge"}
