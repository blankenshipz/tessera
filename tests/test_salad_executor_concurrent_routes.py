"""Salad campaign executor with more than one paid route in flight: fakes only (no subprocess,
no network, no live Salad). ``max_concurrent_paid_routes`` defaults to 1, the serial executor."""

import hashlib
import json
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_campaign_salad_executor import (
    OUTPUT,
    PROFILE,
    RECEIPT,
    WORKER,
    FakeControl,
    config_for,
    custody,
    item,
)

from tessera_control.campaign_salad_executor import (
    CONFIG_SCHEMA,
    ExecutorConfig,
    RouteExitedWithoutReceipt,
    SaladCampaignExecutor,
    load_config,
)
from tessera_control.salad_live_check import installed_source_pin
from tessera_control.store import Conflict

WAIT = 5.0


class ConcurrentControl(FakeControl):
    """The fake control, with phases that move as the store's do and a guardian frame limit."""

    def __init__(self, *, limit=2):
        super().__init__()
        self.limit = limit  # int, None, or an exception to raise
        self.limit_reads = []

    def mark_executing(self, attempt_id):
        super().mark_executing(attempt_id)
        for entry in self.items:
            if entry["attempt_id"] == attempt_id:
                entry["phase"] = "executing"

    def record_outcome(self, attempt_id, payload):
        super().record_outcome(attempt_id, payload)
        self.items = [e for e in self.items if e["attempt_id"] != attempt_id]

    def record_no_receipt(self, attempt_id, route_id):
        self._c("record_no_receipt", attempt_id, route_id)
        self.items = [e for e in self.items if e["attempt_id"] != attempt_id]

    def guardian_frame_limit(self, route_id):
        self.limit_reads.append(route_id)
        if isinstance(self.limit, BaseException):
            raise self.limit
        return self.limit


class GatedRunner:
    """A route runner whose children each block until the test releases that route."""

    def __init__(self):
        self.started = {}
        self.release = {}
        self.outcome = {}
        self.threads = {}
        self.receipt_dirs = {}

    def _event(self, table, route_id):
        return table.setdefault(route_id, threading.Event())

    def __call__(self, config_path, receipt_dir, timeout):
        route_id = json.loads(Path(config_path).read_text())["route_id"]
        self.threads[route_id] = threading.current_thread()
        self.receipt_dirs[route_id] = Path(receipt_dir)
        self._event(self.started, route_id).set()
        if threading.current_thread() is not threading.main_thread():
            assert self._event(self.release, route_id).wait(WAIT)
        result = self.outcome.get(route_id, RECEIPT)
        if isinstance(result, BaseException):
            raise result
        return result

    def wait_started(self, route_id):
        assert self._event(self.started, route_id).wait(WAIT)

    def finish(self, executor, route_id):
        self._event(self.release, route_id).set()
        for run in executor._in_flight.values():
            if run.item["route_id"] == route_id and run.thread is not None:
                run.thread.join(WAIT)
                assert not run.thread.is_alive()


def importer(route_id, destination, profile):
    (Path(destination) / "output.json").write_bytes(OUTPUT)
    return [dict(name="output.json", sha256=hashlib.sha256(OUTPUT).hexdigest(), bytes=len(OUTPUT))]


def executor_for(tmp_path, control, runner, *, routes=None, stage_watch=None):
    config = config_for(tmp_path)
    if routes is not None:
        document = config.model_dump(by_alias=True)
        document["max_concurrent_paid_routes"] = routes
        config = ExecutorConfig.model_validate(document)
    return SaladCampaignExecutor(
        config,
        PROFILE,
        control,
        worker_principal_id=WORKER,
        route_runner=runner,
        importer=importer,
        sleep=lambda _seconds: None,
        stage_watch=stage_watch,
    )


def started(n):
    return [item("route_started", attempt_id=f"att_{i}", route_id=f"route_{i}") for i in n]


def actions(log, attempt=None):
    return [
        (a["action"], a["detail"]) for a in log if attempt is None or a["attempt_id"] == attempt
    ]


PER_ROUTE = ["heartbeat", "renew", "mark_executing", "upload", "report_exited", "guardian_final"]


# -- the default is the serial executor --------------------------------------------------------


@pytest.mark.parametrize("routes", [None, 1])
def test_the_default_runs_each_route_to_completion_inside_the_pass(tmp_path, routes):
    control = ConcurrentControl()
    control.items = started((1, 2))
    runner = GatedRunner()
    executor = executor_for(tmp_path, control, runner, routes=routes)
    log = executor.run_once()
    # Both children ran on the pass's own thread, one after the other, each finished in the
    # pass: exactly the serial executor's calls and notes, with no concurrent-mode notes.
    assert all(t is threading.main_thread() for t in runner.threads.values())
    assert [c[0] for c in control.calls] == (PER_ROUTE + ["record_outcome"]) * 2
    assert (
        actions(log)
        == [
            ("guardian_final_unavailable", "unprojectable"),
            ("outcome_recorded", "manifest"),
        ]
        * 2
    )
    assert executor._in_flight == {} and control.limit_reads == []
    assert executor.config.max_concurrent_paid_routes == 1


# -- two in flight, a third waits --------------------------------------------------------------


def test_two_routes_run_concurrently_and_a_third_waits(tmp_path):
    control = ConcurrentControl(limit=2)
    control.items = started((1, 2, 3))
    runner = GatedRunner()
    executor = executor_for(tmp_path, control, runner, routes=2)

    log = executor.run_once()
    runner.wait_started("route_1")
    runner.wait_started("route_2")
    # Both children are running at once, each off the pass's thread, with its own receipts.
    assert set(executor._in_flight) == {"att_1", "att_2"}
    assert runner.threads["route_1"] is not runner.threads["route_2"]
    assert runner.receipt_dirs["route_1"] != runner.receipt_dirs["route_2"]
    assert ("route_deferred", "executor_route_limit") in actions(log, "att_3")
    # The third is untouched: kept alive, never marked executing, no directory, no refusal.
    assert ("mark_executing", "att_3") not in control.calls
    assert not (tmp_path / "work" / "route_3").exists()
    assert control.rejections == [] and not any(a == "error" for a, _ in actions(log))

    # Still both in flight on the next pass: the third waits again, nothing is resumed.
    log = executor.run_once()
    assert actions(log) == [("route_deferred", "executor_route_limit")]

    runner.finish(executor, "route_1")
    log = executor.run_once()
    assert ("outcome_recorded", "manifest") in actions(log, "att_1")
    assert ("route_launched", "") in actions(log, "att_3")
    runner.wait_started("route_3")
    assert set(executor._in_flight) == {"att_2", "att_3"}
    # The guardian's own limit was read from an in-flight route before each extra start.
    assert control.limit_reads and set(control.limit_reads) <= {"route_1", "route_2"}

    runner.finish(executor, "route_2")
    runner.finish(executor, "route_3")
    log = executor.run_once()
    assert [a for a, _ in actions(log)].count("outcome_recorded") == 2
    assert executor._in_flight == {} and control.items == []
    outcomes = [c[1] for c in control.calls if c[0] == "record_outcome"]
    assert sorted(outcomes) == ["att_1", "att_2", "att_3"]
    # Each route's live config is removed after its child returned.
    assert not any(
        (tmp_path / "work" / f"route_{i}" / "live-config.json").exists() for i in (1, 2, 3)
    )


# -- the guardian bounds concurrency; its refusal only defers ----------------------------------


@pytest.mark.parametrize(
    ("limit", "reason"),
    [
        (ValueError("single_operation_orphan_exposure"), "guardian_limit_unknown"),
        (None, "guardian_limit_unknown"),
        (1, "guardian_frame_limit"),
        (True, "guardian_limit_unknown"),
    ],
)
def test_a_guardian_refusal_or_lower_limit_defers_without_failing(tmp_path, limit, reason):
    control = ConcurrentControl(limit=limit)
    control.items = started((1, 2))
    runner = GatedRunner()
    executor = executor_for(tmp_path, control, runner, routes=4)
    log = executor.run_once()
    assert actions(log, "att_2") == [("route_deferred", reason)]
    assert ("mark_executing", "att_2") not in control.calls
    assert control.rejections == [] and "att_2" not in executor.fenced
    assert not (tmp_path / "work" / "route_2").exists()
    # The guardian now admits a second frame: the waiting route starts, nothing was lost.
    control.limit = 2
    log = executor.run_once()
    assert actions(log, "att_2") == [("route_launched", "")]
    runner.finish(executor, "route_1")
    runner.finish(executor, "route_2")
    executor.run_once()
    assert sorted(c[1] for c in control.calls if c[0] == "record_outcome") == ["att_1", "att_2"]


def test_without_a_guardian_limit_reader_a_second_route_never_starts(tmp_path):
    control = FakeControl()
    control.items = started((1, 2))
    runner = GatedRunner()
    executor = executor_for(tmp_path, control, runner, routes=2)
    log = executor.run_once()
    assert actions(log, "att_2") == [("route_deferred", "guardian_limit_unknown")]
    runner.finish(executor, "route_1")


def test_an_attempt_left_executing_by_an_earlier_process_counts_against_the_limit(tmp_path):
    control = ConcurrentControl(limit=4)
    control.items = [item("executing", attempt_id="att_0", route_id="route_0")] + started((1, 2))
    runner = GatedRunner()
    executor = executor_for(tmp_path, control, runner, routes=2)
    log = executor.run_once()
    # The first start is the serial executor's; the second would make three possible frames.
    assert ("route_launched", "") in actions(log, "att_1")
    assert actions(log, "att_2") == [("route_deferred", "executor_route_limit")]
    runner.finish(executor, "route_1")


# -- one route's failure or fence leaves the other alone ---------------------------------------


def test_one_route_ending_without_a_receipt_does_not_affect_the_other(tmp_path):
    control = ConcurrentControl(limit=2)
    control.items = started((1, 2, 3))
    runner = GatedRunner()
    runner.outcome["route_1"] = RouteExitedWithoutReceipt(
        "refused:single_operation_orphan_exposure"
    )
    executor = executor_for(tmp_path, control, runner, routes=2)
    executor.run_once()
    runner.finish(executor, "route_1")
    log = executor.run_once()
    assert actions(log, "att_1") == [
        ("no_receipt", "refused:single_operation_orphan_exposure"),
        ("no_receipt_reported", ""),
    ]
    # A receiptless end holds further concurrent starts until what is in flight drains; it is
    # never retried (its directory exists) and the other route is untouched.
    assert actions(log, "att_3") == [("route_deferred", "draining_after_no_receipt")]
    assert set(executor._in_flight) == {"att_2"}
    runner.finish(executor, "route_2")
    log = executor.run_once()
    assert ("outcome_recorded", "manifest") in actions(log, "att_2")
    assert ("route_launched", "") in actions(log, "att_3")
    runner.finish(executor, "route_3")
    executor.run_once()
    assert ("mark_executing", "att_1") in control.calls
    assert [c[1] for c in control.calls if c[0] == "record_outcome"] == ["att_2", "att_3"]
    assert sum(1 for c in control.calls if c[0] == "mark_executing") == 3


def test_a_route_whose_runner_fails_is_recorded_and_the_other_finishes(tmp_path):
    control = ConcurrentControl(limit=2)
    control.items = started((1, 2))
    runner = GatedRunner()
    runner.outcome["route_2"] = OSError("child could not start /private/path")
    executor = executor_for(tmp_path, control, runner, routes=2)
    executor.run_once()
    runner.finish(executor, "route_1")
    runner.finish(executor, "route_2")
    log = executor.run_once()
    assert ("outcome_recorded", "manifest") in actions(log, "att_1")
    assert actions(log, "att_2") == [("error", "OSError")]
    assert control.rejections == [("att_2", "execute", "OSError")]
    assert not (tmp_path / "work" / "route_2" / "live-config.json").exists()


def test_a_fenced_attempt_never_starts_and_the_other_runs(tmp_path):
    control = ConcurrentControl(limit=2)
    control.items = started((1, 2))
    runner = GatedRunner()
    executor = executor_for(tmp_path, control, runner, routes=2)
    executor.fenced["att_1"] = "Conflict"
    log = executor.run_once()
    assert actions(log, "att_1") == [("fenced_no_run", "Conflict")]
    assert ("route_launched", "") in actions(log, "att_2")
    runner.finish(executor, "route_2")
    log = executor.run_once()
    assert ("outcome_recorded", "manifest") in actions(log, "att_2")
    assert "route_1" not in runner.threads


def test_a_lease_refused_while_another_route_runs_fences_only_that_attempt(tmp_path):
    control = ConcurrentControl(limit=1)
    control.items = started((1,)) + [item("leased", attempt_id="att_9", route_id="route_9")]
    runner = GatedRunner()
    executor = executor_for(tmp_path, control, runner, routes=2)
    control.renew_error = None
    executor.run_once()
    control.renew_error = Conflict("Execution lease is no longer current.")
    log = executor.run_once()
    # The in-flight route's child renews its own lease: the pass never renews it, so a refused
    # renewal fences only the attempt it was sent for.
    assert actions(log, "att_9") == [("lease_renewal_refused", "Conflict")]
    assert "att_1" not in executor.fenced
    control.renew_error = None
    runner.finish(executor, "route_1")
    log = executor.run_once()
    assert ("outcome_recorded", "manifest") in actions(log, "att_1")


# -- the stage watcher follows one in-flight route ---------------------------------------------


class FakeWatch:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.events = []

    def begin(self, *, operation_id, lease_id, epoch, stage_key, deadline_seconds):
        self.events.append(("begin", operation_id))
        return self.tmp_path / "signals.json"

    @contextmanager
    def ticking(self):
        self.events.append(("tick_start",))
        try:
            yield
        finally:
            self.events.append(("tick_stop",))

    def end(self, operation_id):
        self.events.append(("end", operation_id))


def test_the_stage_watcher_follows_one_route_at_a_time(tmp_path):
    control = ConcurrentControl(limit=2)
    control.items = [dict(entry, stage_key="stage_1") for entry in started((1, 2))]
    watch = FakeWatch(tmp_path)
    runner = GatedRunner()
    executor = executor_for(tmp_path / "x", control, runner, routes=2, stage_watch=watch)
    executor.run_once()
    assert [e[0] for e in watch.events] == ["begin", "tick_start"]
    assert executor._watched == "att_1"
    runner.finish(executor, "route_2")
    executor.run_once()
    assert [e[0] for e in watch.events] == ["begin", "tick_start"]  # route 2 ran unwatched
    runner.finish(executor, "route_1")
    executor.run_once()
    assert [e[0] for e in watch.events] == ["begin", "tick_start", "tick_stop", "end"]
    assert executor._watched is None


# -- the config value --------------------------------------------------------------------------


def _private_config(tmp_path):
    tmp_path.chmod(0o700)
    for name in ("state", "cred", "work"):
        (tmp_path / name).mkdir(mode=0o700)
    for name in ("w", "t", "ca", "profiles"):
        (tmp_path / name).write_text("x")
        (tmp_path / name).chmod(0o600)
    return {
        "schema": CONFIG_SCHEMA,
        "state_dir": str(tmp_path / "state"),
        "profiles_file": str(tmp_path / "profiles"),
        "profile": "gpu-main",
        "worker_credential_file": str(tmp_path / "w"),
        "credential_directory": str(tmp_path / "cred"),
        "guardian_socket": "/run/guardian.sock",
        "guardian_uid": 1000,
        "custody": dict(custody(), token_file=str(tmp_path / "t"), ca_file=str(tmp_path / "ca")),
        "work_directory": str(tmp_path / "work"),
        "source_revision": installed_source_pin("a" * 40),
        "poll_seconds": 5,
    }


def test_max_concurrent_paid_routes_is_an_integer_from_one_to_four(tmp_path):
    document = _private_config(tmp_path)
    path = tmp_path / "config.json"

    def load(value):
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return load_config(path)

    assert load(document).max_concurrent_paid_routes == 1
    for good in (1, 2, 3, 4):
        assert (
            load(dict(document, max_concurrent_paid_routes=good)).max_concurrent_paid_routes == good
        )
    for bad in (0, 5, -1, True, False, "2", None, 2.0, [2]):
        with pytest.raises(ValueError):
            load(dict(document, max_concurrent_paid_routes=bad))
        with pytest.raises(ValidationError):
            ExecutorConfig.model_validate(dict(document, max_concurrent_paid_routes=bad))
