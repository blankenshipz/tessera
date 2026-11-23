"""A live heavy local attempt whose only reason is no_remote_profile fires the existing
heavy_stage_on_local finding, marked as a configuration gap (no new code, no migration)."""

from __future__ import annotations

import pytest
from test_heavy_stage_audit import EXPLAINED, NOW, World  # noqa: F401

from tessera_control.store import _connect


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_no_remote_profile_only_fires_as_a_config_gap(world):
    world.attempt(world.first, "running")
    world.warn(world.first, ["no_remote_profile"])
    (finding,) = world.scan_all()
    assert finding["code"] == "heavy_stage_on_local"
    assert finding["evidence"]["config_gap"] == "no_remote_profile"
    assert finding["evidence"]["reasons"] == ["no_remote_profile"]
    assert "explained" in finding["evidence"]
    assert finding["suggested_action"] == "configure_a_salad_profile"


def test_an_explained_reason_has_no_config_gap(world):
    world.attempt(world.first, "running")
    world.warn(world.first, [EXPLAINED])
    (finding,) = world.scan_all()
    assert finding["code"] == "heavy_stage_on_local"
    assert "config_gap" not in finding["evidence"]
    assert finding["suggested_action"] == "check_salad_credential_or_profile"


def test_no_remote_profile_with_another_reason_is_not_a_config_gap(world):
    world.attempt(world.first, "running")
    world.warn(world.first, ["no_remote_profile", EXPLAINED])
    (finding,) = world.scan_all()
    assert "config_gap" not in finding["evidence"]


def test_finished_attempt_and_light_stage_never_fire(world):
    world.warn(world.first, ["no_remote_profile"])
    world.attempt(world.first, "succeeded")
    assert world.scan_all() == []
    other = world.second()
    world.attempt(other, "running")  # light: no heavy fallback event
    assert world.scan_all() == []


def test_repeat_scan_does_not_duplicate(world):
    world.attempt(world.first, "running")
    world.warn(world.first, ["no_remote_profile"])
    first = world.scan_all()
    second = world.scan_all()
    assert len(first) == len(second) == 1
    assert first[0]["id"] == second[0]["id"]
    connection = _connect(world.db)
    try:
        rows = connection.execute("SELECT code FROM campaign_stuck_findings").fetchall()
    finally:
        connection.close()
    assert [r["code"] for r in rows] == ["heavy_stage_on_local"]
