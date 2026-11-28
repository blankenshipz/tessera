"""A stored spec the current code cannot verify blocks its campaign (ADR 0214).

A stored spec that no longer parses under new code (``stored_spec_unreadable``) or no longer
re-digests to its approved digest (``spec_digest_mismatch``) blocks a running campaign once,
reduce-only, instead of failing it with live attempts not unwound. The read-only deploy check
finds the same rows before a deploy, with the same function the runner uses. Real stores and
runner on temporary state.
"""

from __future__ import annotations

import json
import os

from campaign_runner_harness import World, stage_spec

from tessera_control.campaign_models import parse_spec_json, spec_digest
from tessera_control.campaign_runner import (
    STORED_SPEC_DIGEST_MISMATCH,
    STORED_SPEC_EVENT,
    STORED_SPEC_UNREADABLE,
    verify_stored_spec,
)
from tessera_control.deploy_stored_state import check_stored_state

SECRET_MARK = "do-not-echo-this-spec-text"


def _reserved(world: World) -> str:
    """A one-stage running campaign whose attempt holds its reservation."""
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1")]
    cid = world.campaign(spec)["id"]
    world.tick_until(lambda: bool(world.attempts(cid)), worker=False)
    assert world.status(cid) == "running"
    return cid


def _state(world: World, cid: str) -> dict:
    return {
        "attempts": [dict(row) for row in world.attempts(cid)],
        "stages": [
            dict(row)
            for row in world.env.query(
                "SELECT * FROM campaign_stages WHERE campaign_id=? ORDER BY stage_key", cid
            )
        ],
        "held": world.row("SELECT held_cents, settled_cents FROM campaigns WHERE id=?", cid)[
            "held_cents"
        ],
    }


def _blocked_events(world: World, cid: str) -> list[dict]:
    return [
        e["payload"]
        for e in world.events_of("campaign.blocked")
        if e["payload"]["campaign_id"] == cid
    ]


def _break_spec(world: World, cid: str) -> str:
    """Store a spec the current models refuse (an unknown field), as after a stricter deploy."""
    original = world.row("SELECT spec_json FROM campaigns WHERE id=?", cid)["spec_json"]
    broken = json.loads(original)
    broken["unknown_field_from_old_code"] = SECRET_MARK
    world.env.execute("UPDATE campaigns SET spec_json=? WHERE id=?", json.dumps(broken), cid)
    return original


def test_an_unreadable_stored_spec_blocks_once_and_changes_no_attempt(tmp_path):
    world = World(tmp_path)
    cid = _reserved(world)
    _break_spec(world, cid)
    before = _state(world, cid)
    report = world.runner.tick()
    assert f"blocked:{cid}:{STORED_SPEC_UNREADABLE}" in report.actions
    for _ in range(5):
        world.runner.tick()
    row = world.row("SELECT status, stop_reason FROM campaigns WHERE id=?", cid)
    assert (row["status"], row["stop_reason"]) == ("blocked", STORED_SPEC_UNREADABLE)
    events = _blocked_events(world, cid)
    assert len(events) == 1
    assert events[0]["reason"] == STORED_SPEC_UNREADABLE
    assert events[0]["error_class"] == "ValidationError"
    assert events[0]["error_count"] >= 1
    assert SECRET_MARK not in json.dumps(events)
    assert world.events_of("campaign.completed") == []
    assert world.events_of(STORED_SPEC_EVENT) == []
    after = _state(world, cid)
    assert after["attempts"] == before["attempts"]
    assert after["stages"] == before["stages"]
    assert after["held"] == before["held"]


def test_a_non_json_stored_spec_is_unreadable_too(tmp_path):
    world = World(tmp_path)
    cid = _reserved(world)
    world.env.execute("UPDATE campaigns SET spec_json=? WHERE id=?", "{not json", cid)
    for _ in range(3):
        world.runner.tick()
    assert world.status(cid) == "blocked"
    events = _blocked_events(world, cid)
    assert [e["reason"] for e in events] == [STORED_SPEC_UNREADABLE]


def test_a_digest_mismatch_blocks_with_its_own_reason(tmp_path):
    world = World(tmp_path)
    cid = _reserved(world)
    before = _state(world, cid)
    world.env.execute("UPDATE campaigns SET spec_digest=? WHERE id=?", "0" * 64, cid)
    for _ in range(4):
        world.runner.tick()
    row = world.row("SELECT status, stop_reason FROM campaigns WHERE id=?", cid)
    assert (row["status"], row["stop_reason"]) == ("blocked", STORED_SPEC_DIGEST_MISMATCH)
    assert [e["reason"] for e in _blocked_events(world, cid)] == [STORED_SPEC_DIGEST_MISMATCH]
    assert _state(world, cid)["attempts"] == before["attempts"]
    assert world.events_of("campaign.completed") == []


def test_a_spec_readable_again_resumes_and_clears_the_reason(tmp_path):
    world = World(tmp_path)
    cid = _reserved(world)
    original = _break_spec(world, cid)
    world.runner.tick()
    assert world.status(cid) == "blocked"
    world.env.execute("UPDATE campaigns SET spec_json=? WHERE id=?", original, cid)
    world.runner.tick()
    row = world.row("SELECT status, stop_reason FROM campaigns WHERE id=?", cid)
    assert (row["status"], row["stop_reason"]) == ("running", None)
    unblocked = [e["payload"] for e in world.events_of("campaign.unblocked")]
    assert unblocked and unblocked[-1]["cleared_stop_reason"] == STORED_SPEC_UNREADABLE


def test_cancel_of_an_unreadable_campaign_is_recorded_once_and_finishes_when_readable(tmp_path):
    world = World(tmp_path)
    cid = _reserved(world)
    original = _break_spec(world, cid)
    world.runner.tick()
    assert world.status(cid) == "blocked"
    world.cancel({"id": cid})
    for _ in range(4):
        world.runner.tick()
    # The unwind needs each stage's provider from the spec: the cancel waits, never fails.
    assert world.status(cid) == "cancel_requested"
    notes = [e["payload"] for e in world.events_of(STORED_SPEC_EVENT)]
    assert notes == [
        {
            "campaign_id": cid,
            "reason": STORED_SPEC_UNREADABLE,
            "status": "cancel_requested",
            **{k: notes[0][k] for k in ("error_class", "error_count", "error_types", "locations")},
        }
    ]
    assert SECRET_MARK not in json.dumps(notes)
    world.env.execute("UPDATE campaigns SET spec_json=? WHERE id=?", original, cid)
    world.tick_all(cid, worker=False, limit=30)
    assert world.status(cid) == "cancelled"


def _snapshot(path):
    status = os.stat(path)
    return path.read_bytes(), status.st_mtime_ns


def _findings(world: World) -> dict:
    return {f.check_id: f for f in check_stored_state(world.env.state)}


def test_deploy_check_names_an_unreadable_campaign_and_writes_nothing(tmp_path):
    world = World(tmp_path)
    cid = _reserved(world)
    healthy = _findings(world)
    assert healthy["state.stored_specs"].status == "ok"
    assert healthy["state.stored_specs"].detail["checked"] == 1
    assert healthy["state.stored_attempts"].status == "ok"
    _break_spec(world, cid)
    database = world.env.state / "tessera.sqlite3"
    before = _snapshot(database)
    findings = _findings(world)
    assert _snapshot(database) == before
    spec = findings["state.stored_specs"]
    assert spec.status == "fail" and spec.area == "state"
    assert spec.detail["listed"] == [{"id": cid, "reason": STORED_SPEC_UNREADABLE}]
    payload = json.dumps([f.payload() for f in findings.values()])
    assert SECRET_MARK not in payload
    # The runner was not involved: the campaign is still running.
    assert world.status(cid) == "running"


def test_deploy_check_names_a_digest_mismatch_and_ignores_terminal_campaigns(tmp_path):
    world = World(tmp_path)
    cid = _reserved(world)
    world.env.execute("UPDATE campaigns SET spec_digest=? WHERE id=?", "0" * 64, cid)
    spec = _findings(world)["state.stored_specs"]
    assert spec.detail["listed"] == [{"id": cid, "reason": STORED_SPEC_DIGEST_MISMATCH}]
    world.env.execute("UPDATE campaigns SET status='failed' WHERE id=?", cid)
    spec = _findings(world)["state.stored_specs"]
    assert (spec.status, spec.detail["checked"]) == ("ok", 0)


def test_deploy_check_validates_stored_guardian_finals(tmp_path):
    world = World(tmp_path)
    cid = _reserved(world)
    attempt = world.attempts(cid)[0]["id"]
    world.env.execute(
        """INSERT INTO campaign_salad_attempts (attempt_id, phase, guardian_final_json,
        revision, updated_at) VALUES (?, 'leased', ?, 1, '2026-01-01T00:00:00Z')""",
        attempt,
        json.dumps({"status": "stopped", "mystery": SECRET_MARK}),
    )
    found = _findings(world)["state.stored_attempts"]
    assert found.status == "fail"
    assert found.detail["listed"] == [{"id": attempt, "reason": "guardian_final_unreadable"}]
    assert SECRET_MARK not in json.dumps(found.payload())


def test_deploy_check_skips_without_a_database_or_on_an_old_schema(tmp_path):
    world = World(tmp_path)
    missing = check_stored_state(tmp_path / "nothing-here")
    assert {f.status for f in missing} == {"skipped"}
    world.env.execute("UPDATE schema_meta SET schema_version=schema_version-1")
    old = check_stored_state(world.env.state)
    assert {f.status for f in old} == {"skipped"}
    assert all("after it migrates" in f.summary for f in old)


def test_a_spec_stored_by_this_code_round_trips_exactly(tmp_path):
    world = World(tmp_path)
    cid = _reserved(world)
    row = world.row("SELECT spec_json, spec_digest FROM campaigns WHERE id=?", cid)
    model = parse_spec_json(row["spec_json"])
    assert spec_digest(model) == row["spec_digest"]
    assert (
        spec_digest(parse_spec_json(json.dumps(model.model_dump(mode="json"))))
        == (row["spec_digest"])
    )
    verdict = verify_stored_spec(row["spec_json"], row["spec_digest"])
    assert (verdict.reason, verdict.detail) == (None, {})
    assert verdict.spec is not None
