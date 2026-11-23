"""Worker grant listing route, channel GET, and the development grant poller."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_execution import _grant_payload, _setup
from test_worker_channel import FakeOpener, FakeResponse

from tessera_control.api import create_app
from tessera_control.store import _connect, canonical_digest
from tessera_control.worker_grants_api import router as worker_grants_router
from tessera_worker.cli import parser
from tessera_worker.control_channel import ControlChannelError, HttpControlChannel
from tessera_worker.grant_poller import DevelopmentGrantPoller, GrantPollerConfig

IMAGE = "python@sha256:" + "a" * 64


def _issue(tmp_path: Path):
    state, execution, owner, worker, project, task = _setup(tmp_path)
    with _connect(execution.database_path) as connection:
        snapshot_id = connection.execute("SELECT snapshot_id FROM snapshot_releases").fetchone()[0]
    payload = _grant_payload(task, worker, {"id": snapshot_id})
    _, grant = execution.issue_execution_grant(
        owner, project["id"], "grant", canonical_digest(payload), payload
    )
    app = create_app(state)
    app.include_router(worker_grants_router)
    return state, execution, owner, worker, grant, TestClient(app)


def _get(client, token="worker-token"):
    return client.get("/worker/execution-grants", headers={"Authorization": f"Bearer {token}"})


def test_listing_returns_only_unleased_active_grants_of_the_calling_worker(tmp_path):
    state, execution, owner, worker, grant, client = _issue(tmp_path)
    response = _get(client)
    assert response.status_code == 200
    assert response.json()["grants"] == [
        {
            "id": grant["id"],
            "task_id": grant["task_id"],
            "pinned_image": IMAGE,
            "expires_at": grant["expires_at"],
            "created_at": grant["created_at"],
        }
    ]
    owner_token = (state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
    assert _get(client, owner_token).status_code == 401
    assert _get(client, "nobody").status_code == 401
    assert client.get("/worker/execution-grants").status_code == 401
    execution.acquire_execution_lease(worker, grant["id"], "lease", "lease")
    assert _get(client).json() == {"grants": []}


def test_listing_excludes_expired_and_non_active_grants(tmp_path):
    state, _, _, _, grant, client = _issue(tmp_path)
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE execution_grants SET status='cancel_requested' WHERE id=?", (grant["id"],)
        )
    assert _get(client).json() == {"grants": []}
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE execution_grants SET status='active', expires_at='2000-01-01T00:00:00Z'"
        )
    assert _get(client).json() == {"grants": []}


def test_channel_get_is_bounded_authenticated_and_rejects_bad_paths():
    opener = FakeOpener(FakeResponse(b'{"grants":[]}'))
    channel = HttpControlChannel("https://control.example/", "worker-token", opener=opener)
    assert channel.get("/worker/execution-grants") == {"grants": []}
    request, _ = opener.requests[0]
    assert request.get_method() == "GET" and request.data is None
    assert request.get_header("Authorization") == "Bearer worker-token"
    assert request.full_url == "https://control.example/worker/execution-grants"
    with pytest.raises(ValueError):
        channel.get("/worker/../x")
    with pytest.raises(ControlChannelError):
        HttpControlChannel(
            "https://control.example", "t", opener=FakeOpener(FakeResponse(b"[]"))
        ).get("/x")
    with pytest.raises(ControlChannelError) as error:
        HttpControlChannel(
            "https://control.example", "t", opener=FakeOpener(FakeResponse(status=500))
        ).get("/x")
    assert error.value.status == 500


class ListChannel:
    def __init__(self, grants):
        self.grants = grants
        self.calls = 0

    def get(self, path):
        assert path == "/worker/execution-grants"
        self.calls += 1
        return {"grants": self.grants}


class FakeService:
    def __init__(self, config, log, fail=None):
        self.config, self.log, self.fail = config, log, fail

    def run(self):
        self.log.append((self.config.grant_id, self.config.allowed_image))
        if self.fail:
            raise self.fail
        return {"state": "collected", "quarantined": True}


class FakeClock:
    """Monotonic clock advanced only by the poller's own sleep."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _poller(tmp_path, grants, *, max_grants=None, fail=None, clock=None):
    credential = tmp_path / "cred"
    credential.write_text("worker-token")
    credential.chmod(0o600)
    config = GrantPollerConfig(
        "http://127.0.0.1:8765",
        credential,
        tmp_path / "ws",
        tmp_path / "out",
        (IMAGE,),
        max_grants=max_grants,
    )
    log: list = []
    channel = ListChannel(grants)
    poller = DevelopmentGrantPoller(
        config,
        channel=channel,
        service_factory=lambda cfg: FakeService(cfg, log, fail),
        sleep=clock.sleep if clock else lambda _: None,
        **({"clock": clock, "jitter": lambda: 0.0} if clock else {}),
    )
    return poller, log, channel


def _grant(grant_id, image=IMAGE):
    return {"id": grant_id, "task_id": "t", "pinned_image": image}


def test_poller_runs_allowed_grants_once_and_records_private_result(tmp_path):
    poller, log, channel = _poller(
        tmp_path, [_grant("g1"), _grant("g2", "other@sha256:" + "b" * 64), _grant("../evil")]
    )
    report = poller.run(max_cycles=2)
    assert log == [("g1", IMAGE)]
    assert report["cycles"] == 2 and [g["grant_id"] for g in report["grants"]] == ["g1"]
    result = tmp_path / "ws" / "grants" / "g1" / "result.json"
    assert json.loads(result.read_text()) == {"quarantined": True, "state": "collected"}
    assert stat.S_IMODE(result.stat().st_mode) == 0o600
    assert (tmp_path / "ws" / "grants" / "g1" / "workspace").is_dir()
    assert not (tmp_path / "ws" / "grants" / "g2").exists()


def test_poller_stops_after_max_grants(tmp_path):
    poller, log, _ = _poller(tmp_path, [_grant("g1"), _grant("g2"), _grant("g3")], max_grants=2)
    report = poller.run(max_cycles=None)
    assert [name for name, _ in log] == ["g1", "g2"] and len(report["grants"]) == 2


def test_poller_recovers_grant_with_runtime_descriptor_before_listing(tmp_path):
    poller, log, channel = _poller(tmp_path, [])
    directory = tmp_path / "ws" / "grants" / "gr"
    (directory / "workspace" / "runtime").mkdir(parents=True)
    directory.chmod(0o700)
    (directory / "workspace").chmod(0o700)
    (directory / "workspace" / "runtime" / "runtime.json").write_text("{}")
    DevelopmentGrantPoller._remember_image(directory, IMAGE)
    poller.run(max_cycles=2)
    assert log == [("gr", IMAGE)]


def test_poller_service_failure_records_no_result_and_gives_up_after_bound(tmp_path):
    from tessera_worker.service import DevelopmentWorkerError

    poller, log, _ = _poller(
        tmp_path, [_grant("g1")], fail=DevelopmentWorkerError("lease denied at /secret/path")
    )
    report = poller.run(max_cycles=8)
    assert len(log) == 5 and report["errors"] == 5 and report["grants"] == []
    assert not (tmp_path / "ws" / "grants" / "g1" / "result.json").exists()


def test_poll_cli_requires_allowed_image_and_accepts_repeats():
    arguments = parser().parse_args(
        [
            "development-poll",
            "--control-url=http://127.0.0.1:1",
            "--credential-file=c",
            "--workspace-root=w",
            "--development-output-root=o",
            "--allowed-image=a",
            "--allowed-image=b",
            "--max-grants=1",
        ]
    )
    assert arguments.allowed_image == ["a", "b"] and arguments.max_grants == 1
    with pytest.raises(SystemExit):
        parser().parse_args(["development-poll"])


class FlakyService(FakeService):
    """Fails with a control outage N times, then leases and completes."""

    outages = 0

    def run(self):
        self.log.append((self.config.grant_id, self.config.allowed_image))
        if len(self.log) <= self.outages:
            raise ControlChannelError("connection refused")
        return {"state": "collected", "quarantined": True}


def test_poller_outage_does_not_exhaust_give_up_bound(tmp_path):
    poller, log, _ = _poller(tmp_path, [_grant("g1")])
    FlakyService.outages = 12
    poller.service_factory = lambda cfg: FlakyService(cfg, log)
    report = poller.run(max_cycles=20)
    assert len(log) == 13 and [g["grant_id"] for g in report["grants"]] == ["g1"]
    assert (tmp_path / "ws" / "grants" / "g1" / "result.json").exists()


def test_poller_resumes_leased_grant_absent_from_listing_without_descriptor(tmp_path):
    # The grant was leased (so control no longer lists it) but the service died on a control
    # outage before writing runtime.json: the image marker alone makes it resumable.
    poller, log, _ = _poller(tmp_path, [])
    FlakyService.outages = 0
    poller.service_factory = lambda cfg: FlakyService(cfg, log)
    directory = tmp_path / "ws" / "grants" / "gl"
    directory.mkdir(parents=True)
    directory.chmod(0o700)
    DevelopmentGrantPoller._remember_image(directory, IMAGE)
    report = poller.run(max_cycles=3)
    assert log == [("gl", IMAGE)] and [g["grant_id"] for g in report["grants"]] == ["gl"]


def test_poller_permanent_refusal_of_resumed_grant_gives_up(tmp_path):
    clock = FakeClock()
    poller, log, _ = _poller(tmp_path, [], fail=ControlChannelError("conflict", 409), clock=clock)
    directory = tmp_path / "ws" / "grants" / "gl"
    directory.mkdir(parents=True)
    directory.chmod(0o700)
    DevelopmentGrantPoller._remember_image(directory, IMAGE)
    report = poller.run(max_cycles=60)
    assert len(log) == 5 and report["errors"] == 5 and report["refused"] == 5
    # Without a descriptor the existing failure bound applies; no abandoned marker.
    assert report["abandoned"] == 0 and not (directory / "abandoned.json").exists()


def _descriptor_grant(tmp_path, grant_id="gd"):
    directory = tmp_path / "ws" / "grants" / grant_id
    (directory / "workspace" / "runtime").mkdir(parents=True)
    directory.chmod(0o700)
    (directory / "workspace").chmod(0o700)
    (directory / "workspace" / "runtime" / "runtime.json").write_text("{}")
    DevelopmentGrantPoller._remember_image(directory, IMAGE)
    return directory


def test_poller_backs_off_and_abandons_descriptor_grant_control_refuses(tmp_path):
    # Control already reconciled the grant and answers 409 on the package path.
    clock = FakeClock()
    refusal = ControlChannelError("control rejected request", 409)
    poller, log, _ = _poller(tmp_path, [], clock=clock)
    directory = _descriptor_grant(tmp_path)
    attempts_at = []

    def service(cfg):
        attempts_at.append(clock.now)
        return FakeService(cfg, log, refusal)

    poller.service_factory = service

    report = poller.run(max_cycles=7)  # 12 s of 2 s cycles: attempts at 0, 2 and 6 s
    assert len(log) == 3, "a refused grant is not retried every cycle"
    assert report["refused"] == 3 and report["errors"] == 3 and report["abandoned"] == 0
    assert "grant_id" not in json.dumps({k: report[k] for k in ("refused", "abandoned")})

    report = poller.run(max_cycles=200)
    assert len(log) == 5 and report["refused"] == 2 and report["abandoned"] == 1
    gaps = [later - earlier for earlier, later in zip(attempts_at, attempts_at[1:], strict=False)]
    assert gaps == [2.0, 4.0, 8.0, 16.0]
    marker = json.loads((directory / "abandoned.json").read_text())
    assert marker["reason"] == "control_refused_recovery" and marker["control_status"] == 409
    assert marker["refusals"] == 5 and marker["first_refused_at"] <= marker["abandoned_at"]
    assert stat.S_IMODE((directory / "abandoned.json").stat().st_mode) == 0o600
    assert not (directory / "result.json").exists(), "no fabricated outcome"
    assert poller._recoverable() == []


def test_refusal_backoff_is_capped_and_jittered(tmp_path):
    clock = FakeClock()
    poller, _, _ = _poller(tmp_path, [], clock=clock)
    poller.jitter = lambda: 1.0
    directory = _descriptor_grant(tmp_path)
    for _ in range(3):
        poller._refused(directory, "gd", 409)
    assert poller._retry_at["gd"] == clock.now + 8.0 * 1.25
    poller._refusals["gd"] = (20, "t")
    (directory / "workspace" / "runtime" / "runtime.json").unlink()
    poller._refused(directory, "gd", 409)
    assert poller._retry_at["gd"] == clock.now + 900.0


def test_transient_failure_of_descriptor_grant_is_retried_without_bound(tmp_path):
    clock = FakeClock()
    poller, log, _ = _poller(
        tmp_path, [], fail=ControlChannelError("control rejected request", 503), clock=clock
    )
    directory = _descriptor_grant(tmp_path)
    report = poller.run(max_cycles=12)
    assert len(log) == 12 and report["refused"] == 0 and report["abandoned"] == 0
    assert not (directory / "abandoned.json").exists()
    assert poller._recoverable() == ["gd"]


def test_abandoned_grant_is_never_recovered_or_listed(tmp_path):
    poller, log, _ = _poller(tmp_path, [_grant("gd")])
    directory = _descriptor_grant(tmp_path)
    (directory / "abandoned.json").write_text("{}")
    report = poller.run(max_cycles=3)
    assert log == [] and report["grants"] == [] and poller._recoverable() == []
