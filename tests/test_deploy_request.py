"""Deploy requests fire the checked upgrade once their blockers clear (ADR 0165)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest

from tessera_control import deploy_request, deploy_upgrade
from tessera_control.deploy_request import Blocker, RequestRefused
from tessera_control.deploy_upgrade_plan import PLAN_SCHEMA
from tessera_control.store import DATABASE_NAME, initialize_state

RELEASE = "a" * 40
MOVED = "b" * 40
GATE = "c" * 64
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class World:
    """A state directory, a plan for RELEASE and stubbed blockers, main head and upgrade."""

    def __init__(self, tmp_path, *, initialize=False):
        self.root = tmp_path
        self.state = tmp_path / "state"
        if initialize:
            initialize_state(self.state)
        else:
            self.state.mkdir(mode=0o700)
        for name in ("backups", "receipts", "repo", "plan"):
            (tmp_path / name).mkdir(mode=0o700)
        self.plan = self.write_plan()
        self.head = RELEASE
        self.blockers: list[Blocker] = []
        self.upgrades: list[str] = []
        self.result = "ok"

    def write_plan(self, **extra):
        document = {
            "schema": PLAN_SCHEMA,
            "state_dir": str(self.state),
            "backup_dir": str(self.root / "backups"),
            "receipt_dir": str(self.root / "receipts"),
            "source_revision": RELEASE,
            "repo_root": str(self.root / "repo"),
            "services": [
                {
                    "name": "control",
                    "stop_argv": ["stop-control"],
                    "restart_argv": ["restart-control"],
                    "health": {
                        "kind": "file",
                        "path": str(self.root / "health"),
                        "max_age_seconds": 60,
                    },
                }
            ],
            **extra,
        }
        path = self.root / "plan" / "plan.json"
        path.write_text(json.dumps(document))
        path.chmod(0o600)
        return path

    def head_of(self, _root, _ref):
        return self.head

    def probe(self, _state_dir):
        return list(self.blockers)

    def upgrade(self, plan):
        self.upgrades.append(plan.source_revision)
        return {"result": self.result, "failed_step": None, "source_revision": RELEASE}

    def create(self, **extra):
        options = {
            "plan_path": self.plan,
            "release": RELEASE,
            "requester": "operator",
            "gate_evidence_sha256": GATE,
            "now": T0,
            "head_of": self.head_of,
        } | extra
        return deploy_request.create(self.state, **options)

    def tick(self, now=T0 + timedelta(minutes=1), **extra):
        options = {
            "now": now,
            "probes": (self.probe,),
            "preflight": lambda _plan: [],
            "head_of": self.head_of,
            "upgrade": self.upgrade,
        } | extra
        return deploy_request.tick(self.state, **options)

    def record(self, request_id):
        return deploy_request.show(self.state, request_id)

    def receipts(self, request_id):
        return deploy_request.receipts_for(self.state, request_id)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def _events(world, request_id):
    return [receipt["event"] for receipt in world.receipts(request_id)]


# -- request while blocked waits, fires once when the blocker clears ---------------------------


def test_a_blocked_request_waits_then_fires_exactly_once(world):
    request = world.create()
    world.blockers = [Blocker("route.in_flight:salad:running", "1 attempt running")]
    for minute in (1, 2, 3):
        outcome = world.tick(now=T0 + timedelta(minutes=minute))
        assert outcome["outcome"] == "waiting"
        assert [b["id"] for b in outcome["blockers"]] == ["route.in_flight:salad:running"]
    assert world.upgrades == []
    assert world.record(request["request_id"])["status"] == "pending"
    assert world.record(request["request_id"])["checks"] == 3

    world.blockers = []
    assert world.tick(now=T0 + timedelta(minutes=4))["outcome"] == "fired"
    assert world.upgrades == [RELEASE]
    for minute in (5, 6):
        assert world.tick(now=T0 + timedelta(minutes=minute))["outcome"] == "idle"
    assert world.upgrades == [RELEASE]

    record = world.record(request["request_id"])
    assert record["status"] == "fired" and record["reason"] == "deployed"
    assert record["fire"]["result"] == "ok"
    # One waiting receipt for the unchanged blocker set, not one per tick.
    assert _events(world, request["request_id"]) == ["requested", "waiting", "fire", "fired"]


def test_receipts_are_chained_owner_only_and_hold_no_path(world):
    request = world.create(gate_evidence_sha256=None, gate_evidence=_gate_file(world))
    world.tick()
    receipts = world.receipts(request["request_id"])
    previous = None
    for index, receipt in enumerate(receipts, start=1):
        assert receipt["seq"] == index
        assert receipt["previous_receipt_sha256"] == previous
        body = {k: v for k, v in receipt.items() if k != "receipt_sha256"}
        digest = deploy_request.deploy_check.sha256_hex(json.dumps(body, sort_keys=True).encode())
        assert receipt["receipt_sha256"] == digest
        previous = digest
    folder = deploy_request.requests_dir(world.state) / "receipts"
    for path in folder.iterdir():
        assert path.stat().st_mode & 0o077 == 0
        assert str(world.root) not in path.read_text()
    assert str(world.root) not in json.dumps(
        deploy_request.public_view(world.record(request["request_id"]))
    )


def test_a_changed_blocker_set_is_receipted_again(world):
    request = world.create()
    world.blockers = [Blocker("route.in_flight:salad:running", "x")]
    world.tick(now=T0 + timedelta(minutes=1))
    world.blockers = [Blocker("route.in_flight:salad:collected", "x")]
    world.tick(now=T0 + timedelta(minutes=2))
    assert _events(world, request["request_id"]) == ["requested", "waiting", "waiting"]


def test_a_failing_preflight_or_probe_blocks(world):
    request = world.create()

    def broken(_state_dir):
        raise RuntimeError("cannot tell")

    outcome = world.tick(probes=(broken,), preflight=lambda _p: [Blocker("preflight:x", "x")])
    assert outcome["outcome"] == "waiting"
    assert {b["id"] for b in outcome["blockers"]} == {"probe.error", "preflight:x"}
    world.head = None
    outcome = world.tick()
    assert [b["id"] for b in outcome["blockers"]] == ["main.unreadable"]
    assert world.upgrades == []
    assert world.record(request["request_id"])["status"] == "pending"


# -- refusals ----------------------------------------------------------------------------------


def test_an_expired_request_refuses_and_never_fires(world):
    request = world.create(ttl_hours=1)
    outcome = world.tick(now=T0 + timedelta(hours=1))
    assert outcome == {
        "outcome": "expired",
        "request_id": request["request_id"],
        "reason": "expired",
    }
    assert world.upgrades == []
    assert world.tick(now=T0 + timedelta(hours=2))["outcome"] == "idle"
    assert _events(world, request["request_id"]) == ["requested", "refused"]


def test_main_moved_refuses_with_a_named_reason(world):
    request = world.create()
    world.head = MOVED
    outcome = world.tick()
    assert outcome["outcome"] == "refused" and outcome["reason"] == "main_moved"
    assert world.upgrades == []
    last = world.receipts(request["request_id"])[-1]
    assert last["event"] == "refused" and last["main_head"] == MOVED
    # A request for a release that is not main's head is refused up front.
    with pytest.raises(RequestRefused, match="main_moved"):
        world.create()
    # A new gate for the new head is a new request (the plan must name it too).
    world.write_plan(source_revision=MOVED)
    world.create(release=MOVED, gate_evidence_sha256="d" * 64)


def test_a_changed_plan_refuses(world):
    request = world.create()
    world.write_plan(smoke_cpu=True)
    assert world.tick()["reason"] == "plan_changed"
    assert world.record(request["request_id"])["status"] == "refused"
    assert world.upgrades == []


def _gate_file(world):
    path = world.root / "gate.json"
    path.write_text('{"gate": "passed"}')
    return path


def test_a_changed_gate_evidence_file_refuses(world):
    gate = _gate_file(world)
    world.create(gate_evidence_sha256=None, gate_evidence=gate)
    gate.write_text('{"gate": "edited"}')
    assert world.tick()["reason"] == "gate_evidence_changed"
    assert world.upgrades == []


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"release": MOVED}, "source_revision"),
        ({"release": "main"}, "40-character"),
        ({"requester": "a b"}, "requester"),
        ({"ttl_hours": 0}, "expiry"),
        ({"ttl_hours": deploy_request.MAX_TTL_HOURS + 1}, "expiry"),
        ({"gate_evidence_sha256": "x"}, "digest"),
        ({"gate_evidence_sha256": None}, "exactly one"),
        ({"main_ref": "-n"}, "ref"),
    ],
)
def test_create_refuses_bad_input(world, change, match):
    with pytest.raises(RequestRefused, match=match):
        world.create(**change)


def test_create_refuses_a_plan_without_repo_root(world):
    document = json.loads(world.plan.read_text())
    del document["repo_root"]
    world.plan.write_text(json.dumps(document))
    with pytest.raises(RequestRefused, match="repo_root"):
        world.create()


def test_create_is_idempotent_and_admits_one_open_request(world):
    first = world.create()
    assert world.create()["request_id"] == first["request_id"]
    with pytest.raises(RequestRefused, match="already open"):
        world.create(gate_evidence_sha256="d" * 64)
    deploy_request.cancel(world.state, first["request_id"], by="operator")
    assert world.create(gate_evidence_sha256="d" * 64)["request_id"] != first["request_id"]


def test_cancel_stops_a_pending_request_with_a_receipt(world):
    request = world.create()
    cancelled = deploy_request.cancel(world.state, request["request_id"], by="operator")
    assert cancelled["status"] == "cancelled"
    assert world.tick()["outcome"] == "idle"
    assert world.upgrades == []
    assert _events(world, request["request_id"]) == ["requested", "cancelled"]
    with pytest.raises(RequestRefused, match="only a pending one"):
        deploy_request.cancel(world.state, request["request_id"], by="operator")
    with pytest.raises(RequestRefused, match="Unknown"):
        deploy_request.cancel(world.state, "../x", by="operator")


def test_a_failed_upgrade_is_recorded_and_not_retried(world):
    request = world.create()
    world.result = "failed"
    assert world.tick()["outcome"] == "failed"
    assert world.tick()["outcome"] == "idle"
    assert world.upgrades == [RELEASE]
    assert world.record(request["request_id"])["reason"] == "upgrade_failed"

    def raising(_plan):
        raise OSError("disk")

    second = world.create(gate_evidence_sha256="e" * 64)
    assert world.tick(upgrade=raising)["outcome"] == "failed"
    assert world.record(second["request_id"])["reason"] == "upgrade_error"


# -- single flight ---------------------------------------------------------------------------


def test_concurrent_ticks_fire_once(world):
    world.create()
    started, release = threading.Event(), threading.Event()

    def slow_upgrade(plan):
        started.set()
        assert release.wait(30)
        return world.upgrade(plan)

    results: list[dict] = []
    first = threading.Thread(target=lambda: results.append(world.tick(upgrade=slow_upgrade)))
    first.start()
    assert started.wait(30)
    # While the first fire runs, every other tick, create and cancel is refused as busy.
    assert [world.tick()["outcome"] for _ in range(5)] == ["busy"] * 5
    with pytest.raises(deploy_request.Busy):
        world.create(gate_evidence_sha256="d" * 64)
    release.set()
    first.join(30)
    assert results == [
        {"outcome": "fired", "request_id": results[0]["request_id"], "reason": "deployed"}
    ]
    assert world.tick()["outcome"] == "idle"
    assert world.upgrades == [RELEASE]


def test_an_interrupted_fire_is_never_fired_again(world):
    request = world.create()
    path = deploy_request.requests_dir(world.state) / f"{request['request_id']}.json"
    record = json.loads(path.read_text())
    record["status"] = "firing"  # what a tick that died mid-upgrade leaves behind
    path.write_text(json.dumps(record))
    outcome = world.tick()
    assert outcome["outcome"] == "failed" and outcome["reason"] == "fire_interrupted"
    assert world.upgrades == []


# -- the built-in probe ----------------------------------------------------------------------


def test_in_flight_attempts_block_and_settled_ones_do_not(tmp_path):
    world = World(tmp_path, initialize=True)
    assert deploy_request.in_flight_attempts(world.state) == []
    with sqlite3.connect(world.state / DATABASE_NAME) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("PRAGMA ignore_check_constraints=ON")
        columns = [row[1] for row in db.execute("PRAGMA table_info(campaign_stage_attempts)")]
        assert columns, "the store creates campaign_stage_attempts"
        for index, (status, provider) in enumerate(
            [("running", "salad"), ("succeeded", "salad"), ("unknown", None)]
        ):
            db.execute(
                """INSERT INTO campaign_stage_attempts (id, campaign_id, stage_key, attempt,
                status, hold_cents, revision, created_at, updated_at, provider_id)
                VALUES (?, 'c', ?, 1, ?, 0, 1, 't', 't', ?)""",
                (f"a{index}", f"s{index}", status, provider),
            )
    ids = [b.blocker_id for b in deploy_request.in_flight_attempts(world.state)]
    assert ids == ["route.in_flight:salad:running", "route.in_flight:unnamed:unknown"]


def test_an_unreadable_database_is_a_blocker(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / DATABASE_NAME).write_bytes(b"not a database" * 100)
    ids = [b.blocker_id for b in deploy_request.in_flight_attempts(state)]
    assert ids == ["state.unreadable"]


def test_the_default_fire_runs_the_checked_upgrade_path(world, monkeypatch):
    """The fire calls deploy_upgrade.run_upgrade(plan, execute=True) and writes its receipt."""
    calls = []

    def fake_run(plan, *, execute):
        calls.append((plan.source_revision, execute))
        return {
            "result": "ok",
            "failed_step": None,
            "source_revision": plan.source_revision,
            "started_at": "2026-01-01T12:00:00Z",
        }

    monkeypatch.setattr(deploy_upgrade, "run_upgrade", fake_run)
    world.create()
    outcome = deploy_request.tick(
        world.state,
        now=T0 + timedelta(minutes=1),
        probes=(),
        preflight=lambda _p: [],
        head_of=world.head_of,
    )
    assert outcome["outcome"] == "fired"
    assert calls == [(RELEASE, True)]
    written = list((world.root / "receipts").glob("deploy-*.json"))
    assert len(written) == 1
    assert world.record(outcome["request_id"])["fire"]["deploy_receipt_file"] == written[0].name


# -- read surfaces ---------------------------------------------------------------------------


def test_the_cli_lists_shows_and_cancels(world, capsys):
    from tessera_control.cli import main

    request = world.create()
    assert main(["deploy-request", "list", "--state-dir", str(world.state)]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["request_id"] for item in listed] == [request["request_id"]]
    assert "plan_path" not in listed[0]
    assert (
        main(["deploy-request", "show", "--state-dir", str(world.state), request["request_id"]])
        == 0
    )
    shown = json.loads(capsys.readouterr().out)
    assert [r["event"] for r in shown["receipt_log"]] == ["requested"]
    assert (
        main(
            [
                "deploy-request",
                "cancel",
                "--state-dir",
                str(world.state),
                request["request_id"],
                "--by",
                "operator",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "cancelled"
    assert main(["deploy-request", "tick", "--state-dir", "relative"]) == 2


def test_the_api_lists_requests_read_only_and_hides_paths(tmp_path):
    from test_provider_registration_api import World as ApiWorld

    api = ApiWorld(tmp_path)
    state = api.env.state
    (tmp_path / "w").mkdir()
    plan_world = World(tmp_path / "w")
    plan_world.state = state
    plan_world.plan = plan_world.write_plan()
    request = plan_world.create()

    route = "/admin/deploy-requests"
    response = api.client.get(route, headers=api.human())
    assert response.status_code == 200
    body = response.json()
    assert body["open"] == [request["request_id"]]
    assert body["requests"][0]["release"] == RELEASE
    assert [r["event"] for r in body["requests"][0]["receipt_log"]] == ["requested"]
    assert str(tmp_path) not in json.dumps(body)
    assert api.client.get(route, headers=api.agent(["provider_trust:read"])).status_code == 200
    assert api.client.get(route, headers=api.agent(["campaign:read"])).status_code in (401, 403)
    assert api.client.get(route).status_code in (401, 403)
    assert api.client.post(route, headers=api.human(), json={}).status_code == 405


def test_the_mcp_tool_is_read_only_and_calls_the_route():
    from mcp.server.fastmcp import FastMCP

    from tessera_mcp.server import register_provider_registration_tools

    calls = []

    class Client:
        async def deploy_requests(self):
            calls.append("requests")
            return {"schema": "tessera.deploy-requests.v1"}

    server = FastMCP("test")
    register_provider_registration_tools(server, Client())
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    tool = tools["tessera_deploy_requests"]
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.destructiveHint is False
    asyncio.run(server.call_tool("tessera_deploy_requests", {}))
    assert calls == ["requests"]
