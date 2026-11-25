"""Durable, idempotent delivery of worker observations (ADR 0054).

Reproduces the failure seen on a live run: a container exited, the exited observation could not
be delivered once, and the attempt was never told. The outbox must deliver it later, once, in
the right order, across a worker restart, and must not retry what control refused.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from test_development_worker_service import (
    ApiChannel,
    _outage_outlasts_the_lease,
    _service,
    _unleased_delivery,
)
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
    # Real time also passes while the end-of-attempt window retries (ADR 0188), so each delay
    # is the doubled outbox backoff less a fraction of a second.
    assert delays[:4] == pytest.approx([4.0, 8.0, 16.0, 32.0], abs=0.5)
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
    # The healthy container keeps running; its exit supersedes the queued running report.
    first = _run_to_pending(tmp_path, state, grant, channel, adapter)
    assert first.get("state") != "report_pending"
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


def test_an_age_limit_runs_from_the_first_undelivered_report_of_the_operation(tmp_path):
    """A superseding exit does not restart the 24 hours of the report it replaced."""
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    adapter = FakeAdapter()
    # The running report fails (queued; the container keeps running), then the exit fails too.
    first = _run_to_pending(
        tmp_path, state, grant, FlakyObservations(state, fail_at={1, 2}), adapter
    )
    assert first["state"] == "report_pending"
    with _connect(tmp_path / "worker" / "runtime" / "runtime-observations.sqlite3") as connection:
        connection.execute(
            "UPDATE reports SET first_failed_at='2000-01-01T00:00:00+00:00', "
            "next_attempt_at='2000-01-01T00:00:00+00:00'"
        )
    # The exit supersedes the stale running report and its own first failure is brand new, but
    # the operation has been undeliverable for far more than a day: it is abandoned, not retried.
    outcome = _resume(tmp_path, state, grant, adapter, FlakyObservations(state, fail_at={1}))
    assert outcome["state"] == "unknown" and outcome["reason"] == "observation_refused"
    rows = {row["observed_state"]: row for row in _outbox(tmp_path)}
    assert rows["exited"]["abandoned"] == 1 and "aged out" in rows["exited"]["last_error"]


def test_a_redelivered_exit_reaches_collection_the_receipt_and_a_succeeded_campaign(
    tmp_path, monkeypatch
):
    """The live failure end to end: the exit report fails once; the next cycle finishes the run."""
    from campaign_runner_harness import ScriptedAdapter, World, dump, stage_spec
    from test_campaign_runner import METRIC, _spec

    from tessera_worker import runtime as worker_runtime
    from tessera_worker.service import DevelopmentWorkerService

    monkeypatch.setattr(worker_runtime, "DELIVERY_BACKOFF_BASE_SECONDS", 0.0)  # due at once
    world = World(tmp_path)
    failed = []
    real_post = world.channel.post

    def post(path, body, key):
        if (
            path == OBSERVATIONS
            and body.get("observed_state") == "exited"
            and body.get("termination_confirmed")
            and not failed
        ):
            failed.append(key)
            raise ConnectionError("synthetic: the exit report is lost once")
        return real_post(path, body, key)

    monkeypatch.setattr(world.channel, "post", post)
    # One adapter per grant, as with a real Docker daemon: the container the first run left
    # behind is still there when the poller recovers the grant.
    adapters = {}

    def service(config):
        adapter = adapters.get(config.grant_id)
        if adapter is None:
            exit_code, outputs = (
                world.scenarios.pop(0) if world.scenarios else world.default_scenario
            )
            adapter = adapters[config.grant_id] = ScriptedAdapter(exit_code, outputs)
            adapter.development_output_root = (
                config.development_output_root / "development-only-cpu-outputs" / "workspaces"
            )
        return DevelopmentWorkerService(
            config,
            channel=world.channel,
            adapter=adapter,
            sleep=lambda _: setattr(adapter, "state", "exited"),
        )

    world.poller.service_factory = service
    campaign = world.campaign(_spec(world, [stage_spec(world.env, "s1", metrics=METRIC)]))
    world.tick_all(campaign["id"])
    assert failed, "the failure was injected"
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    (attempt,) = world.attempts(campaign["id"])
    assert attempt["status"] == "succeeded" and attempt["run_receipt_digest"]  # a receipt
    assert attempt["manifest_id"]  # the redelivered exit led on to collection and a manifest
    exits = [
        row
        for row in world.env.query(
            "SELECT observed_state, termination_confirmed FROM execution_observations"
        )
        if row["observed_state"] == "exited" and row["termination_confirmed"]
    ]
    assert len(exits) == 1  # recorded once, though sent twice under the same key
    assert world.env.query("SELECT count(*) AS n FROM campaign_receipts")[0]["n"] == 1


def test_a_rejected_credential_keeps_the_poller_retrying_at_the_backoff_cap(tmp_path):
    from tessera_worker.runtime import DELIVERY_BACKOFF_CAP_SECONDS

    now = [0.0]
    rejected = {
        "state": "report_pending",
        "delivery_pending": True,
        "credential_rejected": True,
        "control_status": 401,
        "retry_after_seconds": DELIVERY_BACKOFF_CAP_SECONDS,
    }
    poller, calls = _poller(tmp_path, [dict(rejected) for _ in range(3)], lambda: now[0])
    result = poller.run(max_cycles=1)
    assert result["errors"] == 0 and result["grants"] == []
    for at, expected_calls in ((100.0, 1), (299.0, 1), (300.0, 2), (599.0, 2), (600.0, 3)):
        now[0] = at
        poller.run(max_cycles=1)
        assert len(calls) == expected_calls, at  # never sooner than the cap, then again at it
    grant = tmp_path / "ws" / "grants" / "grant_1"
    assert not (grant / "result.json").exists()  # a rejected credential is never a verdict
    # After the operator renews the credential the same grant delivers and is then recorded.
    poller_outcomes = [{"state": "exited", "quarantined": True}]
    (tmp_path / "again").mkdir()
    poller2, calls2 = _poller(tmp_path / "again", poller_outcomes, lambda: now[0])
    assert [i["outcome"]["state"] for i in poller2.run(max_cycles=1)["grants"]] == ["exited"]
    assert calls2 == ["grant_1"]


class SlowObservations(ApiChannel):
    """Fails the Nth observation post with a chosen transient error, before control sees it."""

    def __init__(self, state, error, fail_at=(1,)):
        super().__init__(state)
        self.error, self.fail_at, self.posts_seen = error, set(fail_at), 0

    def post(self, path, body, key):
        if path == OBSERVATIONS:
            self.posts_seen += 1
            if self.posts_seen in self.fail_at:
                raise self.error
        return super().post(path, body, key)


def _exits_on_nth_sleep(adapter, n):
    count = [0]

    def sleep(_):
        count[0] += 1
        if count[0] >= n:
            adapter.state = "exited"

    return sleep


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("timed out"),
        ControlChannelError("control request timed out"),
        ControlChannelError("control unavailable", 503),
        ConnectionResetError("reset"),
    ],
    ids=["socket-timeout", "channel-timeout", "http-503", "connection-reset"],
)
def test_a_slow_or_failed_running_observation_does_not_stop_a_healthy_container(
    tmp_path, monkeypatch, error
):
    """The live failure: a routine running report timed out and the worker killed the container."""
    from tessera_worker import runtime as worker_runtime

    monkeypatch.setattr(worker_runtime, "DELIVERY_BACKOFF_BASE_SECONDS", 0.0)  # due at once
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    adapter = FakeAdapter()
    channel = SlowObservations(state, error)  # the first running report is lost
    service = _service(
        tmp_path,
        state,
        grant,
        channel=channel,
        adapter=adapter,
        max_ticks=6,
        sleep=_exits_on_nth_sleep(adapter, 3),  # still running for two more ticks
    )
    outcome = service.run()
    assert "cancel" not in adapter.calls  # the healthy container was never cancelled
    assert outcome.get("state") != "report_pending"
    assert outcome.get("reason") != "observation_report_failure"
    observed = _control_observations(state)
    assert observed[0] == ("running", 0)  # the lost running report was redelivered
    assert ("exited", 1) in observed
    assert channel.posts_seen >= 3
    running = [r for r in _outbox(tmp_path) if r["observed_state"] == "running"]
    assert running and all(r["completed"] == 1 for r in running)  # delivered, none left queued
    assert running[0]["attempts"] == 1  # one transient failure, then the retry succeeded


def test_the_run_keeps_going_while_the_running_report_waits_out_its_backoff(tmp_path):
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    adapter = FakeAdapter()
    channel = SlowObservations(state, TimeoutError("timed out"))
    service = _service(
        tmp_path,
        state,
        grant,
        channel=channel,
        adapter=adapter,
        max_ticks=4,
        sleep=_exits_on_nth_sleep(adapter, 3),
    )
    outcome = service.run()  # the 2 s backoff has not elapsed, so no early resend
    assert "cancel" not in adapter.calls
    assert channel.posts_seen == 2  # the failed running report, then the exit (no spam between)
    assert ("exited", 1) in _control_observations(state)
    assert outcome.get("state") != "report_pending"


@pytest.mark.parametrize("status", [400, 409, 422])
def test_a_confirmed_refusal_of_a_running_observation_still_cancels(tmp_path, status):
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    adapter = FakeAdapter()
    channel = SlowObservations(state, ControlChannelError("refused", status))
    service = _service(
        tmp_path,
        state,
        grant,
        channel=channel,
        adapter=adapter,
        max_ticks=6,
        sleep=_exits_on_nth_sleep(adapter, 3),
    )
    outcome = service.run()
    assert (outcome["state"], outcome["reason"]) == ("unknown", "observation_refused")
    assert adapter.calls.count("cancel") == 1  # control refused it: stop the run
    assert adapter.state == "exited"
    assert any(r["abandoned"] for r in _outbox(tmp_path))


def test_a_lost_lease_while_an_observation_waits_still_cancels(tmp_path, monkeypatch):
    """Authority loss, not a slow report, is what stops a run: renewal fails and cancels."""
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    adapter = FakeAdapter()

    class Outage(SlowObservations):
        def post(self, path, body, key):
            if path == OBSERVATIONS and self.posts_seen >= 1:
                raise ControlChannelError("control unreachable")  # all fails after the first
            if path == OBSERVATIONS:
                self.posts_seen += 1
                raise self.error
            if self.posts_seen >= 1:
                raise ControlChannelError("control unreachable")
            return ApiChannel.post(self, path, body, key)

    service = _service(
        tmp_path,
        state,
        grant,
        channel=Outage(state, TimeoutError("timed out")),
        adapter=adapter,
        max_ticks=6,
        sleep=_exits_on_nth_sleep(adapter, 4),
    )
    # The outage lasts past the lease's own expiry: a renewal that fails while the lease has
    # time left is retried (ADR 0178), one that cannot be confirmed before it ends cancels.
    outcome = _outage_outlasts_the_lease(service, monkeypatch).run()
    assert outcome["reason"] == "heartbeat_or_renewal_failure"
    assert adapter.calls.count("cancel") == 1


# -- the operator's control timeout --------------------------------------------------------


def test_the_control_timeout_defaults_to_15_seconds_and_fits_the_lease():
    from tessera_worker.control_channel import (
        DEFAULT_TIMEOUT_SECONDS,
        HttpControlChannel,
        validate_operator_timeout,
    )

    assert DEFAULT_TIMEOUT_SECONDS == 15.0
    assert HttpControlChannel("http://127.0.0.1:1", "token").timeout_seconds == 15.0
    assert validate_operator_timeout(1) == 1.0 and validate_operator_timeout(20) == 20.0
    for bad in (0, 0.5, 21, 120, -3, True, "15", None, float("nan")):
        with pytest.raises(ValueError):
            validate_operator_timeout(bad)


def test_the_timeout_is_an_operator_setting_of_the_worker_and_the_poller(tmp_path, monkeypatch):
    import json
    import os

    from tessera_worker import grant_poller
    from tessera_worker import service as worker_service
    from tessera_worker.grant_poller import load_poller_config
    from tessera_worker.service import DevelopmentWorkerConfig, DevelopmentWorkerError

    created = []

    class Channel:
        def __init__(self, url, token, *, timeout_seconds):
            created.append(timeout_seconds)

        def get(self, path):  # pragma: no cover - unused
            return {}

    monkeypatch.setattr(worker_service, "HttpControlChannel", Channel)
    monkeypatch.setattr(grant_poller, "HttpControlChannel", Channel)
    credential = tmp_path / "cred"
    credential.write_text("token")
    credential.chmod(0o600)
    base = {
        "control_url": "http://127.0.0.1:1",
        "credential_file": credential,
        "workspace_root": tmp_path / "ws",
        "development_output_root": tmp_path / "out",
    }
    image = "python@sha256:" + "a" * 64

    def worker(**kw):
        return DevelopmentWorkerConfig(grant_id="g", allowed_image=image, **base, **kw)

    def poller_config(**kw):
        return grant_poller.GrantPollerConfig(**base, allowed_images=(image,), **kw)

    # The poller's own channel and the one a per-grant service builds both honour the setting.
    assert worker().control_timeout_seconds == 15.0
    poller = grant_poller.DevelopmentGrantPoller(poller_config(control_timeout_seconds=18))
    worker_service.DevelopmentWorkerService(worker(control_timeout_seconds=18))
    assert created == [18, 18] and poller.config.control_timeout_seconds == 18
    for bad in (0, 0.2, 21, 500, True):
        with pytest.raises(DevelopmentWorkerError):
            worker(control_timeout_seconds=bad)
        with pytest.raises(DevelopmentWorkerError):
            poller_config(control_timeout_seconds=bad)
    # The config file key and the CLI flags.
    path = tmp_path / "poll.json"
    document = {
        "schema": "tessera.worker.development-poll.v1",
        "control_url": "http://127.0.0.1:1",
        "credential_file": str(credential),
        "workspace_root": str(tmp_path / "ws"),
        "development_output_root": str(tmp_path / "out"),
        "allowed_images": [image],
        "control_timeout_seconds": 20,
    }
    path.write_text(json.dumps(document))
    os.chmod(path, 0o600)
    assert load_poller_config(path).control_timeout_seconds == 20
    from tessera_worker.cli import parser

    flags = [
        "--control-url", "http://127.0.0.1:1", "--credential-file", str(credential),
        "--workspace-root", str(tmp_path), "--development-output-root", str(tmp_path),
        "--allowed-image", image, "--control-timeout-seconds", "30",
    ]  # fmt: skip
    assert parser().parse_args(["development-poll", *flags]).control_timeout_seconds == 30.0
    assert parser().parse_args(["development-poll", *flags[:-2]]).control_timeout_seconds == 15.0
    assert (
        parser().parse_args(["development-run", "--grant-id", "g", *flags]).control_timeout_seconds
        == 30.0
    )


# -- a timed-out request must not block the next call (real channel, real local server) ------


class _SlowServer:
    """A loopback control stand-in: dribbles a body, answers late, or answers at once."""

    def __init__(self, dribble_seconds=0.15, late_seconds=1.0):
        import http.server
        import threading
        import time

        body = b'{"ok":1}'
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply_headers(self, length):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(length))
                self.end_headers()

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.paths.append(self.path)
                try:
                    if self.path.endswith("/dribble"):
                        # Headers at once, then the body a byte at a time: every read succeeds
                        # within the socket timeout, so the request thread outlives the caller.
                        self._reply_headers(len(body))
                        self.wfile.flush()
                        for byte in body:
                            time.sleep(dribble_seconds)
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                    elif self.path.endswith("/late"):
                        time.sleep(late_seconds)
                        self._reply_headers(len(body))
                        self.wfile.write(body)
                    elif self.path.endswith("/hang"):
                        time.sleep(30)
                    else:
                        self._reply_headers(len(body))
                        self.wfile.write(body)
                except OSError:
                    pass  # the client gave up

        self.paths = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def slow_server():
    server = _SlowServer()
    yield server
    server.close()


@pytest.mark.parametrize("slow", ["dribble", "late"])
def test_a_timed_out_request_does_not_make_the_next_call_fail(slow_server, slow):
    from tessera_worker.control_channel import HttpControlChannel

    # The report gets a short per-call deadline; the next call (the heartbeat) has the channel's
    # 2 s, so it may wait up to half of that for the straggler to finish.
    channel = HttpControlChannel(slow_server.url, "token", timeout_seconds=2.0)
    with pytest.raises(ControlChannelError, match="deadline"):
        channel.post(f"/worker/test/{slow}", {}, "k1", timeout_seconds=0.4)  # the report times out
    # The very next call goes through instead of being refused with "still in progress", which
    # would have cancelled the healthy run as a failed heartbeat.
    assert channel.post("/worker/test/fast", {}, "k2") == {"ok": 1}
    assert slow_server.paths == [f"/worker/test/{slow}", "/worker/test/fast"]


def test_a_call_with_a_short_deadline_is_never_held_for_a_straggler(slow_server):
    import time

    from tessera_worker.control_channel import HttpControlChannel

    channel = HttpControlChannel(slow_server.url, "token", timeout_seconds=2.0)
    with pytest.raises(ControlChannelError, match="deadline"):
        channel.post("/worker/test/dribble", {}, "k1", timeout_seconds=0.4)
    started = time.monotonic()
    with pytest.raises(ControlChannelError):  # a cancellation-path call with a 0.2 s deadline
        channel.post("/worker/test/fast", {}, "k2", timeout_seconds=0.2)
    assert time.monotonic() - started < 0.5  # it waited at most half its own 0.2 s


def test_the_wait_for_a_straggler_is_bounded_and_never_adds_threads(monkeypatch):
    import threading
    import time

    from tessera_worker import control_channel
    from tessera_worker.control_channel import HttpControlChannel

    server = _SlowServer(dribble_seconds=0.3)  # a 2.4 s body, each read within the 0.4 s timeout
    try:
        monkeypatch.setattr(control_channel, "STRAGGLER_WAIT_SECONDS", 0.2)
        channel = HttpControlChannel(server.url, "token", timeout_seconds=2.0)
        with pytest.raises(ControlChannelError, match="deadline"):
            channel.post("/worker/test/dribble", {}, "k1", timeout_seconds=0.4)
        before = threading.active_count()
        started = time.monotonic()
        with pytest.raises(ControlChannelError, match="still in progress"):
            channel.post("/worker/test/fast", {}, "k2")
        assert time.monotonic() - started < 1.5  # it waited ~0.2 s, not for the whole body
        assert threading.active_count() <= before  # no second request thread was started
    finally:
        server.close()


# -- one report may not outlast the lease --------------------------------------------------


class _RecordingChannel:
    timeout_seconds = 15.0

    def __init__(self):
        self.timeouts = []

    def post(self, path, body, key, *, timeout_seconds=None):
        self.timeouts.append(timeout_seconds)
        return {}


@pytest.mark.parametrize(
    "seconds_left,expected",
    [(120.0, 15.0), (30.0, 15.0), (22.0, 12.0), (12.0, 2.0), (3.0, 1.0), (-5.0, 1.0)],
)
def test_a_report_never_outlasts_the_lease_it_runs_under(tmp_path, seconds_left, expected):
    from datetime import UTC, datetime, timedelta

    from test_sandbox_egress_worker import OP, _private, egress_op

    from tessera_worker.cpu import CpuObservation
    from tessera_worker.runtime import CpuRuntime
    from tessera_worker.supervisor import DriverResult, Lease

    channel = _RecordingChannel()
    runtime = CpuRuntime(
        tmp_path / "rt", _private(tmp_path / "in"), channel, FakeAdapter(), worker_id="w"
    )
    runtime.operation = egress_op()
    now = datetime.now(UTC)
    runtime._clock = lambda: now
    runtime.lease = Lease("lease-1", OP, "c" * 64, 3, now + timedelta(seconds=seconds_left), 1, "p")
    runtime.report(DriverResult("running", CpuObservation("running", "cid")))
    assert channel.timeouts == [expected]


def test_the_timeout_setting_must_fit_the_lease_and_the_renewal_interval():
    from tessera_control.execution import LEASE_SECONDS
    from tessera_worker.control_channel import (
        MAX_OPERATOR_TIMEOUT_SECONDS,
        RENEWAL_INTERVAL_SECONDS,
        WORKER_LEASE_SECONDS,
        validate_operator_timeout,
    )
    from tessera_worker.service import DevelopmentWorkerConfig, DevelopmentWorkerError

    assert WORKER_LEASE_SECONDS == LEASE_SECONDS  # the worker's copy tracks control's lease
    assert DevelopmentWorkerConfig.renew_after_seconds == RENEWAL_INTERVAL_SECONDS
    assert MAX_OPERATOR_TIMEOUT_SECONDS == LEASE_SECONDS - RENEWAL_INTERVAL_SECONDS == 20.0
    # A slower renewal cadence leaves less room: with renewal every 15 s the ceiling is 15 s.
    assert validate_operator_timeout(15, 15.0) == 15.0
    with pytest.raises(ValueError):
        validate_operator_timeout(16, 15.0)
    base = {
        "control_url": "http://127.0.0.1:1",
        "grant_id": "g",
        "credential_file": Path("/nonexistent/credential"),
        "workspace_root": Path("/nonexistent/workspace"),
        "development_output_root": Path("/nonexistent/outputs"),
        "allowed_image": "python@sha256:" + "a" * 64,
    }
    DevelopmentWorkerConfig(**base, renew_after_seconds=15.0, control_timeout_seconds=15.0)
    with pytest.raises(DevelopmentWorkerError):
        DevelopmentWorkerConfig(**base, renew_after_seconds=15.0, control_timeout_seconds=16.0)
