"""A launch refuses when the operator provider files changed after serve read them."""

from __future__ import annotations

import json

import pytest
from salad_campaign_fixture import SaladWorld, salad_provider_reset  # noqa: F401
from tessera_control import operator_config_freshness as fresh


@pytest.fixture(autouse=True)
def _clean():
    fresh.clear()
    yield
    fresh.clear()


def _profiles_file(tmp_path, country):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({"profiles": [{"name": "cpu-2c-4g", "country": country}]}))
    return path


def test_unchanged_files_are_not_stale(tmp_path):
    path = _profiles_file(tmp_path, "GB")
    fresh.record([path, None])
    fresh.refuse_if_stale()
    assert fresh.stale_files() == []


@pytest.mark.parametrize("change", ["edit", "remove"])
def test_changed_or_removed_file_is_stale(tmp_path, change):
    path = _profiles_file(tmp_path, "US")
    fresh.record([path])
    if change == "edit":
        _profiles_file(tmp_path, "GB")
    else:
        path.unlink()
    assert fresh.stale_files() == ["profiles.json"]
    with pytest.raises(ValueError, match="operator_config_stale"):
        fresh.refuse_if_stale()


def test_nothing_recorded_never_refuses():
    fresh.refuse_if_stale()


def test_launch_is_refused_after_a_mid_process_profile_change(tmp_path):
    """The US-after-GB-edit case: the process still holds the old selection."""
    path = _profiles_file(tmp_path, "US")
    world = SaladWorld(tmp_path)
    fresh.record([path])
    _profiles_file(tmp_path, "GB")  # the operator edits the country; no restart
    world.start()
    world.tick(5)
    assert world.events("campaign.stage_operator_config_stale") != []
    stage = world.row(
        "SELECT status FROM campaign_stages WHERE campaign_id=?", world.campaign_id
    )
    assert stage["status"] == "failed"
    campaign = world.row("SELECT stop_reason FROM campaigns WHERE id=?", world.campaign_id)
    assert campaign["stop_reason"].endswith(":operator_config_stale")
    assert world.attempt() is None  # no attempt, hold or reservation was made
    assert (
        world.row("SELECT held_cents FROM campaigns WHERE id=?", world.campaign_id)["held_cents"]
        == 0
    )


def test_launch_proceeds_when_the_files_still_match(tmp_path):
    path = _profiles_file(tmp_path, "GB")
    world = SaladWorld(tmp_path)
    fresh.record([path])
    world.start()
    world.tick_until(lambda: world.events("campaign.stage_awaiting_human") != [])
    assert world.events("campaign.stage_operator_config_stale") == []


def _executor_files(tmp_path, profile, **over):
    """An executor config naming a profiles file that holds ``profile`` with overrides."""
    document = {
        "schema": "tessera.salad.campaign-profiles.v1",
        "profiles": [{**profile.model_dump(mode="json", by_alias=True), **over}],
    }
    profiles_file = tmp_path / "executor-profiles.json"
    profiles_file.write_text(json.dumps(document))
    config = tmp_path / "executor.json"
    config.write_text(json.dumps({"profiles_file": str(profiles_file), "profile": profile.name}))
    profiles_file.chmod(0o600)
    config.chmod(0o600)
    return config


def test_executor_copy_that_differs_is_a_mismatch_even_if_the_served_file_is_unchanged(tmp_path):
    """The two operator files diverge: served file says one country, executor file another."""
    world = SaladWorld(tmp_path)
    profile = world.profiles.profiles[0]
    served = _profiles_file(tmp_path, profile.country or "US")
    other = "GB" if profile.country != "GB" else "US"
    config = _executor_files(tmp_path, profile, country=other)
    fresh.record([served], executor_config=config)
    assert fresh.stale_files() == []
    assert fresh.launch_problem(profile.name, profile) == "operator_profile_mismatch"
    world.start()
    world.tick(5)
    assert world.events("campaign.stage_operator_profile_mismatch") != []
    assert world.attempt() is None
    campaign = world.row("SELECT stop_reason FROM campaigns WHERE id=?", world.campaign_id)
    assert campaign["stop_reason"].endswith(":operator_profile_mismatch")


def test_matching_executor_copy_is_accepted_and_an_edit_after_start_is_stale(tmp_path):
    world = SaladWorld(tmp_path)
    profile = world.profiles.profiles[0]
    config = _executor_files(tmp_path, profile)
    fresh.record([], executor_config=config)
    assert fresh.launch_problem(profile.name, profile) is None
    # The executor profile file is edited after start: the copy now differs.
    _executor_files(tmp_path, profile, country="ZZ")
    assert fresh.launch_problem(profile.name, profile) == "operator_profile_mismatch"
    # The executor config itself is edited or unreadable: stale or mismatch, never silent.
    config.write_text("{}")
    assert fresh.launch_problem(profile.name, profile) == "operator_config_stale"
    config.unlink()
    assert fresh.launch_problem(profile.name, profile) == "operator_config_stale"


def _to_reviewed(world):
    world.to_awaiting_review()
    world.resolve_decisions()


def test_route_start_is_refused_when_a_file_changes_before_approval(tmp_path):
    path = _profiles_file(tmp_path, "GB")
    world = SaladWorld(tmp_path)
    world.to_awaiting_review()  # scaffold is done: only the approve/start check is left
    fresh.record([path])
    _profiles_file(tmp_path, "US")
    world.resolve_decisions()
    world.tick(12)
    assert world.phase() != "route_started"
    attempt = world.attempt()
    assert attempt is None or attempt["status"] != "running"
    assert attempt is None or attempt["operation_id"] is None


def test_replay_after_start_route_is_recorded_is_not_refused_by_a_later_edit(
    tmp_path, monkeypatch
):
    """start_route succeeded but its bookkeeping failed; an edit before the replay must
    not mark the already running attempt failed."""
    path = _profiles_file(tmp_path, "GB")
    world = SaladWorld(tmp_path)
    world.to_awaiting_review()
    fresh.record([path])
    driver = world.driver
    real = driver._record_started
    calls = []

    def failing(attempt_id, operation_id):
        calls.append(attempt_id)
        raise RuntimeError("database busy")

    monkeypatch.setattr(driver, "_record_started", failing)
    world.resolve_decisions()
    world.tick_until(lambda: calls, limit=40)  # start_route is recorded, bookkeeping failed
    monkeypatch.setattr(driver, "_record_started", real)
    _profiles_file(tmp_path, "US")  # the operator edits after the route started
    assert fresh.stale_files() == ["profiles.json"]
    world.tick(8)
    assert world.phase() == "route_started"
    attempt = world.attempt()
    assert attempt["status"] == "running" and attempt["operation_id"] is not None
    assert world.events("campaign.stage_operator_config_stale") == []


def test_serve_records_the_served_files_and_refuses_a_relative_executor_config(
    monkeypatch, tmp_path
):
    import hashlib

    from test_campaign_serve_wiring import _private_file, _profiles_document, _serve

    path = _private_file(tmp_path, "profiles.json", _profiles_document())
    code, _ = _serve(
        monkeypatch, "--salad-campaign-profiles", str(path),
        "--salad-executor-config", "executor.json",
    )
    assert code == 2  # a relative path is refused at start, not at every launch
    code, _ = _serve(monkeypatch, "--salad-campaign-profiles", str(path))
    assert code == 0
    assert fresh._RECORDED[str(path)] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert fresh.stale_files() == []
    path.write_text(path.read_text() + " ")
    assert fresh.stale_files() == ["profiles.json"]
