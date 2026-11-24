"""deploy-upgrade re-pins a legacy or malformed executor ``source_revision`` with the plan's
commit, and still blocks when the config has any other error or the plan names no commit."""

from __future__ import annotations

import pytest
from test_deploy_config import executor_document, write

from tessera_control import deploy_config, deploy_upgrade
from tessera_control import salad_live_check as live
from tessera_control.campaign_salad_executor import load_config

COMMIT = "a" * 40
REFRESHABLE = {
    "bare commit": COMMIT,
    "malformed pin": COMMIT + "+sha256:" + "b" * 63,
}


def _config(tmp_path, **changes):
    base = tmp_path / "executor"
    base.mkdir(mode=0o700, exist_ok=True)
    return write(base, dict(executor_document(base), **changes))


def _plan(tmp_path, path, source_revision):
    state = tmp_path / "state"
    state.mkdir(mode=0o700, exist_ok=True)

    class Plan:
        state_dir = str(state)
        executor_configs = [str(path)]
        live_configs: list[str] = []
        providers_file = None
        max_backup_bytes = None

    Plan.source_revision = source_revision
    return Plan


def _config_blocking(plan):
    _findings, blocking = deploy_upgrade.preflight(plan, smoke=False)
    return sorted(item for item in blocking if item.startswith("config.1."))


@pytest.mark.parametrize("value", REFRESHABLE.values(), ids=REFRESHABLE.keys())
def test_deploy_upgrade_self_fixes_a_refreshable_pin_with_the_plan_commit(tmp_path, value):
    path = _config(tmp_path, source_revision=value)
    plan = _plan(tmp_path, path, COMMIT)
    findings, _blocking = deploy_upgrade.preflight(plan, smoke=False)
    by_id = {item.check_id: item for item in findings}
    assert by_id["config.1.source_pin"].fix.fix_id == "refresh_source_pin"
    assert "config.1.valid" not in by_id
    assert _config_blocking(plan) == []

    receipts = tmp_path / "receipts"
    receipts.mkdir(mode=0o700)
    receipt = deploy_config.apply_refresh_source_pin(path, receipts, plan.source_revision)
    assert receipt is not None
    assert load_config(path).source_revision == live.installed_source_pin(COMMIT)
    assert all(item.status == "ok" for item in deploy_config.check_config(path, label="1"))


@pytest.mark.parametrize("value", REFRESHABLE.values(), ids=REFRESHABLE.keys())
def test_without_a_plan_commit_a_refreshable_pin_still_blocks(tmp_path, value):
    path = _config(tmp_path, source_revision=value)
    assert _config_blocking(_plan(tmp_path, path, None)) == ["config.1.source_pin"]


OTHER_ERRORS = {
    "invalid field": {"poll_seconds": "five"},
    "model-level error": {"profiles": ["gpu-main"]},
}


@pytest.mark.parametrize("other", OTHER_ERRORS.values(), ids=OTHER_ERRORS.keys())
@pytest.mark.parametrize("value", REFRESHABLE.values(), ids=REFRESHABLE.keys())
def test_a_pin_problem_with_another_error_still_blocks(tmp_path, value, other):
    path = _config(tmp_path, source_revision=value, **other)
    plan = _plan(tmp_path, path, COMMIT)
    findings, _blocking = deploy_upgrade.preflight(plan, smoke=False)
    valid = {item.check_id: item for item in findings}["config.1.valid"]
    locs = {error["loc"] for error in valid.detail["errors"]}
    assert "source_revision" in locs
    assert _config_blocking(plan) == ["config.1.valid"]


@pytest.mark.parametrize("other", OTHER_ERRORS.values(), ids=OTHER_ERRORS.keys())
def test_another_error_with_a_good_pin_still_blocks(tmp_path, other):
    path = _config(tmp_path, **other)
    assert _config_blocking(_plan(tmp_path, path, COMMIT)) == ["config.1.valid"]
