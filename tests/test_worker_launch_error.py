"""The development worker reports a definite launch refusal to control (ADR 0046).

Fakes only: a scripted Docker stands in for the engine; the control app runs in-process.
"""

from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from campaign_runner_harness import World, dump, stage_spec
from test_sandbox_egress_worker import PROXY_IMAGE, egress_op
from test_supervisor import OPERATION as SUPERVISED
from test_supervisor import Clock, FakeAdapter, lease, supervisor

from tessera_worker.cpu import CpuLaunchError, CpuOperation, DockerCpuAdapter, launch_error_class

IMAGE = "python@sha256:" + "a" * 64
POOLS = (
    "Error response from daemon: could not find an available, non-overlapping IPv4 "
    "address pool among the defaults to assign to the network: all predefined address "
    "pools have been fully subnetted"
)
METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]


class Docker:
    """Refuses one command with a chosen stderr; everything else is absent or fine."""

    def __init__(self, refuse: str | None = None, stderr: str = "") -> None:
        self.refuse, self.stderr, self.calls = refuse, stderr, []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        a = list(argv)
        self.calls.append(a)
        if a[:2] == ["network", "inspect"] or a[:3] == ["inspect", "--type", "container"]:
            return subprocess.CompletedProcess(a, 1, "", "No such object")
        if self.refuse and a[0] == self.refuse or a[:2] == ["network", self.refuse]:
            return subprocess.CompletedProcess(a, 1, "", self.stderr)
        return subprocess.CompletedProcess(a, 0, "ok", "")


def _adapter(tmp_path: Path, docker: Docker) -> DockerCpuAdapter:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    return DockerCpuAdapter(
        frozenset({IMAGE}), docker, input_root=root, egress_proxy_image=PROXY_IMAGE
    )


def test_an_exhausted_address_pool_is_a_launch_error_before_any_container(tmp_path):
    docker = Docker("create", "unused")
    docker.refuse = None

    def refuse_network(argv):
        if list(argv)[:2] == ["network", "create"]:
            docker.calls.append(list(argv))
            return subprocess.CompletedProcess(list(argv), 1, "", POOLS)
        return docker(argv)

    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    adapter = DockerCpuAdapter(
        frozenset({IMAGE}), refuse_network, input_root=root, egress_proxy_image=PROXY_IMAGE
    )
    with pytest.raises(CpuLaunchError) as raised:
        adapter.prepare(egress_op())
    assert raised.value.launch_error == "network_address_pool_exhausted"
    assert not [c for c in docker.calls if c[0] == "create"]


@pytest.mark.parametrize(
    ("stderr", "network", "expected"),
    [
        (POOLS, True, "network_address_pool_exhausted"),
        ("permission denied", True, "network_create_failed"),
        ("Error: No such image: x", False, "image_unavailable"),
        ("pull access denied for x", False, "image_unavailable"),
        ("something else at /path/to/detail", False, "container_create_failed"),
    ],
)
def test_launch_error_classes_are_closed_and_carry_no_text(stderr, network, expected):
    assert launch_error_class(stderr, network=network) == expected


def _plain_op() -> CpuOperation:
    return CpuOperation("b" * 32, IMAGE, ("python", "-c", "print(2 + 2)"), "c" * 64, 10)


def test_a_refused_container_create_with_no_container_is_a_launch_error(tmp_path):
    adapter = _adapter(tmp_path, Docker("create", "Error: No such image: python"))
    with pytest.raises(CpuLaunchError) as raised:
        adapter.prepare(_plain_op())
    assert raised.value.launch_error == "image_unavailable"


def test_a_lost_create_reply_is_not_a_launch_error(tmp_path):
    def lost(argv):
        if list(argv)[0] == "create":
            from tessera_worker.cpu import CpuError

            raise CpuError("lost reply")
        return Docker()(argv)

    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    adapter = DockerCpuAdapter(frozenset({IMAGE}), lost, input_root=root)
    # Unconfirmed, not refused: the observation stays absent/unknown and nothing is claimed.
    assert adapter.prepare(_plain_op()).state in {"absent", "unknown"}


def test_the_supervisor_result_carries_the_launch_error(tmp_path):
    clock = Clock()

    class Refusing(FakeAdapter):
        def prepare(self, operation):
            raise CpuLaunchError("refused", "network_create_failed")

    with supervisor(tmp_path, Refusing(), clock) as driver:
        result = driver.drive_once(SUPERVISED, lease(clock))
    assert result.phase == "uncertain" and result.reason == "prepare_unconfirmed"
    assert result.launch_error == "network_create_failed"


def test_the_worker_never_starts_an_operation_after_a_launch_error(tmp_path):
    """Control trusts a reported launch error because the worker's journal holds the
    operation uncertain: a later drive only reconciles, it never prepares or starts."""
    clock = Clock()
    calls: list[str] = []

    class Refusing(FakeAdapter):
        def prepare(self, operation):
            calls.append("prepare")
            raise CpuLaunchError("refused", "network_create_failed")

        def start(self, operation):
            calls.append("start")
            return super().start(operation)

    with supervisor(tmp_path, Refusing(), clock) as driver:
        first = driver.drive_once(SUPERVISED, lease(clock))
        again = driver.drive_once(SUPERVISED, lease(clock))
    assert first.launch_error == "network_create_failed"
    assert calls == ["prepare"]  # no second prepare and no start
    assert again.phase in {"uncertain", "running", "terminal"} and again.launch_error is None


# -- end to end through the worker, control and the campaign runner ----------------------


def _launch_refused(monkeypatch):
    from campaign_runner_harness import ScriptedAdapter

    def refuse(self, operation):
        raise CpuLaunchError("refused", "network_address_pool_exhausted")

    monkeypatch.setattr(ScriptedAdapter, "prepare", refuse)


def test_a_refused_launch_is_reported_and_the_campaign_recovers_unattended(tmp_path, monkeypatch):
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC)]
    cid = world.campaign(spec)["id"]
    world.tick_until(
        lambda: any(a["status"] == "admitted" for a in world.attempts(cid)), worker=False
    )
    _launch_refused(monkeypatch)
    world.poller.run(max_cycles=1)
    (event,) = [
        e
        for e in world.events_of("execution.observation_reported")
        if e["payload"].get("launch_error")
    ]
    assert event["payload"]["launch_error"] == "network_address_pool_exhausted"
    assert event["payload"]["observed_state"] == "unknown"
    attempt = world.attempts(cid)[-1]
    past = "2000-01-01T00:00:00Z"
    world.env.execute(
        "UPDATE execution_grants SET expires_at=? WHERE id=?", past, attempt["grant_id"]
    )
    world.env.execute(
        "UPDATE execution_leases SET expires_at=?, hard_expires_at=? WHERE grant_id=?",
        past,
        past,
        attempt["grant_id"],
    )
    monkeypatch.undo()
    world.tick_all(cid)
    assert world.status(cid) == "succeeded", dump(world, cid)
    one, two = world.attempts(cid)
    assert one["failure_class"] == "not_started"
    assert "launch_error:network_address_pool_exhausted" in one["failure_detail"]
    assert two["status"] == "succeeded"


def test_an_old_report_journal_gains_the_launch_error_column(tmp_path):
    from tessera_worker.runtime import CpuRuntime

    path = tmp_path / "runtime-observations.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE reports (
            id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL, observed_state TEXT NOT NULL,
            provider_id TEXT, termination_confirmed INTEGER NOT NULL,
            payload_json TEXT NOT NULL, idempotency_key TEXT NOT NULL,
            completed INTEGER NOT NULL CHECK (completed IN (0, 1)))"""
        )
    runtime = CpuRuntime.__new__(CpuRuntime)
    runtime.directory = tmp_path
    runtime._initialize_observation_journal()
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(reports)")}
    assert "launch_error" in columns
