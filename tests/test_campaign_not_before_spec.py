"""``not_before`` in a v2 spec (G-06): shape, digest and the approve-time bound."""

from __future__ import annotations

import json

import pytest
from campaign_fixture import Env
from pydantic import ValidationError
from test_campaign_models_v2 import _spec2, _stage2

from tessera_control import campaign_provider_worker as worker
from tessera_control.campaign_hold import not_before_of, waiting
from tessera_control.campaign_models import normalize, parse_spec, spec_digest
from tessera_control.store import Conflict

WHEN = "2030-01-02T03:04:05Z"


def test_absent_not_before_keeps_the_digest_and_the_dump():
    spec = parse_spec(_spec2(_stage2("a")))
    assert "not_before" not in spec.model_dump(mode="json")
    assert normalize(spec).not_before is None


def test_present_not_before_is_part_of_the_digest():
    plain = parse_spec(_spec2(_stage2("a")))
    held = parse_spec(_spec2(_stage2("a"), not_before=WHEN))
    later = parse_spec(_spec2(_stage2("a"), not_before="2030-01-02T03:04:06Z"))
    assert held.model_dump(mode="json")["not_before"] == WHEN
    assert len({spec_digest(plain), spec_digest(held), spec_digest(later)}) == 3
    assert normalize(held).not_before == WHEN


@pytest.mark.parametrize(
    "value",
    [
        "2030-01-02 03:04:05Z",
        "2030-01-02T03:04:05",  # no zone
        "2030-01-02T03:04:05+00:00",  # not the canonical form
        "2030-01-02T03:04:05.5Z",
        "2030-13-02T03:04:05Z",  # not a real month
        "2030-02-30T03:04:05Z",
        "tomorrow",
        "",
        20300102,
    ],
)
def test_a_malformed_or_unreal_time_is_refused(value):
    with pytest.raises(ValidationError):
        parse_spec(_spec2(_stage2("a"), not_before=value))


def test_a_version_one_spec_does_not_take_it():
    v1 = {
        "version": 1,
        "name": "c",
        "budget_cap_cents": 10,
        "deadline_seconds": 3600,
        "stages": [],
        "not_before": WHEN,
    }
    with pytest.raises(ValidationError):
        parse_spec(v1)


def test_the_hold_helper_reads_only_a_well_formed_time():
    assert not_before_of(json.dumps({"not_before": WHEN})) is not None
    for junk in (None, "not json", "[]", json.dumps({"not_before": 5}), {"version": 1}):
        assert not_before_of(junk) is None
    assert waiting(json.dumps({"not_before": "garbage"}), "running", None) is None


@pytest.fixture
def env(tmp_path):
    env = Env(tmp_path)
    worker.register_from_config({"local_docker": {"worker_principal_id": env.worker["id"]}})
    env.trust()
    yield env
    worker.register_from_config(None)


def _v2(env, **extra):
    stage = {
        "key": "s1",
        "provider": "local-docker",
        "runtime": "docker-cpu-v1",
        "image": "python@sha256:" + "a" * 64,
        "argv": ["python", "-c", "print(1)"],
        "max_duration_seconds": 60,
        "inputs": [
            {
                "kind": "snapshot",
                "name": "code",
                "snapshot_id": env.snapshot_id,
                "digest": env.snapshot_digest,
                "data_class": "public",
            }
        ],
        "outputs": [{"name": "result", "path": "data.json", "max_bytes": 1024}],
    }
    return _spec2(stage, **extra)


def test_approve_refuses_a_start_time_the_approval_cannot_reach(env):
    # The approval lasts deadline_seconds (3600), so a start a day away could never run.
    submitted = env.submit(spec=_v2(env, not_before="2999-01-01T00:00:00Z"))
    with pytest.raises(Conflict, match="could never start"):
        env.approve(submitted)


def test_approve_accepts_a_start_time_inside_the_approval(env):
    from datetime import UTC, datetime, timedelta

    soon = (datetime.now(UTC) + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    approved = env.approve(env.submit(spec=_v2(env, not_before=soon)))
    assert approved["status"] == "running"
    stored = env.store.get_campaign(env.owner, approved["id"])
    assert stored["spec"]["not_before"] == soon
