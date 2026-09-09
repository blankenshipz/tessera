from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tessera_worker.cpu import CpuError, CpuObservation, CpuOperation
from tessera_worker.supervisor import CpuSupervisor, JournalLockedError, Lease, SupervisorError

IMAGE = "python@sha256:" + "a" * 64
OPERATION = CpuOperation("b" * 32, IMAGE, ("python", "-c", "print(2)"), "c" * 64, 10)


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


class MonotonicClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class FakeAdapter:
    def __init__(self) -> None:
        self.state = "absent"
        self.calls: list[str] = []
        self.lose_start_reply = False
        self.crash_before_start = False
        self.cancel_unknown = False

    def prepare(self, operation: CpuOperation) -> CpuObservation:
        self.calls.append("prepare")
        if self.state == "absent":
            self.state = "created"
        return self.observe(operation)

    def start(self, operation: CpuOperation) -> CpuObservation:
        self.calls.append("start")
        if self.crash_before_start:
            raise CpuError("driver crashed before start")
        if self.lose_start_reply:
            self.state = "exited"
            raise CpuError("lost start reply")
        if self.state == "created":
            self.state = "running"
        return self.observe(operation)

    def observe(self, operation: CpuOperation) -> CpuObservation:
        self.calls.append("observe")
        return CpuObservation(
            self.state,
            "container" if self.state != "absent" else None,
            0,
            self.state == "exited",
        )

    def cancel(self, operation: CpuOperation) -> CpuObservation:
        self.calls.append("cancel")
        if self.cancel_unknown:
            return CpuObservation("unknown")
        if self.state in {"running", "created"}:
            self.state = "exited"
        return CpuObservation(self.state, "container", 0, self.state == "exited")


def lease(clock: Clock, **changes: object) -> Lease:
    values: dict[str, object] = {
        "id": "lease-1",
        "operation_id": OPERATION.operation_id,
        "plan_digest": OPERATION.plan_digest,
        "epoch": 1,
        "expires_at": clock() + timedelta(minutes=5),
        "task_revision": 1,
        "project_id": "project-1",
    }
    values.update(changes)
    return Lease(**values)  # type: ignore[arg-type]


def supervisor(
    tmp_path: Path,
    adapter: FakeAdapter,
    clock: Clock,
    valid=lambda _: True,
    monotonic: MonotonicClock | None = None,
) -> CpuSupervisor:
    return CpuSupervisor(tmp_path, adapter, valid, now=clock, monotonic=monotonic)


def events(path: Path) -> list[str]:
    with sqlite3.connect(path / "supervisor.sqlite3") as connection:
        return [row[0] for row in connection.execute("SELECT event FROM events ORDER BY id")]


def journal_value(path: Path, column: str) -> object:
    with sqlite3.connect(path / "supervisor.sqlite3") as connection:
        return connection.execute(f"SELECT {column} FROM operations").fetchone()[0]


def test_crash_between_start_intent_and_effect_never_restarts(tmp_path: Path) -> None:
    clock = Clock()
    adapter = FakeAdapter()
    first = supervisor(tmp_path, adapter, clock)
    adapter.crash_before_start = True
    assert first.drive_once(OPERATION, lease(clock)).phase == "uncertain"
    assert adapter.calls.count("start") == 1
    first.close()

    adapter.crash_before_start = False
    adapter.state = "created"
    with supervisor(tmp_path, adapter, clock) as recovered:
        result = recovered.drive_once(OPERATION, lease(clock))
    assert result.phase == "uncertain"
    assert adapter.calls.count("start") == 1
    assert "start_intent" in events(tmp_path)


def test_lost_start_reply_reconciles_observed_exit_without_a_retry(tmp_path: Path) -> None:
    clock = Clock()
    adapter = FakeAdapter()
    first = supervisor(tmp_path, adapter, clock)
    adapter.lose_start_reply = True
    assert first.drive_once(OPERATION, lease(clock)).phase == "uncertain"
    first.close()

    adapter.lose_start_reply = False
    with supervisor(tmp_path, adapter, clock) as recovered:
        result = recovered.reconcile(OPERATION, lease(clock))
    assert result.phase == "terminal"
    assert adapter.calls.count("start") == 1


def test_terminal_exit_is_persisted_and_not_restarted(tmp_path: Path) -> None:
    clock = Clock()
    adapter = FakeAdapter()
    with supervisor(tmp_path, adapter, clock) as driver:
        assert driver.drive_once(OPERATION, lease(clock)).phase == "running"
        adapter.state = "exited"
        assert driver.tick(OPERATION, lease(clock)).phase == "terminal"
        assert driver.drive_once(OPERATION, lease(clock)).phase == "terminal"
    assert adapter.calls.count("start") == 1


def test_terminal_recovery_uses_a_fresh_read_only_observation_without_restart(
    tmp_path: Path,
) -> None:
    clock = Clock()
    adapter = FakeAdapter()
    with supervisor(tmp_path, adapter, clock) as driver:
        assert driver.drive_once(OPERATION, lease(clock)).phase == "running"
        adapter.state = "exited"
        assert driver.tick(OPERATION, lease(clock)).phase == "terminal"
    observed_before = adapter.calls.count("observe")
    with supervisor(tmp_path, adapter, clock) as recovered:
        result = recovered.drive_once(OPERATION, lease(clock))
    assert result.phase == "terminal"
    assert adapter.calls.count("observe") == observed_before + 1
    assert adapter.calls.count("start") == 1


def test_journal_is_exclusive_and_payload_or_authority_changes_are_denied(tmp_path: Path) -> None:
    clock = Clock()
    adapter = FakeAdapter()
    first = supervisor(tmp_path, adapter, clock)
    with pytest.raises(JournalLockedError):
        supervisor(tmp_path, FakeAdapter(), clock)
    assert first.drive_once(OPERATION, lease(clock)).phase == "running"
    with pytest.raises(SupervisorError, match="authority epoch"):
        first.tick(OPERATION, lease(clock, epoch=2))
    changed = CpuOperation(
        OPERATION.operation_id, IMAGE, ("python", "-c", "print(3)"), "c" * 64, 10
    )
    with pytest.raises(SupervisorError, match="payload digest"):
        first.reconcile(changed, lease(clock))
    with pytest.raises(SupervisorError, match="payload digest"):
        first.cancel(changed)
    first.close()


def test_stale_and_validation_failures_create_no_external_work(tmp_path: Path) -> None:
    clock = Clock()
    adapter = FakeAdapter()
    with supervisor(tmp_path, adapter, clock, lambda _: False) as driver:
        assert driver.drive_once(OPERATION, lease(clock)).phase == "denied"
    assert adapter.calls == []

    adapter = FakeAdapter()

    def unavailable(_: Lease) -> bool:
        raise RuntimeError("control unavailable")

    with supervisor(tmp_path / "other", adapter, clock, unavailable) as driver:
        assert driver.drive_once(OPERATION, lease(clock)).reason == "lease_denied"
    assert adapter.calls == []


def test_expiry_and_duration_cancel_only_after_provider_confirmation(tmp_path: Path) -> None:
    clock = Clock()
    adapter = FakeAdapter()
    with supervisor(tmp_path, adapter, clock) as driver:
        active_lease = lease(clock)
        assert driver.drive_once(OPERATION, active_lease).phase == "running"
        adapter.cancel_unknown = True
        clock.value += timedelta(minutes=6)
        result = driver.tick(OPERATION, active_lease)
        assert result.phase == "uncertain"
        assert "cancel_lease_expired_unconfirmed" == result.reason
        adapter.cancel_unknown = False
        result = driver.cancel(OPERATION, reason="reconcile")
        assert result.phase == "terminal"
        assert result.reason == "cancelled_reconcile"


def test_duration_uses_persisted_start_intent_and_never_a_renewed_expiry(tmp_path: Path) -> None:
    clock = Clock()
    adapter = FakeAdapter()
    with supervisor(tmp_path, adapter, clock) as driver:
        assert driver.drive_once(OPERATION, lease(clock)).phase == "running"
        clock.value += timedelta(seconds=11)
        result = driver.tick(OPERATION, lease(clock, expires_at=clock() + timedelta(days=1)))
    # Lease expiry changes are not authority changes; duration is still anchored
    # at the original persisted intent and requests termination.
    assert result.phase == "terminal"
    assert result.reason == "cancelled_duration_elapsed"


def test_monotonic_duration_fences_backwards_wall_clock_and_renewal(tmp_path: Path) -> None:
    clock = Clock()
    monotonic = MonotonicClock()
    adapter = FakeAdapter()
    with supervisor(tmp_path, adapter, clock, monotonic=monotonic) as driver:
        active_lease = lease(clock)
        assert driver.drive_once(OPERATION, active_lease).phase == "running"
        # A lease renewal cannot reset the frozen operation duration, and a
        # backwards wall clock cannot extend it within this live supervisor.
        clock.value -= timedelta(days=1)
        monotonic.value += 9
        assert (
            driver.tick(OPERATION, lease(clock, expires_at=clock() + timedelta(days=1))).phase
            == "running"
        )
        monotonic.value += 2
        result = driver.tick(OPERATION, lease(clock, expires_at=clock() + timedelta(days=1)))
    assert result.phase == "terminal"
    assert result.reason == "cancelled_duration_elapsed"


def test_recovery_clock_rollback_requests_cancellation_and_preserves_unknown(
    tmp_path: Path,
) -> None:
    clock = Clock()
    adapter = FakeAdapter()
    first = supervisor(tmp_path, adapter, clock)
    active_lease = lease(clock)
    assert first.drive_once(OPERATION, active_lease).phase == "running"
    # Persist a later high-water mark than the original start intent.
    clock.value += timedelta(seconds=5)
    assert first.tick(OPERATION, active_lease).phase == "running"
    first.close()

    # A restarted process can compare its wall clock with durable operation
    # timestamps even though the previous process's monotonic clock is gone.
    clock.value -= timedelta(seconds=3)
    adapter.cancel_unknown = True
    with supervisor(tmp_path, adapter, clock) as recovered:
        result = recovered.reconcile(OPERATION, lease(clock))
        repeated = recovered.tick(OPERATION, lease(clock))
    assert result.phase == "uncertain"
    assert result.reason == "cancel_clock_rollback_unconfirmed"
    assert result.observation.state == "unknown"
    assert repeated.reason == "cancel_clock_rollback_unconfirmed"
    with supervisor(tmp_path, adapter, clock) as restarted:
        after_restart = restarted.reconcile(OPERATION, lease(clock))
    assert after_restart.reason == "cancel_clock_rollback_unconfirmed"
    assert adapter.calls.count("cancel") == 3


def test_concurrent_drive_once_has_one_start_intent_and_one_start(tmp_path: Path) -> None:
    clock = Clock()
    adapter = FakeAdapter()
    driver = supervisor(tmp_path, adapter, clock)
    barrier = threading.Barrier(3)

    def drive() -> str:
        barrier.wait()
        return driver.drive_once(OPERATION, lease(clock)).phase

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(drive), pool.submit(drive)]
        barrier.wait()
        assert [future.result() for future in futures] == ["running", "running"]
    first_started_at = journal_value(tmp_path, "started_at")
    assert driver.tick(OPERATION, lease(clock)).phase == "running"
    assert journal_value(tmp_path, "started_at") == first_started_at
    assert adapter.calls.count("start") == 1
    assert events(tmp_path).count("start_intent") == 1
    driver.close()


def test_reconcile_revocation_and_overdue_start_intent_cancel(tmp_path: Path) -> None:
    clock = Clock()
    adapter = FakeAdapter()
    allowed = [True]
    active_lease = lease(clock)
    with supervisor(tmp_path, adapter, clock, lambda _: allowed[0]) as driver:
        assert driver.drive_once(OPERATION, active_lease).phase == "running"
        allowed[0] = False
        result = driver.reconcile(OPERATION, active_lease)
        assert result.phase == "terminal"
        assert result.reason == "cancelled_lease_denied"

    clock = Clock()
    adapter = FakeAdapter()
    first = supervisor(tmp_path / "overdue", adapter, clock)
    adapter.crash_before_start = True
    active_lease = lease(clock)
    assert first.drive_once(OPERATION, active_lease).phase == "uncertain"
    first.close()
    adapter.crash_before_start = False
    clock.value += timedelta(seconds=11)
    with supervisor(tmp_path / "overdue", adapter, clock) as recovered:
        result = recovered.reconcile(OPERATION, active_lease)
    assert result.phase == "terminal"
    assert result.reason == "cancelled_duration_elapsed"
    assert adapter.calls.count("start") == 1


def test_watchdog_is_bounded_checks_time_and_returns_unknown_after_grace(tmp_path: Path) -> None:
    clock = Clock()
    monotonic = MonotonicClock()
    adapter = FakeAdapter()
    adapter.cancel_unknown = True
    active_lease = lease(clock, expires_at=clock() + timedelta(seconds=2))
    validations = 0

    def validate(_: Lease) -> bool:
        nonlocal validations
        validations += 1
        return True

    def sleep(seconds: float) -> None:
        clock.value += timedelta(seconds=seconds)
        monotonic.value += seconds

    with supervisor(tmp_path, adapter, clock, validate, monotonic) as driver:
        result = driver.run_until_terminal(
            OPERATION,
            active_lease,
            poll_interval_seconds=1,
            cancellation_grace_seconds=2,
            max_ticks=5,
            sleep=sleep,
        )
    assert result.phase == "uncertain"
    assert result.reason == "cancellation_grace_elapsed"
    assert validations >= 3
    assert adapter.calls.count("cancel") >= 1


def test_watchdog_tick_limit_attempts_cancellation_and_preserves_unknown(tmp_path: Path) -> None:
    clock = Clock()
    adapter = FakeAdapter()
    with supervisor(tmp_path / "confirmed", adapter, clock) as driver:
        confirmed = driver.run_until_terminal(
            OPERATION,
            lease(clock),
            poll_interval_seconds=1,
            cancellation_grace_seconds=0,
            max_ticks=1,
            sleep=lambda _: None,
        )
    assert confirmed.phase == "terminal"
    assert confirmed.reason == "cancelled_watchdog_tick_limit"
    assert adapter.calls.count("cancel") == 1

    adapter = FakeAdapter()
    adapter.cancel_unknown = True
    with supervisor(tmp_path / "unconfirmed", adapter, clock) as driver:
        unconfirmed = driver.run_until_terminal(
            OPERATION,
            lease(clock),
            poll_interval_seconds=1,
            cancellation_grace_seconds=0,
            max_ticks=1,
            sleep=lambda _: None,
        )
    assert unconfirmed.phase == "uncertain"
    assert unconfirmed.reason == "cancel_watchdog_tick_limit_unconfirmed"
    assert unconfirmed.observation.state == "unknown"
    assert adapter.calls.count("cancel") == 1


def test_closed_supervisor_cannot_drive_after_releasing_journal_lock(tmp_path: Path) -> None:
    clock = Clock()
    adapter = FakeAdapter()
    driver = supervisor(tmp_path, adapter, clock)
    driver.close()
    with pytest.raises(SupervisorError, match="closed"):
        driver.drive_once(OPERATION, lease(clock))
    with pytest.raises(SupervisorError, match="closed"):
        driver.reconcile(OPERATION, lease(clock))
    with pytest.raises(SupervisorError, match="closed"):
        driver.tick(OPERATION, lease(clock))
    with pytest.raises(SupervisorError, match="closed"):
        driver.cancel(OPERATION)
    with pytest.raises(SupervisorError, match="closed"):
        driver.run_until_terminal(
            OPERATION,
            lease(clock),
            poll_interval_seconds=1,
            cancellation_grace_seconds=1,
            max_ticks=1,
        )
    with supervisor(tmp_path, adapter, clock):
        pass
