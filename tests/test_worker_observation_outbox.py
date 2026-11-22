"""Durable, idempotent delivery of worker observations (ADR 0054).

Reproduces the failure seen on a live run: a container exited, the exited observation could not
be delivered once, and the attempt was never told. The outbox must deliver it later, once, in
the right order, across a worker restart, and must not retry what control refused.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from test_development_worker_service import ApiChannel, _service, _unleased_delivery
from test_supervisor import FakeAdapter

from tessera_control.store import _connect
from tessera_worker.control_channel import ControlChannelError
from tessera_worker.grant_poller import DevelopmentGrantPoller, GrantPollerConfig
from tessera_worker.runtime import ObservationDeliveryPending

OBSERVATIONS = "/worker/observations"


class FlakyObservations(ApiChannel):
    """Fails the Nth observation post; optionally after control already recorded it."""

    def __init__(self, state, fail_at=(), *, lost_reply=False, status=None):
        super().__init__(state)
        self.fail_at, self.lost_reply, self.status = set(fail_at), lost_reply, status
        self.observation_posts = 0

    def post(self, path, body, key):
        if path != OBSERVATIONS:
            return super().post(path, body, key)
        self.observation_posts += 1
        if self.observation_posts in self.fail_at:
            if self.lost_reply:
                super().post(path, body, key)  # control recorded it; the reply is lost
            if self.status is not None:
                raise ControlChannelError("synthetic refusal", self.status)
            raise ConnectionError("synthetic observation report failure")
        return super().post(path, body, key)


def _exit_after_first_tick(adapter):
    def sleep(_):
        adapter.state = "exited"

    return sleep


def _outbox(tmp_path: Path):
    with _connect(tmp_path / "worker" / "runtime" / "runtime-observations.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute("SELECT * FROM reports ORDER BY id")]


def _control_observations(state):
    with _connect(state / "tessera.sqlite3") as connection:
        return [
            (row["observed_state"], row["termination_confirmed"])
            for row in connection.execute(
                "SELECT observed_state, termination_confirmed FROM execution_observations "
                "ORDER BY observed_at, rowid"
            )
        ]


def _run_to_pending(tmp_path, state, grant, channel, adapter):
    service = _service(
        tmp_path,
        state,
        grant,
        channel=channel,
        adapter=adapter,
        max_ticks=3,
        sleep=_exit_after_first_tick(adapter),
    )
    return service.run()


def _make_due(tmp_path):
    """Let the persisted backoff elapse without sleeping."""
    with _connect(tmp_path / "worker" / "runtime" / "runtime-observations.sqlite3") as connection:
        connection.execute("UPDATE reports SET next_attempt_at='2000-01-01T00:00:00+00:00'")


def _resume(tmp_path, state, grant, adapter, channel=None):
    return _service(
        tmp_path, state, grant, channel=channel or ApiChannel(state), adapter=adapter, max_ticks=3
    ).run()


def test_an_exit_report_that_failed_once_is_delivered_by_the_next_cycle(tmp_path):
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    adapter = FakeAdapter()
    # Observation 1 (running) is delivered, observation 2 (the confirmed exit) fails once.
    channel = FlakyObservations(state, fail_at={2})
    first = _run_to_pending(tmp_path, state, grant, channel, adapter)
    assert first["state"] == "report_pending" and first["delivery_pending"] is True
    assert _control_observations(state) == [("running", 0)]  # control has never seen the exit
    pending = [row for row in _outbox(tmp_path) if not row["completed"]]
    assert [(r["observed_state"], r["termination_confirmed"], r["attempts"]) for r in pending] == [
        ("exited", 1, 1)
    ]
    key = pending[0]["idempotency_key"]

    _make_due(tmp_path)
    second = _resume(tmp_path, state, grant, adapter)
    assert second.get("state") != "report_pending"  # the run completed through collection
    assert ("exited", 1) in _control_observations(state)
    assert all(row["completed"] for row in _outbox(tmp_path) if row["observed_state"] == "exited")
    assert key in {row["idempotency_key"] for row in _outbox(tmp_path)}  # the same identity


def test_the_backoff_survives_a_restart_and_is_bounded(tmp_path):
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    adapter = FakeAdapter()
    channel = FlakyObservations(state, fail_at={2})
    _run_to_pending(tmp_path, state, grant, channel, adapter)
    row = next(r for r in _outbox(tmp_path) if not r["completed"])
    assert row["attempts"] == 1 and row["next_attempt_at"] is not None
    assert row["last_error"].startswith("transient")

    # A restarted worker whose backoff has not elapsed does not contact control for it.
    restarted = ApiChannel(state)
    again = _resume(tmp_path, state, grant, adapter, restarted)
    assert again["state"] == "report_pending" and again["retry_after_seconds"] > 0
    assert OBSERVATIONS not in restarted.paths
    assert [r["attempts"] for r in _outbox(tmp_path) if not r["completed"]] == [1]

    # Repeated failures back off exponentially up to a cap, never beyond.
    delays = []
    for _ in range(12):
        _make_due(tmp_path)
        outcome = _resume(tmp_path, state, grant, adapter, FlakyObservations(state, fail_at={1}))
        delays.append(outcome["retry_after_seconds"])
    assert delays[:4] == [4.0, 8.0, 16.0, 32.0]
    assert max(delays) == 300.0 and delays[-1] == 300.0


def test_a_lost_reply_is_replayed_with_the_same_key_and_recorded_once(tmp_path):
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    adapter = FakeAdapter()
    channel = FlakyObservations(state, fail_at={2}, lost_reply=True)
    _run_to_pending(tmp_path, state, grant, channel, adapter)
    assert _control_observations(state).count(("exited", 1)) == 1  # control did record it
    _make_due(tmp_path)
    healthy = ApiChannel(state)
    _resume(tmp_path, state, grant, adapter, healthy)
    sent = [key for path, key in healthy.keys + channel.keys if path == OBSERVATIONS]
    exits = [r["idempotency_key"] for r in _outbox(tmp_path) if r["observed_state"] == "exited"]
    assert exits[0] in sent
    # Control replayed the stored answer for the repeated key: still exactly one exit row.
    assert _control_observations(state).count(("exited", 1)) == 1


def test_an_undelivered_running_observation_is_never_sent_after_the_exit(tmp_path):
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    adapter = FakeAdapter()
    channel = FlakyObservations(state, fail_at={1})  # the first (running) report fails
    first = _run_to_pending(tmp_path, state, grant, channel, adapter)
    assert first["state"] == "report_pending"
    assert _control_observations(state) == []
    _make_due(tmp_path)
    _resume(tmp_path, state, grant, adapter)
    states = [s for s, _ in _control_observations(state)]
    assert "exited" in states and "running" not in states  # the stale running is superseded
    rows = {r["observed_state"]: r for r in _outbox(tmp_path)}
    assert rows["running"]["abandoned"] == 1 and rows["running"]["last_error"] == "superseded"


def test_a_permanent_refusal_is_recorded_and_never_retried(tmp_path):
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    adapter = FakeAdapter()
    channel = FlakyObservations(state, fail_at={2}, status=409)
    outcome = _run_to_pending(tmp_path, state, grant, channel, adapter)
    assert outcome["state"] == "unknown" and outcome["reason"] == "observation_refused"
    assert "delivery_pending" not in outcome
    refused = [r for r in _outbox(tmp_path) if r["abandoned"]]
    assert [r["observed_state"] for r in refused] == ["exited"]
    assert refused[0]["last_error"].startswith("refused")


def test_a_report_older_than_a_day_is_abandoned_not_retried_forever(tmp_path):
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    adapter = FakeAdapter()
    _run_to_pending(tmp_path, state, grant, FlakyObservations(state, fail_at={2}), adapter)
    with _connect(tmp_path / "worker" / "runtime" / "runtime-observations.sqlite3") as connection:
        connection.execute(
            "UPDATE reports SET first_failed_at='2000-01-01T00:00:00+00:00', "
            "next_attempt_at='2000-01-01T00:00:00+00:00' WHERE completed=0"
        )
    outcome = _resume(tmp_path, state, grant, adapter, FlakyObservations(state, fail_at={1}))
    assert outcome["reason"] == "observation_refused"
    assert any(r["abandoned"] and "aged out" in r["last_error"] for r in _outbox(tmp_path))


def test_control_takes_a_late_exit_after_the_lease_expired_and_never_invents_one(tmp_path):
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    adapter = FakeAdapter()
    _run_to_pending(tmp_path, state, grant, FlakyObservations(state, fail_at={2}), adapter)
    # Nothing was invented on control's side while the exit was undelivered.
    assert ("exited", 1) not in _control_observations(state)
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE execution_leases SET expires_at='2000-01-01T00:00:00Z', "
            "hard_expires_at='2000-01-01T00:00:00Z'"
        )
    _make_due(tmp_path)
    channel = ApiChannel(state)
    _resume(tmp_path, state, grant, adapter, channel)
    # The grant is unreconciled, so control still takes the worker's genuine exit observation
    # even though its lease has expired. (The run itself then ends in an unrelated quarantine
    # failure: this synthetic adapter writes no declared output.)
    assert OBSERVATIONS in channel.paths
    assert ("exited", 1) in _control_observations(state)


def _poller(tmp_path, outcomes, clock):
    credential = tmp_path / "cred"
    credential.write_text("token")
    credential.chmod(0o600)
    calls: list[str] = []

    class Channel:
        def get(self, path):
            return {"grants": [{"id": "grant_1", "pinned_image": "python@sha256:" + "a" * 64}]}

        def post(self, *args):  # pragma: no cover - never reached
            raise AssertionError

    class Service:
        def __init__(self, config):
            self.config = config

        def run(self):
            calls.append(self.config.grant_id)
            return outcomes.pop(0)

    poller = DevelopmentGrantPoller(
        GrantPollerConfig(
            "http://127.0.0.1:1",
            credential,
            tmp_path / "ws",
            tmp_path / "out",
            ("python@sha256:" + "a" * 64,),
        ),
        channel=Channel(),
        service_factory=Service,
        sleep=lambda _: None,
        clock=clock,
    )
    return poller, calls


def test_the_poller_retries_a_pending_delivery_after_the_backoff_not_before(tmp_path):
    now = [100.0]
    outcomes = [
        {"state": "report_pending", "delivery_pending": True, "retry_after_seconds": 30.0},
        {"state": "exited", "quarantined": True},
    ]
    poller, calls = _poller(tmp_path, outcomes, lambda: now[0])
    first = poller.run(max_cycles=1)
    assert first["errors"] == 0 and first["grants"] == []  # neither a verdict nor an error
    assert not (tmp_path / "ws" / "grants" / "grant_1" / "result.json").exists()
    now[0] = 120.0  # still inside the backoff
    poller.run(max_cycles=1)
    assert calls == ["grant_1"]
    now[0] = 131.0  # the backoff has elapsed: the grant is recovered and the result recorded
    done = poller.run(max_cycles=1)
    assert calls == ["grant_1", "grant_1"]
    assert [item["outcome"]["state"] for item in done["grants"]] == ["exited"]
    assert (tmp_path / "ws" / "grants" / "grant_1" / "result.json").exists()


def test_pending_is_an_exception_class_the_runtime_exposes():
    assert issubclass(ObservationDeliveryPending, Exception)


@pytest.mark.parametrize("attempts,expected", [(1, 2.0), (2, 4.0), (9, 300.0), (30, 300.0)])
def test_backoff_formula_is_exponential_and_capped(attempts, expected):
    from tessera_worker.runtime import DELIVERY_BACKOFF_BASE_SECONDS, DELIVERY_BACKOFF_CAP_SECONDS

    delay = min(DELIVERY_BACKOFF_CAP_SECONDS, DELIVERY_BACKOFF_BASE_SECONDS * 2 ** (attempts - 1))
    assert delay == expected


def test_a_never_delivered_exit_is_resolved_at_grant_expiry_without_inventing_evidence(tmp_path):
    from campaign_runner_harness import World, stage_spec
    from test_campaign_runner import METRIC, _spec

    world = World(tmp_path)
    env = world.env
    campaign = world.campaign(_spec(world, [stage_spec(env, "s1", metrics=METRIC)]))
    world.tick_until(
        lambda: any(a["status"] == "admitted" for a in world.attempts(campaign["id"])),
        worker=False,
    )
    world.poller.run(max_cycles=1)  # the worker leased and recorded its operation
    # The worker's observations never arrived (the lost-delivery case this change repairs).
    env.execute("DROP TRIGGER IF EXISTS execution_observations_no_delete")
    env.execute("DELETE FROM execution_observations")
    for _ in range(3):
        world.runner.tick()
    (waiting,) = world.attempts(campaign["id"])
    assert waiting["status"] != "unknown"  # before expiry control waits; it never guesses
    # The grant expires with the exit still undelivered: control marks the attempt unknown for a
    # person. It does not fabricate an exit, an observation, a receipt or a success.
    env.execute("UPDATE execution_grants SET expires_at='2000-01-01T00:00:00Z'")
    for _ in range(3):
        world.runner.tick()
    (attempt,) = world.attempts(campaign["id"])
    assert (attempt["status"], attempt["failure_class"]) == ("unknown", "observed_unknown")
    assert env.query("SELECT count(*) AS n FROM execution_observations")[0]["n"] == 0
    assert env.query("SELECT count(*) AS n FROM campaign_receipts")[0]["n"] == 0
    assert world.status(campaign["id"]) != "succeeded"


def test_the_single_run_cli_exits_with_a_retryable_code_while_an_observation_waits(
    tmp_path, monkeypatch, capsys
):
    from tessera_worker import cli
    from tessera_worker.daemon import classify_exit
    from tessera_worker.exit_codes import EXIT_REPORT_PENDING

    credential = tmp_path / "cred"
    credential.write_text("token")
    credential.chmod(0o600)
    pending = {
        "state": "report_pending",
        "delivery_pending": True,
        "retry_after_seconds": 2.0,
        "reason": "observation_delivery_pending",
    }

    class Service:
        def __init__(self, config):
            pass

        def run(self):
            return pending

    monkeypatch.setattr(cli, "DevelopmentWorkerService", Service)
    monkeypatch.setattr(
        "sys.argv",
        [
            "tessera-worker",
            "development-run",
            "--control-url",
            "http://127.0.0.1:1",
            "--grant-id",
            "grant_1",
            "--credential-file",
            str(credential),
            "--workspace-root",
            str(tmp_path / "ws"),
            "--development-output-root",
            str(tmp_path / "out"),
            "--allowed-image",
            "python@sha256:" + "a" * 64,
        ],
    )
    assert cli.main() == EXIT_REPORT_PENDING == 4
    assert "outbox" in capsys.readouterr().err
    assert classify_exit(EXIT_REPORT_PENDING) == "failure"  # a daemon retries it with backoff
