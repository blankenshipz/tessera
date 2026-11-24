"""A failed campaign attempt is never a result (docs/campaigns.md, Receipt).

A stage that exits non-zero may still have written a metrics file. Its numbers must not be
derived as measurements, must not trip a stop rule, and the receipt must show an operational
failure with no scientific verdict. Fakes only: no provider, network or credential.
"""

from __future__ import annotations

import json

import pytest
from campaign_runner_harness import World, dump, stage_spec

METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]
STOP = [{"metric": "score", "op": "gt", "value": 2.5}]


@pytest.mark.parametrize("exit_code", [1, 7])
def test_a_failed_attempt_with_a_metrics_file_yields_no_measurement_or_stop_rule(
    tmp_path, exit_code
):
    world = World(tmp_path)
    world.scenarios = [(exit_code, {"data.json": json.dumps({"score": 3})})]
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC, stop_when=STOP)]
    campaign = world.campaign(spec)
    cid = campaign["id"]
    world.tick_all(cid)
    assert world.status(cid) == "failed", dump(world, cid)
    assert world.stages(cid) == {"s1": "failed"}
    for attempt in world.attempts(cid):
        assert attempt["status"] != "succeeded"
        values = [m.get("value") for m in json.loads(attempt["measurements_json"] or "[]")]
        assert all(value is None for value in values)
    row = world.row("SELECT * FROM campaign_receipts WHERE campaign_id=?", cid)
    receipt = json.loads(row["receipt_json"])
    assert receipt["stop_reason"] != "stop_rule:s1.score"
    assert receipt["stages"][0]["stop_results"] == []
    assert receipt["verdict"] == {"operational": "stop", "scientific_verdict": "not_assessed"}
    assert receipt["evidence_class"] == "operational"
    assert "pending_human_approval" not in json.dumps(receipt)
