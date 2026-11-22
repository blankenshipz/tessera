"""A worker-built watch report is exactly what the control route accepts (and nothing looser)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from tessera_control.stage_watch_models import ReportStageWatch
from tessera_worker.stage_watcher.report import BODY_KEYS, COUNTER_KEYS, MAX_COUNTER, WatchReport

OPERATION = "a" * 32
DIGEST = "b" * 64


def _report(kind="progress", counters=None, **over):
    values = {
        "operation_id": OPERATION,
        "lease_id": "lease_" + "c" * 20,
        "epoch": 1,
        "kind": kind,
        "observed_at": datetime(2026, 12, 2, 12, 0, tzinfo=UTC),
        "seq": 0,
        "counters": {"elapsed_s": 5, "silent_s": 0, "restarts": 0}
        if counters is None
        else counters,
        "kill_switch_engaged": False,
        "contract_digest": DIGEST,
    }
    return WatchReport(**{**values, **over})


@pytest.mark.parametrize("kind", ["heartbeat", "progress", "stuck", "failed"])
def test_every_worker_body_validates_against_the_control_model(kind):
    body = _report(kind).to_body()
    assert set(body) == BODY_KEYS
    parsed = ReportStageWatch.model_validate(body)
    assert parsed.kind == kind and parsed.expected_epoch == 1


def test_all_counter_keys_and_the_maximum_value_are_accepted_by_control():
    body = _report(counters={key: MAX_COUNTER for key in COUNTER_KEYS}).to_body()
    parsed = ReportStageWatch.model_validate(body)
    assert parsed.counters.model_dump(exclude_none=True) == {k: MAX_COUNTER for k in COUNTER_KEYS}


def test_the_control_model_refuses_what_the_worker_never_builds():
    body = _report().to_body()
    for bad in (
        {**body, "note": "free text"},
        {**body, "counters": {"log": 1}},
        {**body, "kind": "done"},
    ):
        with pytest.raises(ValidationError):
            ReportStageWatch.model_validate(bad)
