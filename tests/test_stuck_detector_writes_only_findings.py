"""The stuck-campaign detector records findings and changes nothing else.

README: the detector "never cancels, resumes, resolves or retries anything". A scan may write
only its own records: the finding and settings tables and its own events
(``campaign.stuck_detected`` / ``_acknowledged`` / ``_resolved`` / ``_settings_set`` /
``_scan_failed``). Every other table, and every other event, must be byte-identical afterwards.
The loop that offers findings to a person is held to the same rule by its allowlisted control
adapter (``test_the_adapter_refuses_every_other_route_before_any_request`` in
``test_stuck_loop_runner.py``)."""

from __future__ import annotations

import sqlite3

from test_campaign_stuck import NOW, World

from tessera_control import campaign_stuck as stuck
from tessera_control.campaign_runner import CampaignRunner
from tessera_control.campaign_store import CampaignStore

# The only tables the detector writes, and the only events it emits.
OWN_TABLES = {"campaign_stuck_findings", "campaign_stuck_settings"}
OWN_EVENTS = (
    stuck.DETECTED,
    stuck.ACKNOWLEDGED,
    stuck.RESOLVED,
    stuck.SETTINGS_SET,
    stuck.STUCK_SCAN_FAILED,
)


def _snapshot(path) -> dict[str, list]:
    """Every table except the detector's own, and the events except the detector's own."""
    marks = ",".join("?" for _ in OWN_EVENTS)
    with sqlite3.connect(path) as db:
        names = [
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
            if row[0] not in OWN_TABLES
        ]
        out = {}
        for name in names:
            if name == "events":
                query = f'SELECT * FROM "events" WHERE event_type NOT IN ({marks}) ORDER BY 1'
                out[name] = db.execute(query, OWN_EVENTS).fetchall()
            else:
                out[name] = db.execute(f'SELECT * FROM "{name}" ORDER BY 1').fetchall()
        return out


def _stuck_world(tmp_path) -> World:
    """A campaign that is blocked, with an unknown attempt on a stale lease: three findings' worth
    of reasons to cancel, resume or retry, all of which the detector must leave alone."""
    world = World(tmp_path)
    world.set_status("blocked", stop_reason="budget_refused:s1", age=3600)
    world.grant_and_lease(lease_expired_ago=100000)
    world.attempt("unknown", age=100000, grant_id="grant_1")
    return world


def test_a_scan_changes_nothing_but_its_own_findings_and_events(tmp_path, monkeypatch):
    world = _stuck_world(tmp_path)
    before = _snapshot(world.db_path)

    # The detector must not reach any action that cancels, resumes, approves or drives a campaign.
    def forbidden(*_a, **_k):
        raise AssertionError("the stuck detector called an action entry point")

    for owner, names in (
        (CampaignStore, ("cancel_campaign", "approve_campaign", "submit_campaign")),
        (CampaignRunner, ("tick", "_drive", "_settle")),
    ):
        for name in names:
            monkeypatch.setattr(owner, name, forbidden)

    opened = world.scan()["opened"]
    assert opened, "the fixture must actually be stuck, or this test proves nothing"
    assert stuck.scan_all(world.db_path, clock=lambda: NOW) >= 0
    world.scan()  # a repeat pass is just as inert
    assert world.findings(), "the detector must have recorded its findings"
    assert _snapshot(world.db_path) == before


def test_the_snapshot_would_notice_a_change_to_the_work(tmp_path):
    """Guards the guard: the comparison fails if a campaign row is touched."""
    world = _stuck_world(tmp_path)
    before = _snapshot(world.db_path)
    world.set_status("running", age=1)
    assert _snapshot(world.db_path) != before
