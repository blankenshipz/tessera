"""A recorded refusal event carries exactly five fields and one stable code, never the text."""

from __future__ import annotations

import json

import pytest
from test_stage_rejections import SECRET, _events, world  # noqa: F401  (fixture)

from tessera_control.stage_rejections import record
from tessera_control.store import Conflict, Forbidden, NotFound, StoreError

KEYS = {"campaign_id", "stage_key", "attempt", "step", "code"}


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (Conflict(f"Worker heartbeat is stale. {SECRET}"), "stale_heartbeat"),
        (Conflict(SECRET), "refused_conflict"),
        (Forbidden(SECRET), "refused_forbidden"),
        (NotFound(SECRET), "refused_not_found"),
        (StoreError(SECRET), "refused"),
        (ValueError(SECRET), "unexpected_error"),
    ],
)
def test_the_recorded_event_has_the_exact_key_set_and_code_and_leaks_nothing(world, error, code):  # noqa: F811
    env, campaign, _ = world
    assert record(env.store, env.worker, "att_rej", "lease", error) is True
    (event,) = _events(env)
    assert set(event) == KEYS
    assert event["code"] == code
    assert event["campaign_id"] == campaign["id"]
    for part in ("host.example.invalid", "10.0.0.9", "secret-token"):
        assert part not in json.dumps(event)
