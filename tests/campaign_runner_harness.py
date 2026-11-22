"""In-process harness for campaign runner tests: real control app, fake worker adapter."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from campaign_fixture import IMAGE, Env
from fastapi.testclient import TestClient
from test_supervisor import FakeAdapter

from tessera_control.api import create_app
from tessera_control.campaign_runner import CampaignRunner
from tessera_control.store import canonical_digest
from tessera_worker.cpu import CpuLaunchError
from tessera_worker.grant_poller import DevelopmentGrantPoller, GrantPollerConfig
from tessera_worker.service import DevelopmentWorkerService

__all__ = [
    "IMAGE",
    "POOLS",
    "World",
    "admit",
    "cut_reports",
    "lapse",
    "latest",
    "refuse_launches",
    "stage_spec",
    "started_but_unreported",
]

POOLS = "network_address_pool_exhausted"
PAST = "2000-01-01T00:00:00Z"


class ApiChannel:
    """Worker channel that reaches the real control app through its HTTP routes."""

    def __init__(self, state: Path) -> None:
        self.client = TestClient(create_app(state))
        self.headers = {"Authorization": "Bearer worker-token"}

    def post(self, path, body, key):
        response = self.client.post(
            path, json=body, headers={**self.headers, "Idempotency-Key": key}
        )
        if response.status_code >= 400:
            raise ConnectionError(
                f"control rejected command: {response.status_code} {response.text[:200]}"
            )
        return response.json()

    def get(self, path):
        response = self.client.get(path, headers=self.headers)
        if response.status_code >= 400:
            raise ConnectionError(f"control rejected read: {response.status_code}")
        return response.json()


class ScriptedAdapter(FakeAdapter):
    """FakeAdapter that writes declared outputs and reports a scripted exit code."""

    def __init__(self, exit_code: int, outputs: dict[str, str]) -> None:
        super().__init__()
        self.exit_code, self.outputs, self.operation = exit_code, outputs, None
        self.now = None

    def observe(self, operation):
        from tessera_worker.cpu import CpuObservation

        self.operation = operation
        base = super().observe(operation)
        if base.container_id is None:
            return base
        # Real container ids are unique; run receipts are unique per provider id.
        return CpuObservation(
            base.state,
            f"container-{operation.operation_id}",
            self.exit_code if base.state == "exited" else base.exit_code,
            base.termination_confirmed,
        )

    def start(self, operation):
        for name, content in self.outputs.items():
            target = Path(operation.development_output.directory, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return super().start(operation)

    def run(self, arguments):
        from types import SimpleNamespace

        self.now = self.now or datetime.now(UTC)  # stable across calls, like Docker

        def stamp(moment):
            return moment.strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z"

        info = [
            {
                "Id": f"container-{self.operation.operation_id}",
                "Image": "sha256:" + "e" * 64,
                "Config": {
                    "Labels": {
                        "tessera.operation": self.operation.operation_id,
                        "tessera.plan": self.operation.plan_digest,
                    }
                },
                "State": {
                    "Status": "exited",
                    "Running": False,
                    "ExitCode": self.exit_code,
                    "StartedAt": stamp(self.now - timedelta(milliseconds=2)),
                    "FinishedAt": stamp(self.now - timedelta(milliseconds=1)),
                },
            }
        ]
        return SimpleNamespace(returncode=0, stdout=json.dumps(info), stderr="")


def stage_spec(env: Env, key: str, *, inputs=None, **extra) -> dict:
    return {
        "key": key,
        "backend": "docker-local-cpu-v1",
        "image": IMAGE,
        "argv": ["python", "-c", "print(1)"],
        "output_paths": ["data.json"],
        "max_output_bytes": 1024,
        "max_duration_seconds": 60,
        "inputs": inputs
        or [
            {
                "kind": "snapshot",
                "name": "code",
                "snapshot_id": env.snapshot_id,
                "digest": env.snapshot_digest,
                "data_class": "public",
            }
        ],
        **extra,
    }


class World:
    """An approved campaign, a runner, and a fake worker driven through the poller."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.env = Env(tmp_path)
        # The fixture's human release of the same snapshot would block the runner's own.
        self.env.execute("UPDATE snapshot_releases SET status='revoked'")
        self.runner = CampaignRunner(self.env.state)
        self.scenarios: list[tuple[int, dict[str, str]]] = []
        self.default_scenario = (0, {"data.json": json.dumps({"score": 3})})
        self.worker_runs: list[str] = []
        self.channel = ApiChannel(self.env.state)
        credential = tmp_path / "worker.credential"
        credential.write_text("worker-token")
        credential.chmod(0o600)
        self.poller = DevelopmentGrantPoller(
            GrantPollerConfig(
                "http://127.0.0.1:8765",
                credential,
                tmp_path / "worker-root",
                tmp_path / "worker-out",
                (IMAGE,),
            ),
            channel=self.channel,
            service_factory=self._service,
            sleep=lambda _: None,
        )

    def _service(self, config):
        exit_code, outputs = self.scenarios.pop(0) if self.scenarios else self.default_scenario
        adapter = ScriptedAdapter(exit_code, outputs)
        adapter.development_output_root = (
            config.development_output_root / "development-only-cpu-outputs" / "workspaces"
        )
        self.worker_runs.append(config.grant_id)

        def finish(_):
            adapter.state = "exited"

        return DevelopmentWorkerService(
            config,
            channel=self.channel,
            adapter=adapter,
            sleep=finish,
        )

    def campaign(self, spec: dict, actor=None) -> dict:
        submitted = self.env.submit(spec=spec)
        return self.env.approve(submitted, actor)

    def row(self, sql: str, *args):
        rows = self.env.query(sql, *args)
        return rows[0] if rows else None

    def status(self, campaign_id: str) -> str:
        return self.row("SELECT status FROM campaigns WHERE id=?", campaign_id)["status"]

    def stages(self, campaign_id: str) -> dict[str, str]:
        return {
            row["stage_key"]: row["status"]
            for row in self.env.query(
                "SELECT stage_key, status FROM campaign_stages WHERE campaign_id=?", campaign_id
            )
        }

    def attempts(self, campaign_id: str) -> list:
        return self.env.query(
            "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? ORDER BY stage_key, attempt",
            campaign_id,
        )

    def restart_runner(self) -> None:
        """A new process: new holder id, and the dead process's lease has lapsed."""
        self.env.execute("UPDATE campaign_runner_lease SET expires_at='2000-01-01T00:00:00Z'")
        self.runner = CampaignRunner(self.env.state)

    def events_of(self, event_type: str) -> list[dict]:
        return self.env.events(event_type)

    def cancel(self, campaign: dict) -> None:
        current = self.row("SELECT revision FROM campaigns WHERE id=?", campaign["id"])
        body = {"expected_revision": current["revision"], "reason": "operator cancel"}
        status, _ = self.env.store.cancel_campaign(
            self.env.human(),
            campaign["id"],
            "cancel-" + campaign["id"][-8:],
            canonical_digest(body),
            body,
        )
        assert status == 200

    def tick_all(self, campaign_id: str, *, worker: bool = True, limit: int = 300) -> int:
        """Alternate runner ticks and worker polls until the campaign is terminal."""
        for count in range(1, limit + 1):
            self.runner.tick()
            if worker:
                self.poller.run(max_cycles=1)
            if self.status(campaign_id) in {"succeeded", "stopped", "failed", "cancelled"}:
                return count
        raise AssertionError("campaign did not finish\n" + dump(self, campaign_id))

    def tick_until(self, predicate, *, worker: bool = True, limit: int = 300) -> int:
        for count in range(1, limit + 1):
            self.runner.tick()
            if predicate():
                return count
            if worker:
                self.poller.run(max_cycles=1)
                if predicate():
                    return count
        raise AssertionError("condition not reached")


def dump(world: World, campaign_id: str) -> str:
    lines = [f"status={world.status(campaign_id)}"]
    for row in world.attempts(campaign_id):
        lines.append(
            f"attempt {row['stage_key']}#{row['attempt']} {row['status']} "
            f"{row['failure_class']} {row['failure_detail']}"
        )
    for row in world.env.intents(campaign_id):
        lines.append(
            f"intent {row['stage_key']} {row['step']} {row['status']} {row['error_class']}"
        )
    row = world.row("SELECT stop_reason FROM campaigns WHERE id=?", campaign_id)
    lines.append(f"stop_reason={row['stop_reason']}")
    return "\n".join(lines)


def latest(world, cid):
    return world.attempts(cid)[-1]


def admit(world, cid):
    world.tick_until(
        lambda: any(a["status"] == "admitted" for a in world.attempts(cid)), worker=False
    )


def cut_reports(world):
    original = world.channel.post

    def cut(path, body, key):
        if "/worker/observations" in path or "receipt" in path or "manifest" in path:
            raise ConnectionError("synthetic report loss")
        return original(path, body, key)

    world.channel.post = cut
    return original


def lapse(world, grant_id):
    world.env.execute("UPDATE execution_grants SET expires_at=? WHERE id=?", PAST, grant_id)
    world.env.execute(
        "UPDATE execution_leases SET expires_at=?, hard_expires_at=? WHERE grant_id=?",
        PAST,
        PAST,
        grant_id,
    )


def refuse_launches(monkeypatch):
    def refuse(self, operation):
        raise CpuLaunchError("refused", POOLS)

    monkeypatch.setattr(ScriptedAdapter, "prepare", refuse)


def started_but_unreported(world, cid, starts) -> dict:
    """The adapter really creates and starts the container (it runs and writes outputs) but
    every report is lost: control sees a lease and an operation and nothing else."""
    admit(world, cid)
    original = ScriptedAdapter.start

    def counted(self, operation):
        starts.append(operation.operation_id)
        return original(self, operation)

    ScriptedAdapter.start = counted
    try:
        cut_reports(world)
        world.poller.run(max_cycles=1)
    finally:
        ScriptedAdapter.start = original
    attempt = latest(world, cid)
    lapse(world, attempt["grant_id"])
    return attempt
