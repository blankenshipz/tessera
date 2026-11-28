"""Unknown filesystem errors fail closed; a broken state directory is the host's (ADR 0210).

An ``OSError`` whose errno is not on the short transient list (or that has none) is
deterministic: raised on every pass at a stage, a provider hook or the drive's preamble, it
blocks the campaign within ``RUNNER_ERROR_THRESHOLD`` passes with one breaker event, while a
sibling stage keeps advancing. A missing or unreadable state directory raises the host alarm
and fails no stage. A cancelling campaign is never backed off. Real stores and runner on
temporary state; errors are simulated by patching one runner method or provider hook.
"""

from __future__ import annotations

import errno
from urllib.error import URLError

import pytest
from campaign_runner_harness import World
from test_runner_error_breaker import _reserved
from test_runner_error_classes import (
    _raising,
    _slot,
    _step_world,
    _world,
)

from tessera_control.campaign_models import stage_provider
from tessera_control.campaign_runner import (
    CANCEL_RUNNER_ERROR_EVENT,
    RUNNER_ERROR_EVENT,
    RUNNER_ERROR_THRESHOLD,
    STUCK_RUNNER_ERROR_EVENT,
    CampaignRunner,
)
from tessera_control.campaign_steps import StepFailed
from tessera_control.runner_faults import (
    DETERMINISTIC,
    ENVIRONMENT,
    HOST_ALARM,
    TRANSIENT,
    MissingBlob,
    classify,
    is_missing_blob,
)

PROVIDER = type(stage_provider("local-docker"))
BREAKER_EVENTS = (RUNNER_ERROR_EVENT, STUCK_RUNNER_ERROR_EVENT, CANCEL_RUNNER_ERROR_EVENT)


@pytest.fixture(autouse=True)
def _clean_alarm():
    HOST_ALARM.clear()
    yield
    HOST_ALARM.clear()


def _status(world: World, cid: str) -> dict:
    return dict(world.row("SELECT status, stop_reason FROM campaigns WHERE id=?", cid))


def _breaker_events(world: World) -> int:
    return sum(len(world.events_of(name)) for name in BREAKER_EVENTS)


def _eexist(message: str = "synthetic") -> OSError:
    return FileExistsError(errno.EEXIST, message)


# -- the table ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (OSError("no errno"), DETERMINISTIC),
        (OSError(), DETERMINISTIC),
        (URLError("unreachable"), DETERMINISTIC),
        (FileExistsError(errno.EEXIST, "exists"), DETERMINISTIC),
        (OSError(errno.EXDEV, "cross-device"), DETERMINISTIC),
        (OSError(errno.EINVAL, "invalid"), DETERMINISTIC),
        (OSError(errno.ENOTEMPTY, "not empty"), DETERMINISTIC),
        (OSError(errno.EAGAIN, "again"), TRANSIENT),
        (OSError(errno.EINTR, "interrupted"), TRANSIENT),
        (OSError(errno.EBUSY, "busy"), TRANSIENT),
        (OSError(errno.ETIMEDOUT, "timed out"), TRANSIENT),
        (ConnectionResetError(errno.ECONNRESET, "reset"), TRANSIENT),
        (OSError(errno.ENOSPC, "full"), ENVIRONMENT),
        (OSError(errno.EROFS, "read-only"), ENVIRONMENT),
    ],
)
def test_only_listed_errnos_are_transient(error, expected):
    assert classify(error) == expected


def test_a_broken_state_root_is_the_hosts_and_one_missing_file_is_not(tmp_path):
    state = tmp_path / "state"
    (state / "artifacts" / "ab").mkdir(parents=True)
    deep = state / "artifacts" / "ab" / "file"
    sha = state / "artifacts" / "ab" / ("ab" * 32)
    # The root, an ancestor of it, or one of its top-level entries: the host's.
    assert classify(PermissionError(errno.EACCES, "denied", str(state)), state) == ENVIRONMENT
    assert classify(PermissionError(errno.EPERM, "denied", str(tmp_path)), state) == ENVIRONMENT
    assert (
        classify(FileNotFoundError(errno.ENOENT, "gone", str(state / "artifacts")), state)
        == ENVIRONMENT
    )
    # A root that is missing (an unmounted volume) makes any access error the host's.
    missing = tmp_path / "unmounted"
    assert (
        classify(FileNotFoundError(errno.ENOENT, "gone", str(missing / "a" / "b")), missing)
        == ENVIRONMENT
    )
    assert classify(PermissionError(errno.EACCES, "denied"), missing) == ENVIRONMENT
    # One file deep inside a usable root: deterministic, and only a sha-named one is a blob.
    assert classify(FileNotFoundError(errno.ENOENT, "gone", str(deep)), state) == DETERMINISTIC
    assert not is_missing_blob(FileNotFoundError(errno.ENOENT, "gone", str(deep)))
    assert is_missing_blob(FileNotFoundError(errno.ENOENT, "gone", str(sha)))
    assert is_missing_blob(MissingBlob("ab" * 32))
    assert classify(MissingBlob("ab" * 32, str(missing / "x")), missing) == DETERMINISTIC
    assert not is_missing_blob(PermissionError(errno.EACCES, "denied", str(sha)))


# -- forward steps -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error", [_eexist(), OSError("no errno"), URLError("unreachable")], ids=type
)
def test_an_unknown_error_in_a_forward_step_fails_its_intent_once(tmp_path, monkeypatch, error):
    world, cid, kwargs = _step_world(tmp_path, monkeypatch)
    calls = _raising(monkeypatch, error)
    with pytest.raises(StepFailed) as raised:
        world.runner.steps.run(**kwargs)
    assert raised.value.error_class == type(error).__name__
    assert _slot(world, cid)["status"] == "failed"
    with pytest.raises(StepFailed):
        world.runner.steps.run(**kwargs)
    assert len(calls) == 1


# -- outside a step: a stage, a hook, the preamble ------------------------------------------


def _always_in_stage(monkeypatch, key: str, make) -> list[int]:
    original = CampaignRunner._advance_stage
    calls: list[int] = []

    def raising(self, ctx, stage_key):
        if stage_key != key:
            return original(self, ctx, stage_key)
        calls.append(1)
        raise make()

    monkeypatch.setattr(CampaignRunner, "_advance_stage", raising)
    return calls


def _assert_blocked_once(world: World, cid: str, error_class: str) -> None:
    reports = []
    for _ in range(RUNNER_ERROR_THRESHOLD):
        reports.append(world.runner.tick())
        world.poller.run(max_cycles=1)
    assert _status(world, cid) == {
        "status": "blocked",
        "stop_reason": f"runner_error:{error_class}",
    }, [r.actions for r in reports]
    assert len(world.events_of(RUNNER_ERROR_EVENT)) == 1
    for _ in range(2 * RUNNER_ERROR_THRESHOLD):
        world.runner.tick()
    assert _breaker_events(world) == 1  # one event, not one per pass
    assert HOST_ALARM.active() is None


def test_eexist_at_a_stage_every_pass_blocks_within_the_limit_and_the_sibling_advances(
    tmp_path, monkeypatch
):
    world, _, cid = _world(tmp_path, stages=2)
    calls = _always_in_stage(monkeypatch, "b", _eexist)
    _assert_blocked_once(world, cid, "FileExistsError")
    assert calls
    assert any(row["stage_key"] == "a" for row in world.attempts(cid))
    assert world.stages(cid)["b"] != "failed"  # counted toward the breaker, not failed


@pytest.mark.parametrize("hook", ["after_tick", "evidence_pass"])
def test_eexist_at_a_hook_every_pass_blocks_within_the_limit(tmp_path, monkeypatch, hook):
    world = World(tmp_path)
    cid, _ = _reserved(world)

    def raising(*_args, **_kwargs):
        raise _eexist()

    monkeypatch.setattr(PROVIDER, hook, raising, raising=False)
    _assert_blocked_once(world, cid, "FileExistsError")


@pytest.mark.parametrize(
    ("make", "error_class"),
    [
        (_eexist, "FileExistsError"),
        (lambda: OSError("no errno"), "OSError"),
        (lambda: URLError("unreachable"), "URLError"),
        # A transient errno outside a step has no replay bound: it is counted, never silent.
        (lambda: OSError(errno.EAGAIN, "again"), "BlockingIOError"),
    ],
    ids=["EEXIST", "no-errno", "URLError", "EAGAIN"],
)
def test_an_error_in_the_preamble_every_pass_blocks_within_the_limit(
    tmp_path, monkeypatch, make, error_class
):
    world = World(tmp_path)
    cid, _ = _reserved(world)

    def raising(*_args, **_kwargs):
        raise make()

    monkeypatch.setattr(CampaignRunner, "_close_unreported_exits", raising)
    _assert_blocked_once(world, cid, error_class)


# -- the state directory is the host's --------------------------------------------------------


@pytest.mark.parametrize("where", ["root", "top-level"])
def test_a_state_dir_access_error_raises_the_host_alarm_and_fails_no_stage(
    tmp_path, monkeypatch, where
):
    world, _, cid = _world(tmp_path, stages=2)
    root = world.runner.state_dir
    if where == "root":
        make = lambda: PermissionError(errno.EACCES, "denied", str(root))  # noqa: E731
    else:
        make = lambda: FileNotFoundError(errno.ENOENT, "gone", str(root / "artifacts"))  # noqa: E731
    _always_in_stage(monkeypatch, "a", make)
    report = world.runner.tick()
    assert any(action.startswith("host_fault:") for action in report.actions)
    assert HOST_ALARM.active() is not None
    for _ in range(2 * RUNNER_ERROR_THRESHOLD):
        world.runner._host_paused_until = None  # past the pause, without lapsing anything
        assert any(a.startswith("host_fault:") for a in world.runner.tick().actions)
    assert all(status != "failed" for status in world.stages(cid).values())
    assert _status(world, cid)["status"] == "running"
    assert _breaker_events(world) == 0


# -- a cancelling campaign is never backed off --------------------------------------------------


def test_a_cancelling_campaign_keeps_its_every_pass_cancel(tmp_path, monkeypatch):
    world = World(tmp_path)
    cid, _ = _reserved(world)
    world.cancel({"id": cid})
    calls: list[int] = []

    def raising(*_args, **_kwargs):
        calls.append(1)
        raise ValueError("synthetic")

    monkeypatch.setattr(CampaignRunner, "_drive_cancel", raising)
    for _ in range(RUNNER_ERROR_THRESHOLD):
        world.runner.tick()
    assert len(world.events_of(CANCEL_RUNNER_ERROR_EVENT)) == 1
    tripped = len(calls)
    for _ in range(5):
        report = world.runner.tick()
        assert f"backoff:{cid}" not in report.actions
        assert not any(action.startswith("backing_off:") for action in report.actions)
    assert len(calls) == tripped + 5
    assert cid not in world.runner._stuck_backoff
    assert len(world.events_of(CANCEL_RUNNER_ERROR_EVENT)) == 1
