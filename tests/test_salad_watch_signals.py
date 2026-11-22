from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tessera_control import salad_watch_signals as sw

NOW = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone(timedelta(hours=1)))


def snap(status, *, polls=3, term=False, stop=False, pstop=False):
    return sw.signals_from_poll(
        provider_status=status,
        termination_confirmed=term,
        stop_confirmed=stop,
        provider_stop_confirmed=pstop,
        poll_count=polls,
        now=NOW,
    )


@pytest.mark.parametrize(
    ("status", "term", "stop", "pstop", "polls", "expected"),
    [
        ("running", False, False, False, 3, "running"),
        ("running", False, False, False, 0, "running"),
        ("stopped", True, False, False, 3, "exited"),
        ("stopped", False, False, False, 3, "exited"),
        ("succeeded", False, False, False, 3, "exited"),
        ("failed", False, False, False, 3, "exited"),
        ("running", True, True, False, 3, "exited"),
        ("running", True, False, True, 3, "exited"),
        ("running", True, False, False, 3, "exited"),
        ("running", True, False, False, 1, "exited"),
        ("running", True, False, False, 0, "running"),
        ("creating", True, False, False, 3, "exited"),
        ("creating", True, False, False, 0, "unknown"),
        ("running", False, False, False, 3, "running"),
        ("stopped", False, False, False, 0, "unknown"),
        ("stopped", False, True, False, 0, "unknown"),
        ("deleted", False, False, False, 3, "absent"),
        ("deleted", True, True, True, 3, "absent"),
        ("unknown", False, False, False, 3, "unknown"),
        ("creating", False, False, False, 3, "unknown"),
        ("pending", False, False, False, 3, "unknown"),
        (None, False, False, False, 3, "unknown"),
        (7, False, False, False, 3, "unknown"),
    ],
)
def test_state_table(status, term, stop, pstop, polls, expected):
    assert snap(status, polls=polls, term=term, stop=stop, pstop=pstop)["state"] == expected


@pytest.mark.parametrize("polls", [0, 1, 17])
def test_marker_equals_poll_count(polls):
    assert snap("running", polls=polls)["progress_marker"] == polls


def test_closed_keys_and_value_types():
    out = snap("running", polls=5)
    assert set(out) == {"schema", "observed_at", "state", "progress_marker"}
    assert out["schema"] == "tessera.stage-watch.signals.v1"
    assert out["observed_at"] == "2026-11-22T12:00:00+01:00"
    assert "exit_code" not in out
    strings = {k for k, v in out.items() if isinstance(v, str)}
    assert strings == {"schema", "observed_at", "state"}
    assert all(not isinstance(v, (float, bool)) for v in out.values())


def test_bad_poll_count_omits_marker():
    out = sw.signals_from_poll(provider_status="running", poll_count=True, now=NOW)  # type: ignore[arg-type]
    assert "progress_marker" not in out
    assert out["state"] == "running"


def test_naive_now_rejected():
    with pytest.raises(ValueError):
        sw.signals_from_poll(provider_status="running", poll_count=1, now=datetime(2026, 1, 1))


def test_no_financial_input_accepted():
    with pytest.raises(TypeError):
        sw.signals_from_poll(  # type: ignore[call-arg]
            provider_status="running", poll_count=1, now=NOW, estimated_exposure_cents=5
        )


def test_accepted_by_worker_validator():
    from tessera_worker.stage_watcher.signals import validate_signals

    for status in ("running", "stopped", "deleted", "unknown"):
        validate_signals(snap(status))
