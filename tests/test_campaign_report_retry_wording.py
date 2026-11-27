"""The official report says an attempt "was retried" only when the stage has a later attempt
row (AGENTS.md: keep evidence honest). Wording only: no state is changed by the report."""

from __future__ import annotations

import pytest
from campaign_runner_harness import World, dump, stage_spec

from tessera_control import campaign_report as report_module
from tessera_control.campaign_report import _stage_report, build_report, render_text
from tessera_control.store import _connect

CREATE_NOT_SENT = "infra_salad_create_not_sent"
INFRA = [
    CREATE_NOT_SENT,
    "infra_salad_provisioning_overrun",
    "infra_salad_guardian_unavailable",
    "infra_salad_reallocated",
    "infra_salad_safety_stop",
]


def _attempt(number, failure_class):
    return dict(
        attempt=number,
        failure_class=failure_class,
        exit_code=None,
        settled_cents=0,
        run_receipt_digest=None,
        provider_id="salad",
        runtime=None,
    )


def _stage(*classes):
    return dict(
        stage_key="fit",
        status="failed",
        attempts=[_attempt(n, c) for n, c in enumerate(classes, start=1)],
    )


@pytest.mark.parametrize("failure_class", INFRA)
def test_an_only_attempt_under_another_stop_reason_was_not_retried(failure_class):
    report = _stage_report(
        _stage(failure_class), {}, True, False, "stage_failed:other:salad_container_exited_nonzero"
    )
    assert "was retried" not in str(report)
    assert report["outcome"].endswith(
        "but it was not retried because the campaign was already stopping"
    )
    assert report["retried_because"] is None


@pytest.mark.parametrize("reason", ["cancel:operator", "budget_overrun"])
def test_a_cancel_or_budget_stop_also_reads_as_already_stopping(reason):
    report = _stage_report(_stage(CREATE_NOT_SENT), {}, True, False, reason)
    assert report["outcome"].endswith("the campaign was already stopping")


@pytest.mark.parametrize("reason", [None, f"stage_failed:fit:{CREATE_NOT_SENT}_exhausted"])
def test_an_only_attempt_whose_own_bounds_ended_the_stage_used_up_its_retries(reason):
    report = _stage_report(_stage(CREATE_NOT_SENT), {}, True, False, reason)
    assert "was retried" not in str(report)
    assert report["outcome"].endswith("but its retries were used up")


def test_a_stage_with_a_later_attempt_was_retried():
    report = _stage_report(
        _stage(CREATE_NOT_SENT, "salad_container_exited_nonzero"),
        {},
        True,
        False,
        "stage_failed:fit:salad_container_exited_nonzero",
    )
    assert report["retried_because"] == (
        "an earlier attempt failed: the cloud create was refused before it was sent, so it was "
        "retried"
    )
    assert report["outcome"] == "failed because the container process exited with a failing code"


def test_other_classes_keep_their_words():
    report = _stage_report(_stage("salad_interrupted"), {}, True, False, "cancel:x")
    assert report["outcome"] == (
        "failed because the cloud provider interrupted the run after it had started"
    )


# -- through the real report build -------------------------------------------------------------


def _failed_campaign(tmp_path):
    """A one-stage campaign that failed on its first attempt (a real run of the harness)."""
    world = World(tmp_path)
    world.scenarios = [(3, {"data.json": "{}"})]
    spec = world.env.spec(budget_cap_cents=500)
    spec["stages"] = [stage_spec(world.env, "fit")]
    campaign = world.campaign(spec)
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "failed", dump(world, campaign["id"])
    connection = _connect(world.env.state / "tessera.sqlite3")
    connection.execute("PRAGMA foreign_keys=OFF")
    return world, campaign, connection


def _relabel(connection, campaign_id, *, stop_reason, extra_attempt=False):
    """Rewrite the recorded rows into the shape under test (the report is rebuilt from rows)."""
    rows = connection.execute(
        "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? ORDER BY attempt",
        (campaign_id,),
    ).fetchall()
    assert len(rows) == 1
    connection.execute(
        "UPDATE campaign_stage_attempts SET failure_class=?, exit_code=NULL WHERE id=?",
        (CREATE_NOT_SENT, rows[0]["id"]),
    )
    if extra_attempt:
        columns = rows[0].keys()
        values = dict(rows[0])
        values.update(
            id=values["id"] + "_2",
            attempt=2,
            failure_class="salad_container_exited_nonzero",
            grant_id=None,
            operation_id=None,
            manifest_id=None,
            run_receipt_digest=None,
        )
        connection.execute(
            f"INSERT INTO campaign_stage_attempts ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            [values[c] for c in columns],
        )
    connection.execute("UPDATE campaigns SET stop_reason=? WHERE id=?", (stop_reason, campaign_id))
    connection.commit()


def test_the_report_for_an_unretried_create_not_sent_does_not_say_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(report_module, "load_verified_receipt", lambda *a: None)
    world, campaign, connection = _failed_campaign(tmp_path)
    _relabel(
        connection, campaign["id"], stop_reason="stage_failed:other:salad_container_exited_nonzero"
    )
    report = build_report(connection, campaign["id"])
    text = render_text(report)
    assert "was retried" not in text and "was retried" not in str(report)
    assert "not retried because the campaign was already stopping" in text


def test_the_report_with_a_second_attempt_says_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(report_module, "load_verified_receipt", lambda *a: None)
    world, campaign, connection = _failed_campaign(tmp_path)
    _relabel(
        connection,
        campaign["id"],
        stop_reason="stage_failed:fit:salad_container_exited_nonzero",
        extra_attempt=True,
    )
    report = build_report(connection, campaign["id"])
    assert report["stages"][0]["attempts"] == 2
    assert "so it was retried" in render_text(report)
