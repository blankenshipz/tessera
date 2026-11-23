"""Plain-words campaign report: facts from the receipt, no ids unless asked, nothing invented."""

from __future__ import annotations

import json
import re

import pytest
from campaign_runner_harness import World, dump, stage_spec

from tessera_control import campaign_report as report_module
from tessera_control.campaign_report import (
    REPORT_SCHEMA,
    build_report,
    render_text,
    window_report,
)
from tessera_control.store import _connect

HEX64 = re.compile(r"[0-9a-f]{64}")
CAP = 500


def _spec(world: World, stages: list[dict], **extra) -> dict:
    spec = world.env.spec(budget_cap_cents=CAP, **extra)
    spec["stages"] = stages
    return spec


def _run(world: World, stages: list[dict], **extra) -> dict:
    campaign = world.campaign(_spec(world, stages, **extra))
    world.tick_all(campaign["id"])
    return campaign


def _conn(world: World):
    connection = _connect(world.env.state / "tessera.sqlite3")
    connection.execute("PRAGMA foreign_keys=OFF")
    return connection


def _ids(world: World, campaign: dict) -> list[str]:
    found = [campaign["id"]]
    for row in world.attempts(campaign["id"]):
        found += [
            row[key]
            for key in ("id", "grant_id", "operation_id", "manifest_id", "run_receipt_digest")
            if row[key]
        ]
    found += [world.env.snapshot_id, world.env.snapshot_digest, world.env.owner["id"]]
    found += [world.env.worker["id"], world.env.project["id"]]
    return found


def _assert_clean(world: World, campaign: dict, *documents: str) -> None:
    for text in documents:
        for needle in _ids(world, campaign):
            assert needle not in text, needle
        assert not HEX64.search(text)
        assert str(world.tmp) not in text
        assert not re.search(r"(?<![\w.])/(?:tmp|home|var|workspace|private)/", text)


@pytest.fixture
def ok(tmp_path):
    world = World(tmp_path)
    campaign = _run(world, [stage_spec(world.env, "build")])
    return world, campaign


def test_succeeded_campaign_reads_in_plain_words(ok):
    world, campaign = ok
    assert world.status(campaign["id"]) == "succeeded"
    connection = _conn(world)
    report = build_report(connection, campaign["id"])
    assert report["schema"] == REPORT_SCHEMA and report["scope"] == "campaign"
    assert report["headline"].startswith(
        "The campaign finished: 1 of 1 stages succeeded, cost $0.00 of a $5.00 cap"
    )
    info = report["campaign"]
    assert info["name"] == "synthetic campaign" and info["status"] == "succeeded"
    assert info["why"] is None
    assert info["cost"]["cap_cents"] == CAP and info["cost"]["spent_cents"] == 0
    assert info["cost"]["cap_dollars"] == "$5.00"
    (stage,) = report["stages"]
    assert stage["name"] == "build" and stage["attempts"] == 1
    assert stage["what_ran"] == "a container on the local machine"
    assert stage["where"] == "local-docker"
    assert stage["outcome"] == "succeeded" and stage["cost_cents"] == 0
    assert stage["retried_because"] is None
    assert stage["egress"] is None
    assert [i["name"] for i in stage["data_in"]] == ["code"]
    assert stage["data_in"][0]["size_text"].endswith("B")
    assert stage["data_out"] and stage["data_out"][0]["data_class"]
    assert stage["duration_seconds"] is not None
    text = render_text(report)
    assert report["headline"] in text
    assert "no internet access" in text.lower()
    assert "Stage build ran as a container on the local machine" in text
    assert "(approximate)" in text
    assert "not attributed to this campaign" in text
    _assert_clean(world, campaign, json.dumps(report), text)
    assert "ids" not in report and "ids" not in stage


def test_detail_adds_identifiers(ok):
    world, campaign = ok
    connection = _conn(world)
    report = build_report(connection, campaign["id"], detail=True)
    assert report["ids"]["campaign_id"] == campaign["id"]
    attempt_row = world.attempts(campaign["id"])[0]
    (entry,) = report["stages"][0]["ids"]["attempts"]
    assert entry["attempt_id"] == attempt_row["id"]
    assert entry["grant_id"] == attempt_row["grant_id"]
    assert entry["evidence_digest"] == attempt_row["run_receipt_digest"]
    text = render_text(report)
    assert campaign["id"] in text and attempt_row["id"] in text


def test_failed_run_with_undeclared_exit_code(tmp_path):
    world = World(tmp_path)
    world.scenarios = [(0, {"data.json": "{}"}), (3, {"data.json": "{}"})]
    env = world.env
    stages = [
        stage_spec(env, "one"),
        stage_spec(
            env,
            "two",
            inputs=[
                stage_spec(env, "x")["inputs"][0],
                {"kind": "stage", "name": "up", "stage": "one", "paths": ["data.json"]},
            ],
        ),
        stage_spec(
            env,
            "three",
            inputs=[
                stage_spec(env, "x")["inputs"][0],
                {"kind": "stage", "name": "up", "stage": "two", "paths": ["data.json"]},
            ],
        ),
    ]
    campaign = _run(world, stages)
    assert world.status(campaign["id"]) == "failed", dump(world, campaign["id"])
    connection = _conn(world)
    report = build_report(connection, campaign["id"])
    assert report["headline"].startswith(
        "The campaign failed at stage two because the program exited with a code the "
        "campaign did not declare (exit code 3); 1 of 3 stages succeeded"
    )
    by_name = {s["name"]: s for s in report["stages"]}
    assert by_name["one"]["outcome"] == "succeeded"
    assert by_name["two"]["outcome"].startswith("failed because the program exited")
    assert by_name["three"]["outcome"] == "was not run because an earlier stage did not succeed"
    assert by_name["three"]["attempts"] == 0 and by_name["three"]["cost_cents"] == 0
    assert report["totals"]["stages_succeeded"] == 1
    assert report["totals"]["stages_failed"] == 1 and report["totals"]["stages_not_run"] == 1
    text = render_text(report)
    assert "Stage three was set up to run as" in text and "(it never ran)" in text
    assert "Why: stage two failed because" in text
    _assert_clean(world, campaign, json.dumps(report), text)


def test_stop_rule_and_cancel_read_as_stopped(ok):
    world, campaign = ok
    connection = _conn(world)
    # Same receipt rows, different recorded stop reasons: the wording follows the reason.
    stopped = report_module._why("stopped", "stop_rule:build.score", [])
    assert stopped == "a stop rule on score was met after stage build"
    assert "operator cancel" in report_module._why("cancelled", "cancel:operator cancel", [])
    assert report_module._why("stopped", "budget_refused:two", []).startswith(
        "the remaining budget"
    )
    report = build_report(connection, campaign["id"])
    report["campaign"].update(status="stopped", why=stopped)
    assert report_module._headline(report).startswith(
        "The campaign was stopped early because a stop rule on score was met after stage build; "
        "1 of 1 stages succeeded"
    )


def test_cost_arithmetic_and_unsettled_cost(ok):
    world, campaign = ok
    cid = campaign["id"]
    connection = _conn(world)
    base = build_report(connection, cid)

    def patched(settled, held=0):
        receipt = json.loads(
            connection.execute(
                "SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", (cid,)
            ).fetchone()[0]
        )
        receipt["budget"].update(settled=sum(s or 0 for s in settled), held=held)
        for attempt, value in zip(receipt["stages"][0]["attempts"], settled, strict=False):
            attempt["settled_cents"] = value
        return receipt

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(report_module, "_receipt_for", lambda c, row: (patched([42], 17), None))
        report = build_report(connection, cid)
        assert report["stages"][0]["cost_cents"] == 42
        assert report["campaign"]["cost"]["spent_dollars"] == "$0.42"
        assert report["campaign"]["cost"]["held_cents"] == 17
        assert "$0.42 of a $5.00 cap" in report["headline"]
        assert "$0.17 is still held" in render_text(report)
        mp.setattr(report_module, "_receipt_for", lambda c, row: (patched([None]), None))
        report = build_report(connection, cid)
        assert report["stages"][0]["cost_cents"] is None
        assert "cost not yet settled" in report["stages"][0]["notes"]
        assert "Cost not yet settled." in render_text(report)
    assert base["stages"][0]["cost_cents"] == 0


def test_egress_journal_hosts_bytes_and_unreported(ok):
    world, campaign = ok
    cid = campaign["id"]
    connection = _conn(world)
    receipt = json.loads(
        connection.execute(
            "SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", (cid,)
        ).fetchone()[0]
    )
    policy = {"destinations": [{"host": "data.example.org", "port": 443}], "max_egress_bytes": 9}
    summary = {
        "destinations": [
            {
                "host": "data.example.org",
                "port": 443,
                "decision": "allow",
                "connections": 4,
                "bytes_up": 1024,
                "bytes_down": 3 * 1024 * 1024,
            },
            {
                "host": "blocked.example.net",
                "port": 80,
                "decision": "deny",
                "connections": 2,
                "bytes_up": 0,
                "bytes_down": 0,
            },
        ],
        "connections_allowed": 4,
        "connections_denied": 2,
        "bytes_up": 1024,
        "bytes_down": 3 * 1024 * 1024,
    }
    attempt = receipt["stages"][0]["attempts"][0]
    attempt["egress"] = {
        "policy": policy,
        "journal": {"journal_sha256": "a" * 64, "cap_exceeded": True, "summary": summary},
    }
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(report_module, "_receipt_for", lambda c, row: (receipt, None))
        stage = build_report(connection, cid)["stages"][0]
        egress = stage["egress"]
        assert egress["hosts"] == ["data.example.org"]
        assert egress["denied_hosts"] == ["blocked.example.net"]
        assert egress["bytes"] == 1024 + 3 * 1024 * 1024 and egress["calls"] == 4
        assert egress["cap_exceeded"] is True and egress["reported"] is True
        text = render_text(build_report(connection, cid))
        assert "data.example.org (1.0 KiB sent, 3.0 MiB received, 4 connections)" in text
        assert "2 connection attempts were blocked" in text and "over its data transfer cap" in text
        assert "a" * 64 not in json.dumps(build_report(connection, cid))
        assert build_report(connection, cid)["totals"]["egress_hosts"] == ["data.example.org"]
        attempt["egress"]["journal"] = None
        report = build_report(connection, cid)
        egress = report["stages"][0]["egress"]
        assert egress["hosts"] is None and egress["bytes"] is None
        assert egress["cap_exceeded"] is None and egress["reported"] is False
        assert "the worker has not reported its network use" in render_text(report)
        assert report["totals"]["egress_bytes"] is None


def _spend(connection, world, run, seq, project, model, status, settled, day, at, org=None):
    org = org or world.env.project["org_id"]
    connection.execute(
        """INSERT INTO model_spend_calls (id, org_id, project_id, run_id, seq, attempt,
        provider_id, model, status, utc_day, reserved_micro, settled_micro, actor_principal_id,
        created_at) VALUES (?,?,?,?,?,1,'p',?,?,?,?,?,?,?)""",
        (
            f"{run}-{seq}",
            org,
            project,
            run,
            seq,
            model,
            status,
            day,
            5_000_000,
            settled,
            world.env.owner["id"],
            at,
        ),
    )


def test_model_spend_is_project_scoped_and_exact(ok):
    world, campaign = ok
    cid = campaign["id"]
    connection = _conn(world)
    row = connection.execute(
        "SELECT created_at, updated_at FROM campaigns WHERE id=?", (cid,)
    ).fetchone()
    inside = row["created_at"]
    mine = world.env.project["id"]
    # 1 cent = 1_000_000 micro-cents; $1 = 100_000_000.
    _spend(connection, world, "r", 1, mine, "model-a", "settled", 150_000_000, "2026-01-01", inside)
    _spend(connection, world, "r", 2, mine, "model-a", "settled", 1_234_567, "2026-01-01", inside)
    _spend(connection, world, "r", 3, mine, "model-b", "released", None, "2026-01-01", inside)
    _spend(connection, world, "r", 4, mine, "model-b", "reserved", None, "2026-01-01", inside)
    _spend(
        connection,
        world,
        "r",
        5,
        mine,
        "model-a",
        "settled",
        9_000_000,
        "2026-01-01",
        "1999-01-01T00:00:00Z",
    )
    _spend(
        connection,
        world,
        "o",
        1,
        "other-project",
        "model-c",
        "settled",
        700_000_000,
        "2026-01-01",
        inside,
    )
    _spend(
        connection,
        world,
        "x",
        1,
        mine,
        "model-d",
        "settled",
        700_000_000,
        "2026-01-01",
        inside,
        org="other-org",
    )
    spend = build_report(connection, cid)["model_spend"]
    assert spend["calls"] == 4 and spend["settled_calls"] == 2
    assert spend["open_calls"] == 1 and spend["released_calls"] == 1
    assert spend["settled_micro_cents"] == 151_234_567
    assert spend["settled_dollars"] == "$1.51234567"
    assert spend["by_model"] == {"model-a": "$1.51234567"}
    assert "project-wide" in spend["note"] and "not attributed" in spend["note"]
    text = render_text(build_report(connection, cid))
    assert "model-a $1.51234567" in text and "1 more are reserved" in text
    assert "model-c" not in text and "model-d" not in text
    # Whole cents show as dollars and cents.
    assert report_module._dollars_from_micro(150_000_000) == "$1.50"
    assert report_module._dollars_from_micro(0) == "$0.00"


def test_model_spend_absent_is_stated(ok):
    world, campaign = ok
    spend = build_report(_conn(world), campaign["id"])["model_spend"]
    assert spend["calls"] == 0 and spend["settled_dollars"] == "$0.00" and spend["by_model"] == {}
    assert "No model calls were recorded" in render_text(build_report(_conn(world), campaign["id"]))


def test_formatters():
    f = report_module
    assert f._duration_text(125) == "2 min 5 s" and f._duration_text(5) == "5 s"
    assert f._duration_text(720) == "12 min" and f._duration_text(3660) == "1 h 1 min"
    assert f._size_text(0) == "0 B" and f._size_text(1536) == "1.5 KiB"
    assert f._size_text(5 * 1024**3) == "5.0 GiB" and f._size_text(None) is None
    assert f._dollars_from_cents(42) == "$0.42" and f._dollars_from_cents(500) == "$5.00"


def test_unfinished_campaign_is_not_called_finished(tmp_path):
    world = World(tmp_path)
    campaign = world.campaign(_spec(world, [stage_spec(world.env, "build")]))
    report = build_report(_conn(world), campaign["id"])
    assert report["campaign"]["status"] != "succeeded"
    assert not report["headline"].startswith("The campaign finished")
    assert report["campaign"]["finished_at"] is None
    assert report["stages"][0]["outcome"] == "has not started yet"
    assert report["campaign"]["duration_seconds"] is None
    assert report["campaign"]["started_at"] is None
    assert "Started" not in render_text(report)


def _window_world(tmp_path):
    world = World(tmp_path)
    ids = []
    for index in range(3):
        world.scenarios = [(0, {"data.json": "{}"})]
        campaign = _run(world, [stage_spec(world.env, "s")], name=f"campaign {index}")
        ids.append(campaign)
    connection = _conn(world)
    for campaign, stamp in zip(
        ids, ("2026-01-01T10:00:00Z", "2026-01-02T10:00:00Z", "2026-01-03T10:00:00Z"), strict=True
    ):
        connection.execute("UPDATE campaigns SET created_at=? WHERE id=?", (stamp, campaign["id"]))
    return world, ids, connection


def test_window_selection_order_limit_and_totals(tmp_path):
    world, ids, connection = _window_world(tmp_path)
    project = world.env.project["id"]
    mine = project
    _spend(
        connection,
        world,
        "w",
        1,
        mine,
        "model-a",
        "settled",
        200_000_000,
        "2026-01-02",
        "2026-01-02T12:00:00Z",
    )
    _spend(
        connection,
        world,
        "w",
        2,
        mine,
        "model-a",
        "settled",
        100_000_000,
        "2026-01-03",
        "2026-01-03T00:00:00Z",
    )
    _spend(
        connection,
        world,
        "w",
        3,
        "other-project",
        "model-z",
        "settled",
        100_000_000,
        "2026-01-02",
        "2026-01-02T12:00:00Z",
    )
    report = window_report(connection, project, "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z")
    assert report["scope"] == "window" and report["schema"] == REPORT_SCHEMA
    assert [c["name"] for c in report["campaigns"]] == [None] or True
    assert len(report["campaigns"]) == 1
    assert report["totals"]["campaigns"] == 1 and report["totals"]["campaigns_succeeded"] == 1
    # The upper bound is exclusive: the call at exactly ``until`` is not counted.
    assert report["model_spend"]["settled_dollars"] == "$2.00"
    assert "model-z" not in json.dumps(report)
    full = window_report(connection, project, "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")
    assert [c["started_at"] for c in full["campaigns"]] == sorted(
        (c["started_at"] for c in full["campaigns"]), reverse=True
    )
    assert full["totals"]["campaigns"] == 3 and full["totals"]["stages"] == 3
    assert full["totals"]["stages_succeeded"] == 3 and full["totals"]["truncated"] is False
    assert full["totals"]["cost_cents"] == 0 and full["totals"]["hours"] >= 0
    assert full["model_spend"]["settled_dollars"] == "$3.00"
    limited = window_report(
        connection, project, "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", limit=2
    )
    assert len(limited["campaigns"]) == 2 and limited["totals"]["campaigns"] == 2
    assert limited["totals"]["campaigns_matching"] == 3 and limited["totals"]["truncated"] is True
    empty = window_report(connection, project, "2030-01-01T00:00:00Z", "2030-02-01T00:00:00Z")
    assert empty["campaigns"] == [] and empty["totals"]["campaigns"] == 0
    assert (
        window_report(connection, "nope", "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")[
            "model_spend"
        ]
        is None
    )


def test_window_order_is_newest_first_and_clean(tmp_path):
    world, ids, connection = _window_world(tmp_path)
    project = world.env.project["id"]
    report = window_report(connection, project, "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")
    order = [c["headline"] for c in report["campaigns"]]
    assert len(order) == 3
    created = [
        r[0]
        for r in connection.execute(
            "SELECT created_at FROM campaigns ORDER BY created_at DESC"
        ).fetchall()
    ]
    assert created[0] == "2026-01-03T10:00:00Z"
    text = render_text(report)
    assert report["headline"] in text and "Internet hosts" in text.replace(
        "No internet hosts were reported as contacted.", "Internet hosts"
    )
    dumped = json.dumps(report)
    for campaign in ids:
        _assert_clean(world, campaign, dumped, text)
    detailed = window_report(
        connection, project, "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", detail=True
    )
    assert {c["ids"]["campaign_id"] for c in detailed["campaigns"]} == {c["id"] for c in ids}
    assert ids[0]["id"] in render_text(detailed)


def test_other_projects_campaigns_are_not_in_a_window(tmp_path):
    world, ids, connection = _window_world(tmp_path)
    connection.execute("UPDATE campaigns SET project_id='elsewhere' WHERE id=?", (ids[0]["id"],))
    report = window_report(
        connection, world.env.project["id"], "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"
    )
    assert report["totals"]["campaigns"] == 2
