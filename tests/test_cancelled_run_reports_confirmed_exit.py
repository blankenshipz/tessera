"""A run the worker kills itself still tells control how it ended (ADR 0053/0054).

When renewal (or a provider receipt report) fails, the worker cancels its own container with
SIGKILL. The cancel is confirmed by Docker with exit code 137, but before this fix that exit
was never reported: control kept seeing ``running`` and the attempt held its grant and the
project's slot until the grant expired. The confirmed exit is now delivered through the
durable outbox, so ADR 0053 closes the attempt as failed_transient/exit_confirmed_unresolved.
An unconfirmed cancel still reports nothing, and a refused observation is never reposted.
"""

from __future__ import annotations

import dataclasses
import sqlite3

from campaign_runner_harness import ScriptedAdapter, World, stage_spec
from test_development_worker_service import ApiChannel, _service, _unleased_delivery
from test_supervisor import FakeAdapter

from tessera_control import campaign_exited_resolve as cer
from tessera_control.store import _connect
from tessera_worker.control_channel import ControlChannelError
from tessera_worker.cpu import CpuObservation
from tessera_worker.provider import FakeProvider
from tessera_worker.service import DevelopmentWorkerService

OBSERVATIONS = "/worker/observations"
RENEW = "/worker/leases/renew"
METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]


class KilledAdapter(FakeAdapter):
    """Like Docker: a cancel is ``kill --signal KILL`` then a fresh inspect (exit 137)."""

    def __init__(self, *, unconfirmed_cancel=False):
        super().__init__()
        self.unconfirmed_cancel = unconfirmed_cancel

    def observe(self, operation):
        self.calls.append("observe")
        if self.state == "absent":
            return CpuObservation("absent")
        exited = self.state == "exited"
        return CpuObservation(self.state, "container", 137 if exited else None, exited)

    def cancel(self, operation):
        self.calls.append("cancel")
        if self.unconfirmed_cancel:
            return CpuObservation("unknown")
        if self.state in {"running", "created"}:
            self.state = "exited"
        return self.observe(operation)


class Recording(ApiChannel):
    """Records observation bodies; fails renewals after the first and chosen exit posts."""

    def __init__(self, state, *, fail_exit_posts=0, refuse_exit=False):
        super().__init__(state)
        self.renewals = 0
        self.fail_exit_posts, self.refuse_exit = fail_exit_posts, refuse_exit
        self.observations: list[dict] = []

    def post(self, path, body, key):
        if path == RENEW:
            self.renewals += 1
            if self.renewals > 1:
                raise ConnectionError("synthetic renewal failure")
        if path == OBSERVATIONS:
            self.observations.append(body)
            if body.get("observed_state") == "exited":
                if self.refuse_exit:
                    raise ControlChannelError("synthetic refusal", 409)
                if self.fail_exit_posts:
                    self.fail_exit_posts -= 1
                    raise ConnectionError("synthetic observation loss")
        return super().post(path, body, key)

    def exits(self):
        return [o for o in self.observations if o.get("observed_state") == "exited"]


def _control_exits(state):
    with _connect(state / "tessera.sqlite3") as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM execution_observations "
            "WHERE observed_state='exited' AND termination_confirmed=1"
        ).fetchone()[0]


def _outbox(tmp_path):
    path = tmp_path / "worker" / "runtime" / "runtime-observations.sqlite3"
    with _connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(r) for r in connection.execute("SELECT * FROM reports ORDER BY id")]


def _make_due(tmp_path):
    path = tmp_path / "worker" / "runtime" / "runtime-observations.sqlite3"
    with _connect(path) as connection:
        connection.execute("UPDATE reports SET next_attempt_at='2000-01-01T00:00:00+00:00'")


def _killed_by_renewal_failure(tmp_path, channel_factory=Recording, **adapter_options):
    state, _, _, _, grant = _unleased_delivery(tmp_path)
    adapter = KilledAdapter(**adapter_options)
    channel = channel_factory(state)
    outcome = _service(tmp_path, state, grant, channel=channel, adapter=adapter, max_ticks=3).run()
    return state, grant, adapter, channel, outcome


def test_a_renewal_failure_kill_reports_exactly_one_confirmed_exit_with_its_code(tmp_path):
    state, _, adapter, channel, outcome = _killed_by_renewal_failure(tmp_path)
    assert outcome["reason"] == "heartbeat_or_renewal_failure"
    assert "delivery_pending" not in outcome  # delivered: the poller records the result
    assert adapter.calls.count("cancel") == 1 and adapter.state == "exited"
    (exit_body,) = channel.exits()
    assert exit_body["termination_confirmed"] is True and exit_body["exit_code"] == 137
    assert _control_exits(state) == 1


def test_an_unconfirmed_cancel_reports_nothing_new(tmp_path):
    state, _, adapter, channel, outcome = _killed_by_renewal_failure(
        tmp_path, unconfirmed_cancel=True
    )
    assert outcome == {
        "state": "unknown",
        "quarantined": False,
        "reason": "heartbeat_or_renewal_failure",
    }
    assert adapter.calls.count("cancel") == 1
    assert channel.exits() == [] and _control_exits(state) == 0


def test_a_cancel_without_an_integer_exit_code_reports_nothing_new(tmp_path, monkeypatch):
    monkeypatch.setattr(
        KilledAdapter,
        "observe",
        lambda self, operation: CpuObservation(
            self.state, "container", None, self.state == "exited"
        ),
    )
    state, _, _, channel, outcome = _killed_by_renewal_failure(tmp_path)
    assert outcome["reason"] == "heartbeat_or_renewal_failure"
    assert channel.exits() == [] and _control_exits(state) == 0


def test_a_refused_exit_is_journaled_and_never_reposted(tmp_path):
    state, grant, _, channel, outcome = _killed_by_renewal_failure(
        tmp_path, lambda s: Recording(s, refuse_exit=True)
    )
    assert outcome["reason"] == "heartbeat_or_renewal_failure"
    assert "delivery_pending" not in outcome  # a verdict: recorded, not recovered
    assert len(channel.exits()) == 1 and _control_exits(state) == 0
    assert [r["abandoned"] for r in _outbox(tmp_path) if r["observed_state"] == "exited"] == [1]


def test_an_undelivered_kill_exit_is_pending_then_delivered_once(tmp_path):
    state, grant, adapter, channel, outcome = _killed_by_renewal_failure(
        tmp_path, lambda s: Recording(s, fail_exit_posts=1)
    )
    assert outcome["delivery_pending"] is True and outcome["state"] == "report_pending"
    assert outcome["reason"] == "heartbeat_or_renewal_failure"
    assert _control_exits(state) == 0
    key = next(r["idempotency_key"] for r in _outbox(tmp_path) if r["observed_state"] == "exited")

    _make_due(tmp_path)
    resumed = Recording(state)
    _service(tmp_path, state, grant, channel=resumed, adapter=adapter, max_ticks=3).run()
    (delivered,) = resumed.exits()
    assert delivered["exit_code"] == 137 and _control_exits(state) == 1
    exits = [r for r in _outbox(tmp_path) if r["observed_state"] == "exited"]
    assert [(r["idempotency_key"], r["completed"]) for r in exits] == [(key, 1)]

    again = Recording(state)  # a second recovery pass reuses control's stored answer
    _service(tmp_path, state, grant, channel=again, adapter=adapter, max_ticks=3).run()
    assert again.exits() == [] and _control_exits(state) == 1


def test_a_receipt_report_failure_kill_reports_its_confirmed_exit(tmp_path):
    state, _, _, _, grant = _unleased_delivery(tmp_path)

    class OneLostReceipt(Recording):
        lose_next_receipt = False

        def post(self, path, body, key):
            if self.lose_next_receipt and "external-operations" in path:
                self.lose_next_receipt = False
                raise ConnectionError("synthetic receipt report disconnect")
            return super().post(path, body, key)

    channel, adapter = OneLostReceipt(state), KilledAdapter()
    channel.renewals = -100  # renewals succeed here; only one receipt report is lost

    def lose_a_receipt(_):
        channel.lose_next_receipt = True

    outcome = _service(
        tmp_path,
        state,
        grant,
        channel=channel,
        provider=FakeProvider(),
        adapter=adapter,
        max_ticks=2,
        sleep=lose_a_receipt,
    ).run()
    assert outcome["reason"] == "receipt_report_failure"
    assert adapter.calls.count("cancel") == 1
    (exit_body,) = channel.exits()
    assert exit_body["termination_confirmed"] is True and exit_body["exit_code"] == 137
    assert _control_exits(state) == 1


# -- end to end: the real poller, control routes and campaign runner -------------------------


class RunnerKilledAdapter(ScriptedAdapter):
    def cancel(self, operation):
        self.calls.append("cancel")
        if self.state in {"running", "created"}:
            self.state = "exited"
        return self.observe(operation)


def _killed_world(tmp_path, *, lose_first_exit=False):
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC, retry={"max_attempts": 2})]
    campaign = world.campaign(spec)
    world.scenarios = [(137, {})]
    channel_post = world.channel.post
    state = {"renewals": 0, "lose_exit": lose_first_exit}
    posted_exits: list[dict] = []

    def post(path, body, key):
        if path == RENEW:
            state["renewals"] += 1
            if state["renewals"] > 1:
                raise ConnectionError("synthetic renewal failure")
        if path == OBSERVATIONS and body.get("observed_state") == "exited":
            posted_exits.append(body)
            if state["lose_exit"]:
                state["lose_exit"] = False
                raise ConnectionError("synthetic observation loss")
        return channel_post(path, body, key)

    world.channel.post = post
    adapters: dict[str, RunnerKilledAdapter] = {}

    def service(config):
        adapter = adapters.get(config.grant_id)
        if adapter is None:  # one container per grant: a recovery sees the same Docker state
            scenario = world.scenarios.pop(0) if world.scenarios else world.default_scenario
            adapter = adapters[config.grant_id] = RunnerKilledAdapter(*scenario)
        adapter.development_output_root = (
            config.development_output_root / "development-only-cpu-outputs" / "workspaces"
        )
        world.worker_runs.append(config.grant_id)
        return DevelopmentWorkerService(
            dataclasses.replace(config, max_ticks=3, renew_every_ticks=1),
            channel=world.channel,
            adapter=adapter,
            sleep=lambda _: None,  # the container keeps running until the worker kills it
        )

    world.poller.service_factory = service
    return world, campaign["id"], posted_exits, state


def _later(world, minutes):
    from datetime import UTC, datetime, timedelta

    world.runner.now = lambda: datetime.now(UTC) + timedelta(minutes=minutes)


def _assert_closed_and_retried(world, cid, attempt):
    done = world.row("SELECT * FROM campaign_stage_attempts WHERE id=?", attempt["id"])
    assert (done["status"], done["failure_class"]) == (
        "failed_transient",
        cer.EXIT_CONFIRMED_UNRESOLVED,
    )
    assert done["settled_cents"] == 0
    assert "exit_code:137" in done["failure_detail"]
    grant = world.row("SELECT * FROM execution_grants WHERE id=?", attempt["grant_id"])
    assert grant["status"] == "reconciled"  # released, so the project's slot is free
    (event,) = world.events_of("campaign.attempt_exit_resolved")
    assert event["payload"]["is_evidence"] is False
    (granted,) = world.events_of("execution.exited_grant_reconciled")
    assert granted["payload"]["settlement_cents"] == 0
    world.tick_until(lambda: len(world.attempts(cid)) == 2, worker=False, limit=20)
    attempts = world.attempts(cid)
    assert [a["attempt"] for a in attempts] == [1, 2]  # the retry policy launches attempt 2
    assert attempts[1]["grant_id"] != attempt["grant_id"]


def test_the_runner_closes_a_worker_killed_attempt_from_its_reported_exit(tmp_path):
    world, cid, posted_exits, _ = _killed_world(tmp_path)
    world.tick_until(
        lambda: any(a["status"] == "admitted" for a in world.attempts(cid)), worker=False
    )
    world.poller.run(max_cycles=1)
    attempt = world.attempts(cid)[-1]
    (body,) = posted_exits
    assert body["exit_code"] == 137 and body["termination_confirmed"] is True
    world.runner.tick()  # inside the lease and grace the attempt is left alone
    held = world.row("SELECT status FROM campaign_stage_attempts WHERE id=?", attempt["id"])
    assert held["status"] in ("admitted", "running")
    _later(world, 10)
    world.runner.tick()
    _assert_closed_and_retried(world, cid, attempt)
    root = tmp_path / "worker-root" / "grants" / attempt["grant_id"]
    assert (root / "result.json").exists()  # delivered: a recorded result, never recovered


def test_the_poller_recovers_an_undelivered_kill_exit_and_the_runner_closes_it(tmp_path):
    world, cid, posted_exits, _ = _killed_world(tmp_path, lose_first_exit=True)
    world.tick_until(
        lambda: any(a["status"] == "admitted" for a in world.attempts(cid)), worker=False
    )
    world.poller.run(max_cycles=1)
    attempt = world.attempts(cid)[-1]
    assert len(posted_exits) == 1  # attempted, lost
    root = tmp_path / "worker-root" / "grants" / attempt["grant_id"]
    assert not (root / "result.json").exists()  # pending, not a verdict
    _later(world, 10)
    world.runner.tick()
    still = world.row("SELECT status FROM campaign_stage_attempts WHERE id=?", attempt["id"])
    assert still["status"] in ("admitted", "running")  # nothing invented without the exit

    outbox = root / "workspace" / "runtime" / "runtime-observations.sqlite3"
    with _connect(outbox) as connection:
        connection.execute("UPDATE reports SET next_attempt_at='2000-01-01T00:00:00+00:00'")
    world.poller.clock = lambda: 1e12  # the poller's own backoff has elapsed
    world.poller.run(max_cycles=1)
    assert len(posted_exits) == 2 and posted_exits[1]["exit_code"] == 137
    exits = world.env.query(
        "SELECT id FROM execution_observations WHERE grant_id=? AND observed_state='exited'",
        attempt["grant_id"],
    )
    assert len(exits) == 1
    world.runner.tick()
    _assert_closed_and_retried(world, cid, attempt)
