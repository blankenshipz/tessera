"""A reported transfer outside the egress allowlist, driven through the runner (ADR 0134).

The stage has no ``public_artifact`` inputs, so its allowlist is empty: any reported transfer
is a violation. The attempt is resolved, its evidence is not taken and it concludes
``failed_permanent`` with ``egress_violation``; no output is indexed or linked into lineage,
and the receipt still records the refused transfer. Fakes only: the provider's reported rows
come from a patched hook, no network and no provider calls.
"""

from __future__ import annotations

import json

from campaign_runner_harness import World, dump, stage_spec

from tessera_control import campaign_receipts
from tessera_control.campaign_receipts import public_fetch_entry

OFF = {
    "url": "https://elsewhere.example.org/w.bin",
    "sha256": "d" * 64,
    "bytes": 5,
    "seconds": 0.1,
}


def _report(monkeypatch, rows):
    """The provider reports ``rows`` as the attempt's transfers (the provider-neutral hook)."""
    monkeypatch.setattr(
        campaign_receipts,
        "_retained_fetched",
        lambda connection, provider_id, attempt, spec_stage=None: rows,
    )


def _run(world: World) -> dict:
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1")]
    campaign = world.campaign(spec)
    world.tick_all(campaign["id"])
    return campaign


def _receipt(world: World, campaign_id: str) -> dict:
    row = world.row("SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", campaign_id)
    assert row is not None, dump(world, campaign_id)
    return json.loads(row["receipt_json"])


def _attempt_entries(receipt: dict) -> list[dict]:
    return [attempt for stage in receipt["stages"] for attempt in stage.get("attempts", [])]


def test_a_violation_fails_the_attempt_without_lineage_and_stays_on_the_receipt(
    tmp_path, monkeypatch
):
    _report(monkeypatch, [OFF])
    world = World(tmp_path)
    campaign = _run(world)
    cid = campaign["id"]

    attempts = world.attempts(cid)
    assert attempts, dump(world, cid)
    assert {(a["status"], a["failure_class"]) for a in attempts} == {
        ("failed_permanent", "egress_violation")
    }, dump(world, cid)
    assert all("elsewhere.example.org" in a["failure_detail"] for a in attempts)
    assert all("/w.bin" not in a["failure_detail"] for a in attempts)
    assert world.status(cid) != "succeeded"

    # Resolved as failed and the evidence was not taken: no output indexed, no artifact link,
    # no lineage node or edge for this campaign.
    outputs = world.env.query(
        "SELECT * FROM campaign_stage_io WHERE campaign_id=? AND direction='output'", cid
    )
    assert outputs == []
    steps = {
        row["step"]
        for row in world.env.query(
            "SELECT step FROM campaign_step_intents WHERE campaign_id=? AND status='done'", cid
        )
    }
    assert "resolve_attempt" in steps, dump(world, cid)
    assert not steps & {"link_artifacts", "register_lineage_node", "record_lineage_edge"}

    # The violation is on the receipt even though the stage has no public_artifact input.
    receipt = _receipt(world, cid)
    entries = _attempt_entries(receipt)
    assert entries and all(e["status"] == "failed_permanent" for e in entries)
    for entry in entries:
        fetch = entry["public_fetch"]
        assert fetch["egress_verdict"] == "violation" and fetch["files"] == []
        assert fetch["refused_transfers"] == [
            {"host": "elsewhere.example.org", "reason": "not_allowlisted"}
        ]
        assert "https://" not in json.dumps(fetch)
    assert receipt["lineage"] == {"node_ids": [], "edge_ids": []}


def test_no_reported_transfer_leaves_the_stage_and_its_receipt_as_they_were(tmp_path, monkeypatch):
    _report(monkeypatch, None)
    world = World(tmp_path)
    campaign = _run(world)
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    for entry in _attempt_entries(_receipt(world, campaign["id"])):
        assert "public_fetch" not in entry


def test_receipt_entry_for_a_stage_without_public_inputs():
    from types import SimpleNamespace

    stage = SimpleNamespace(inputs=(), egress=None)
    assert public_fetch_entry("local-docker", stage, None) is None
    assert public_fetch_entry("local-docker", stage, []) is None
    entry = public_fetch_entry("local-docker", stage, [OFF])
    assert entry["egress_verdict"] == "violation"
    assert entry["refused_transfers"] == [
        {"host": "elsewhere.example.org", "reason": "not_allowlisted"}
    ]
    assert entry["files"] == [] and entry["hosts"] == []
