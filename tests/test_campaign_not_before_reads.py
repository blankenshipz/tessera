"""The ``not_before`` hold is visible: the derived state, the campaign read, the report, stuck."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from campaign_fixture import Env

from tessera_control import campaign_hold
from tessera_control import campaign_provider_worker as worker
from tessera_control import campaign_providers as cp
from tessera_control.campaign_report import build_report, render_text
from tessera_control.campaign_store import CampaignStore
from tessera_control.store import _connect

NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
SPEC = '{"version": 2, "not_before": "2026-03-01T13:00:00Z"}'


def test_running_with_a_future_start_waits():
    hold = campaign_hold.waiting(SPEC, "running", NOW)
    assert hold == {
        "state": "waiting",
        "reason": "not_before",
        "not_before": "2026-03-01T13:00:00Z",
        "seconds_remaining": 3600,
    }


@pytest.mark.parametrize("now", [NOW + timedelta(hours=1), NOW + timedelta(hours=2)])
def test_a_start_that_has_come_does_not_wait(now):
    assert campaign_hold.waiting(SPEC, "running", now) is None


@pytest.mark.parametrize(
    "status",
    ["submitted", "succeeded", "failed", "cancelled", "stopped", "blocked", "stopping"],
)
def test_only_a_running_campaign_waits(status):
    assert campaign_hold.waiting(SPEC, status, NOW) is None


@pytest.mark.parametrize(
    "spec",
    [None, "", "not json", "[]", '{"version": 1}', '{"version": 2}', '{"not_before": 5}',
     '{"not_before": "tomorrow"}', {"not_before": None}],
)  # fmt: skip
def test_an_absent_v1_or_unreadable_spec_never_waits(spec):
    assert campaign_hold.waiting(spec, "running", NOW) is None


@pytest.fixture(autouse=True)
def _restore_providers():
    yield
    worker.register_from_config(None)


def _future() -> str:
    return (datetime.now(UTC) + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _campaign(tmp_path, **extra):
    env = Env(tmp_path)
    spec = env.spec(**extra)
    if "not_before" in extra:
        env.trust()
        cp.configure_providers({"local_docker": {"worker_principal_id": env.worker["id"]}})
        spec["version"] = 2
        spec.pop("worker_principal_id", None)
        stages = []
        for stage in spec["stages"][:1]:
            stage = dict(stage)
            stage.pop("backend")
            stage["runtime"] = "docker-cpu-v1"
            stage["provider"] = "local-docker"
            stage["outputs"] = [
                {"name": "data", "path": path, "max_bytes": stage["max_output_bytes"]}
                for path in stage.pop("output_paths")
            ]
            del stage["max_output_bytes"]
            stages.append(stage)
        spec["stages"] = stages
    return env, env.approve(env.submit(spec=spec))


def test_a_waiting_campaign_shows_in_the_read_list_and_report(tmp_path):
    env, campaign = _campaign(tmp_path, not_before=_future())
    store = CampaignStore(env.state)
    read = store.get_campaign(env.owner, campaign["id"])
    assert read["status"] == "running"
    assert read["waiting"]["state"] == "waiting" and read["waiting"]["reason"] == "not_before"
    assert 1700 < read["waiting"]["seconds_remaining"] <= 1800
    listed = store.list_campaigns(env.owner, env.project["id"])["campaigns"]
    assert listed[0]["waiting"]["reason"] == "not_before"
    connection = _connect(env.state / "tessera.sqlite3")
    report = build_report(connection, campaign["id"])
    connection.close()
    assert report["campaign"]["waiting"]["not_before"].endswith("Z")
    assert report["headline"].startswith("The campaign is approved and waiting to start at ")
    assert "failed" not in report["headline"] and "stuck" not in report["headline"]
    assert "waiting to start" in render_text(report)


def test_a_campaign_without_not_before_shows_no_waiting(tmp_path):
    env, campaign = _campaign(tmp_path)
    store = CampaignStore(env.state)
    assert "waiting" not in store.get_campaign(env.owner, campaign["id"])
    assert "waiting" not in store.list_campaigns(env.owner, env.project["id"])["campaigns"][0]
    connection = _connect(env.state / "tessera.sqlite3")
    report = build_report(connection, campaign["id"])
    connection.close()
    assert "waiting" not in report["campaign"]


def test_the_stuck_detector_skips_a_waiting_campaign(tmp_path):
    from tessera_control import campaign_stuck as stuck

    env, campaign = _campaign(tmp_path, not_before=_future())
    old = "2020-01-01T00:00:00Z"
    env.execute("UPDATE campaigns SET updated_at=?", old)
    env.execute("UPDATE campaign_stages SET updated_at=?", old)
    connection = _connect(env.state / "tessera.sqlite3")
    found = stuck.detect(
        connection, env.owner["org_id"], env.project["id"],
        {"unchanged_seconds": 60, "lease_grace_seconds": 60}, datetime.now(UTC),
    )  # fmt: skip
    assert [f for f in found if f["campaign_id"] == campaign["id"]] == []
    env.execute("UPDATE campaigns SET spec_json=json_remove(spec_json, '$.not_before')")
    found = stuck.detect(
        connection, env.owner["org_id"], env.project["id"],
        {"unchanged_seconds": 60, "lease_grace_seconds": 60}, datetime.now(UTC),
    )  # fmt: skip
    connection.close()
    assert [f["code"] for f in found if f["campaign_id"] == campaign["id"]] == ["no_progress"]
