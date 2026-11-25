"""The owner-bound review delegation (ADR 0150) clears the reviews of a stage that was placed on
its Salad alternate, and a stage the driver truly cannot preview still stops for a person with
the error recorded. Synthetic fixtures only: the real stores and runner, test doubles for the
CPU host, executor and guardian; no provider call."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest
from provider_trust_fixture import DERIVED, install_trust
from salad_campaign_fixture import (  # noqa: F401
    contract_data,
    salad_provider_reset,
    salad_spec_v2,
)
from test_campaign_placement_salad import NEAR, _stage, near  # noqa: F401
from test_owner_bound_review_delegation import Direct

from tessera_control.campaign_salad import StepFailed
from tessera_control.campaign_salad_adapter import adapted_manifest
from tessera_control.snapshot_composition import composed_inventory
from tessera_control.workspace import inventory_digest


@pytest.fixture
def world(tmp_path, near):  # noqa: F811
    world = Direct(tmp_path, deferred=False)
    near(world, 0)  # the CPU host is busy, so the stage is placed on its Salad alternate
    stage = _stage(world.env)
    stage["max_duration_seconds"] = 600  # inside the test credentials' window
    stage["grant_ttl_seconds"] = 1200
    world.spec = salad_spec_v2(world.env, [stage])
    return world


def _to_review_stop(world: Direct) -> None:
    world.tick_until_phase("awaiting_lease")
    world.lease_and_package()
    world.tick_until_phase("route_started")


def test_a_stage_placed_on_its_salad_alternate_is_cleared_by_the_bound_delegation(world):
    delegation = world.delegate()
    world.approve_direct(review_delegation_id=delegation["id"])
    _to_review_stop(world)
    (placed,) = world.events("campaign.stage_placed")
    assert placed["provider_id"] == "salad"
    assert world.declined() == []
    assert world.events("campaign.stage_awaiting_human") == []
    uses = [dict(r) for r in world.env.query("SELECT * FROM campaign_review_preapproval_uses")]
    assert sorted(u["kind"] for u in uses) == ["plan", "source_assessment", "source_use"]
    assert {u["decision_class"] for u in uses} == {"campaign_delegated"}
    assert world.row("SELECT verdict FROM research_sources")["verdict"] == "supported"


def test_a_stage_that_cannot_be_previewed_stops_for_a_person_and_records_why(world, monkeypatch):
    def refuse(self, ctx, key, provider_id=None):
        raise StepFailed("salad", "step_refused", "salad_config_missing")

    monkeypatch.setattr(type(world.driver), "preview_review_digests", refuse)
    delegation = world.delegate()
    world.approve_direct(review_delegation_id=delegation["id"])
    for _ in range(8):
        world.runner.tick()
    (declined,) = world.events("campaign.preapproval_declined")
    assert declined["kind"] == "delegated_review"
    assert declined["reason"] == "review_preview_failed:StepFailed:salad_config_missing"
    assert world.events("campaign.review_preapproval_delegated") == []
    assert world.row("SELECT 1 FROM campaign_review_preapprovals") is None
    assert world.events("campaign.stage_awaiting_human")


DATA_MAX_BYTES = 4096


def _composed_digest(world: Direct, names: list[str]) -> str:
    """The digest of the snapshot the runner composes from the stage's snapshot inputs (a pure
    function of their file digests), which the stage adapter declares as the workload's input."""
    row = world.env.query(
        "SELECT manifest_json FROM source_snapshots WHERE id=?", world.env.snapshot_id
    )[0]
    listed = json.loads(row["manifest_json"])["entries"]
    entries = [
        (f"{name}/{e['relative_path']}", e["sha256"], e["size"], "imported_unverified")
        for name in names
        for e in listed
    ]
    return inventory_digest(composed_inventory(entries))


def _adapted_workload(stage: dict, data_digest: str) -> dict:
    """A gpu_runtime_v1 workload as the stage adapter writes it: the stage's own argv as the
    image entry, and a reviewed staged input that is the data snapshot, not the code input."""
    raw = json.dumps(
        adapted_manifest(list(stage["argv"]), [], []),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    seconds = 60
    out = stage["outputs"][0]["max_bytes"]
    return contract_data(
        runtime="gpu_runtime_v1",
        env={},
        workload_seconds=seconds,
        outputs=[{"name": "output.json", "max_bytes": out}],
        egress={"kind": "provider_broad", "fetches": []},
        argv_template=[
            "v1",
            "{operation}",
            "{epoch}",
            "{custody_host}",
            "b" * 64,
            str(seconds),
            str(out),
            "1",
            hashlib.sha256(raw).hexdigest(),
            f"input:{data_digest}:{DATA_MAX_BYTES}",
            *[f"b64:{encoded[i : i + 200]}" for i in range(0, len(encoded), 200)],
        ],
        input={
            "kind": "staged_snapshot",
            "snapshot_digest": data_digest,
            "max_bytes": DATA_MAX_BYTES,
        },
    )


def _stored_shape(world: Direct) -> None:
    """The shape of a stored campaign: the stage prefers the CPU host, its Salad alternate is an
    adapted CPU workload whose reviewed input is a staged data snapshot distinct from the stage's
    ``code`` snapshot input."""
    stage = _stage(world.env)
    stage["max_duration_seconds"] = 600
    stage["grant_ttl_seconds"] = 1200
    (alternate,) = stage["alternate_providers"]
    config = dict(alternate["provider_config"])
    # The stage's inputs: the code snapshot and a data snapshot (both composed by the runner into
    # one staged input, so the route's data manifest is not the code input's digest).
    code = stage["inputs"][0]
    stage["inputs"] = [code, {**code, "name": "data"}]
    data_digest = _composed_digest(world, ["code", "data"])
    assert data_digest != code["digest"]
    config |= {"adapted": True, "workload": _adapted_workload(stage, data_digest)}
    alternate["provider_config"] = config
    # The composed snapshot is derived data: the Salad worker is bound to receive it.
    install_trust({"salad": (DERIVED, [world.env.worker["id"]])})
    world.spec = salad_spec_v2(world.env, [stage])


def test_the_real_stored_shape_is_cleared_on_the_placed_salad_candidate(tmp_path, near):  # noqa: F811
    world = Direct(tmp_path, deferred=False)
    near(world, 0)
    _stored_shape(world)
    delegation = world.delegate()
    world.approve_direct(review_delegation_id=delegation["id"])
    _to_review_stop(world)
    (placed,) = world.events("campaign.stage_placed")
    assert placed["provider_id"] == "salad"
    assert world.declined() == []
    assert world.events("campaign.stage_awaiting_human") == []
    uses = [dict(r) for r in world.env.query("SELECT * FROM campaign_review_preapproval_uses")]
    assert sorted(u["kind"] for u in uses) == ["plan", "source_assessment", "source_use"]


def test_a_validation_failure_is_recorded_with_its_message_and_nothing_environmental():
    from pydantic import BaseModel, field_validator

    from tessera_control.campaign_salad import _preview_failure

    class Model(BaseModel):
        value: str

        @field_validator("value")
        @classmethod
        def check(cls, value):
            raise ValueError("workload input must be the route's manifests.data snapshot")

    try:
        Model(value="/var/lib/someone/" + "a" * 64)
    except Exception as error:  # noqa: BLE001
        reason = _preview_failure(error)
    assert reason.startswith("review_preview_failed:ValidationError:")
    assert "workload input must be the route's manifests.data snapshot" in reason
    assert "value_error" in reason
    assert "/var/lib" not in reason and "a" * 12 not in reason and "someone" not in reason
    assert "," not in reason


def test_a_validation_message_with_paths_and_digests_is_redacted():
    from pydantic import BaseModel, field_validator

    from tessera_control.campaign_salad import _preview_failure

    class Model(BaseModel):
        value: str

        @field_validator("value")
        @classmethod
        def check(cls, value):
            raise ValueError(f"bad /var/lib/someone/x {'b' * 64} host@example.com, ~/secret")

    try:
        Model(value="x")
    except Exception as error:  # noqa: BLE001
        reason = _preview_failure(error)
    for leaked in ("/var/lib", "someone", "b" * 12, "example.com", "secret"):
        assert leaked not in reason
    assert "bad" in reason
