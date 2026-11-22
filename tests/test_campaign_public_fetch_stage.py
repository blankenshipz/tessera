"""A public-input fetch stage with allowlisted egress feeds a later no-egress stage."""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest
from campaign_runner_harness import ScriptedAdapter, World, dump, stage_spec

from tessera_control.campaign_receipts import build_receipt
from tessera_control.egress_models import EgressPolicy
from tessera_worker.service import DevelopmentWorkerService

HOST = "downloads.example.org"
PAYLOAD = json.dumps({"weights": [1, 2, 3]})
EGRESS = {
    "profile": "allowlist-proxy-v1",
    "destinations": [{"host": HOST, "port": 443}],
    "max_egress_bytes": 1000,
    "sources": [{"host": HOST, "license": "Apache-2.0", "source": f"https://{HOST}/model"}],
}


class EgressAdapter(ScriptedAdapter):
    """The scripted adapter plus the journal surface a real egress adapter provides."""

    def egress_summary(self, operation):
        destination = operation.egress["destinations"][0]
        return "f" * 64, {
            "connections_allowed": 1,
            "connections_denied": 0,
            "bytes_up": 10,
            "bytes_down": 90,
            "destinations": [
                {
                    **destination,
                    "decision": "allow",
                    "connections": 1,
                    "bytes_up": 10,
                    "bytes_down": 90,
                }
            ],
        }

    def teardown_egress(self, operation):
        self.calls.append("teardown_egress")


class EgressWorld(World):
    def _service(self, config):
        exit_code, outputs = self.scenarios.pop(0) if self.scenarios else self.default_scenario
        adapter = EgressAdapter(exit_code, outputs)
        adapter.development_output_root = (
            config.development_output_root / "development-only-cpu-outputs" / "workspaces"
        )
        self.worker_runs.append(config.grant_id)
        self.adapters.append(adapter)

        def finish(_):
            adapter.state = "exited"

        return DevelopmentWorkerService(config, channel=self.channel, adapter=adapter, sleep=finish)


def test_fetch_stage_output_feeds_a_no_egress_stage(tmp_path):
    world = EgressWorld(tmp_path)
    world.adapters = []
    world.default_scenario = (0, {"data.json": PAYLOAD})
    spec = world.env.spec()
    spec["stages"] = [
        stage_spec(world.env, "fetch", egress=EGRESS),
        stage_spec(
            world.env,
            "consume",
            inputs=[{"kind": "stage", "name": "up", "stage": "fetch", "paths": ["data.json"]}],
        ),
    ]
    campaign = world.campaign(spec)
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    outputs = world.env.query(
        "SELECT * FROM campaign_stage_io WHERE campaign_id=? AND direction='output' "
        "AND stage_key='fetch'",
        campaign["id"],
    )
    assert outputs[0]["sha256"] == hashlib.sha256(PAYLOAD.encode()).hexdigest()
    assert outputs[0]["data_class"] == "private_derived"
    journals = world.events_of("execution.egress_journal_recorded")
    assert len(journals) == 1
    operations = {a.operation.operation_id: a.operation for a in world.adapters if a.operation}
    profiles = sorted(o.network_profile for o in operations.values())
    assert profiles == ["allowlist-proxy-v1", "none"]
    # The receipt binds the allowlisted host, the declared license and the output sha256.
    row = world.row("SELECT * FROM campaign_receipts WHERE campaign_id=?", campaign["id"])
    receipt = json.loads(row["receipt_json"])
    stages = {stage["stage_key"]: stage for stage in receipt["stages"]}
    (attempt,) = stages["fetch"]["attempts"]
    assert attempt["egress"]["policy"]["destinations"] == [{"host": HOST, "port": 443}]
    assert attempt["egress"]["policy"]["sources"][0]["license"] == "Apache-2.0"
    assert attempt["egress"]["journal"]["journal_sha256"] == "f" * 64
    assert attempt["egress"]["journal"]["summary"]["destinations"][0]["host"] == HOST
    (item,) = [io for io in attempt["io"] if io["direction"] == "output"]
    assert item["sha256"] == hashlib.sha256(PAYLOAD.encode()).hexdigest()
    assert item["data_class"] == "private_derived"
    assert "egress" not in stages["consume"]["attempts"][0]  # network stays none downstream
    assert stages["consume"]["attempts"][0]["input_data_class"] == "private_derived"
    connection = sqlite3.connect(world.env.state / "tessera.sqlite3")
    connection.row_factory = sqlite3.Row
    try:
        current = connection.execute("SELECT * FROM campaigns WHERE id=?", (campaign["id"],))
        assert (
            build_receipt(connection, current.fetchone(), "succeeded")[1] == row["receipt_digest"]
        )
    finally:
        connection.close()


def test_egress_sources_are_optional_and_validated():
    base = {k: v for k, v in EGRESS.items() if k != "sources"}
    assert "sources" not in EgressPolicy.model_validate(base).model_dump(mode="json")
    good = {"host": HOST, "license": "MIT", "source": "https://" + HOST}
    for sources in (
        [{**good, "host": "other.example.org"}],  # not an allowlisted host
        [good, good],  # duplicate host
        [{**good, "license": "UNKNOWN"}],  # not an identified license
        [{**good, "source": "has space"}],
        [],
    ):
        with pytest.raises(ValueError):
            EgressPolicy.model_validate({**base, "sources": sources})


def test_receipt_records_an_unclosed_tunnel_with_unknown_bytes(tmp_path, monkeypatch):
    def interrupted(self, operation):
        destination = operation.egress["destinations"][0]
        return "e" * 64, {
            "connections_allowed": 1,
            "connections_denied": 0,
            "bytes_up": 0,
            "bytes_down": 0,
            "connections_unclosed": 1,
            "destinations": [
                {
                    **destination,
                    "decision": "allow",
                    "connections": 1,
                    "bytes_up": 0,
                    "bytes_down": 0,
                    "unclosed": 1,
                }
            ],
        }

    monkeypatch.setattr(EgressAdapter, "egress_summary", interrupted)
    world = EgressWorld(tmp_path)
    world.adapters = []
    world.default_scenario = (0, {"data.json": PAYLOAD})
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "fetch", egress=EGRESS)]
    campaign = world.campaign(spec)
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    row = world.row("SELECT * FROM campaign_receipts WHERE campaign_id=?", campaign["id"])
    receipt = json.loads(row["receipt_json"])
    (attempt,) = receipt["stages"][0]["attempts"]
    summary = attempt["egress"]["journal"]["summary"]
    assert summary["connections_allowed"] == 1 and summary["connections_unclosed"] == 1
    assert summary["destinations"][0]["unclosed"] == 1
