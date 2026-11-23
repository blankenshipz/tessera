"""The maintenance lock: the probe, the process-lifetime shared hold, and who takes it."""

from __future__ import annotations

import fcntl
import os
import stat

import pytest

from tessera_control import campaign_salad_executor as executor_module
from tessera_control import maintenance_lock
from tessera_control.maintenance_lock import (
    MAINTENANCE_LOCK_NAME,
    MaintenanceBusy,
    exclusive,
    hold_shared_for_process,
    probe_exclusive,
    shared,
)


@pytest.fixture(autouse=True)
def _release():
    yield
    maintenance_lock._release_process_locks()


@pytest.fixture
def state(tmp_path):
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    return directory


def _other_holder(state, mode):
    """A second open file description: under flock it conflicts exactly like another process."""
    descriptor = os.open(state / MAINTENANCE_LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
    return descriptor


def _open_descriptors():
    return len(os.listdir("/proc/self/fd"))


# -- probe_exclusive --------------------------------------------------------------------------


def test_probe_creates_nothing_when_the_lock_file_is_absent(state):
    probe_exclusive(state)
    assert list(state.iterdir()) == []


def test_probe_raises_while_a_shared_lock_is_held_and_is_silent_when_free(state):
    with shared(state):
        before = (state / MAINTENANCE_LOCK_NAME).stat()
        with pytest.raises(MaintenanceBusy):
            probe_exclusive(state)
        after = (state / MAINTENANCE_LOCK_NAME).stat()
    assert (before.st_mtime_ns, before.st_size) == (after.st_mtime_ns, after.st_size)
    probe_exclusive(state)
    with exclusive(state):  # the probe released what it took
        pass


def test_probe_raises_while_another_holder_has_the_exclusive_lock(state):
    descriptor = _other_holder(state, fcntl.LOCK_EX)
    try:
        with pytest.raises(MaintenanceBusy):
            probe_exclusive(state)
    finally:
        os.close(descriptor)


def test_probe_passes_inside_this_process_own_exclusive_hold(state):
    with exclusive(state):
        probe_exclusive(state)


def test_probe_never_follows_a_symlinked_lock_file(state, tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("")
    (state / MAINTENANCE_LOCK_NAME).symlink_to(target)
    with pytest.raises(OSError):
        probe_exclusive(state)


# -- hold_shared_for_process ------------------------------------------------------------------


def test_hold_is_idempotent_with_one_descriptor_and_a_private_file(state):
    hold_shared_for_process(state)
    after_first = _open_descriptors()
    for _ in range(5):
        hold_shared_for_process(state)
        hold_shared_for_process(str(state) + "/.")
    assert _open_descriptors() == after_first
    assert len(maintenance_lock._shared_for_process) == 1
    assert stat.S_IMODE((state / MAINTENANCE_LOCK_NAME).stat().st_mode) == 0o600
    with pytest.raises(MaintenanceBusy):
        with exclusive(state):
            pass
    with shared(state):  # services share
        pass


def test_hold_refuses_while_another_holder_has_the_exclusive_lock(state):
    descriptor = _other_holder(state, fcntl.LOCK_EX)
    try:
        before = _open_descriptors()
        with pytest.raises(MaintenanceBusy):
            hold_shared_for_process(state)
        assert _open_descriptors() == before
        assert maintenance_lock._shared_for_process == {}
    finally:
        os.close(descriptor)
    hold_shared_for_process(state)


def test_hold_is_a_no_op_while_this_process_holds_the_exclusive_lock(state):
    with exclusive(state):
        hold_shared_for_process(state)
        assert maintenance_lock._shared_for_process == {}
    with exclusive(state):  # nothing was left behind
        pass


# -- the Salad campaign executor --------------------------------------------------------------


def _executor_main(monkeypatch, state, build):
    class Config:
        state_dir = state

    monkeypatch.setattr(executor_module, "load_config", lambda path: Config())
    monkeypatch.setattr(executor_module, "build_executor", build)
    return executor_module.main(["--config", "x", "--once"])


def test_executor_startup_takes_the_shared_lock_before_opening_the_database(monkeypatch, state):
    seen = []

    def build(config):
        with pytest.raises(MaintenanceBusy):
            with exclusive(state):
                pass
        seen.append("built")
        raise RuntimeError("stop here")

    assert _executor_main(monkeypatch, state, build) == 2
    assert seen == ["built"]
    with pytest.raises(MaintenanceBusy):  # held for the process lifetime
        with exclusive(state):
            pass


def test_a_running_compaction_makes_executor_startup_exit_1(monkeypatch, state, capsys):
    def build(config):
        raise AssertionError("the database must not be opened")

    descriptor = _other_holder(state, fcntl.LOCK_EX)
    try:
        assert _executor_main(monkeypatch, state, build) == 1
    finally:
        os.close(descriptor)
    error = capsys.readouterr().err.strip()
    assert error.startswith("salad executor refused:") and "\n" not in error
    assert maintenance_lock._shared_for_process == {}
