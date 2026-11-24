"""A stage with public_artifact pins needs its fetch report (ADR 0097). Fakes only.

On the worker-grant path the local worker posts its fetch report inside ``report()``, before it
uploads outputs or posts its run receipt. An exited attempt whose stage declares pins and has no
report recorded before its run receipt fails closed (``public_fetch_unreported``) instead of
succeeding with every file ``declared_only``. Salad has no fetch report channel (its evidence
stays runtime-reported or declared_only), so the requirement does not apply there.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from campaign_runner_harness import World, dump
from test_campaign_models_v2 import _spec2
from test_campaign_public_artifact_spec import DIGEST, _public, _snapshot, _v2_stage

from tessera_control import campaign_provider_worker as worker_provider
from tessera_control.campaign_models import FAILURE_CLASSES, PUBLIC_FETCH_UNREPORTED
from tessera_control.campaign_provider_worker import WorkerGrantProvider, public_fetch_reported
from tessera_control.campaign_receipts import public_fetch_entry
from tessera_control.public_fetch_report import EVENT_TYPE
from tessera_control.store import _connect
from tessera_worker.runtime import CpuRuntime

HOST = "huggingface.co"


def _row(**over):
    row = {
        "input": "weights",
        "name": "weights.bin",
        "source_host": HOST,
        "sha256": DIGEST,
        "bytes": 10,
        "seconds": 0.25,
        "cached": False,
    }
    return {**row, **over}


class PublicWorld(World):
    """The harness world whose worker has a public input root (fetches are faked below)."""

    def _service(self, config):
        return super()._service(dataclasses.replace(config, public_root=self.tmp / "public"))


@pytest.fixture
def world(tmp_path, monkeypatch):
    def fake_fetch(self, operation_id, plan):
        directory = Path(self.public_root) / f"tessera-public-{operation_id}"
        (directory / "weights").mkdir(parents=True, exist_ok=True)
        return {"directory": str(directory), "rows": [_row()]}

    monkeypatch.setattr(CpuRuntime, "_fetch_public", fake_fetch)
    built = PublicWorld(tmp_path)
    worker_provider.register_from_config(
        {"local_docker": {"worker_principal_id": built.env.worker["id"]}}
    )
    try:
        yield built
    finally:
        worker_provider.register_from_config(None)


def _run(world, *inputs):
    campaign = world.campaign(_spec2(_v2_stage(world, "a", *inputs)))
    world.tick_all(campaign["id"], limit=80)
    (attempt,) = world.attempts(campaign["id"])
    return campaign, attempt


def _receipt_attempt(world, campaign_id):
    row = world.row("SELECT * FROM campaign_receipts WHERE campaign_id=?", campaign_id)
    (stage,) = json.loads(row["receipt_json"])["stages"]
    return stage["attempts"][-1]


def test_pins_with_a_valid_report_succeed_with_provider_reported_rows(world):
    campaign, attempt = _run(world, _snapshot(world), _public())
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    assert len(world.events_of(EVENT_TYPE)) == 1
    entry = _receipt_attempt(world, campaign["id"])
    (file,) = entry["public_fetch"]["files"]
    assert file["evidence"] == "provider_reported" and file["bytes"] == 10
    assert "refused" not in entry["public_fetch"]
    assert entry["failure_class"] is None


def test_pins_without_a_report_fail_closed_and_the_receipt_says_why(world, monkeypatch):
    monkeypatch.setattr(CpuRuntime, "_report_public_fetch", lambda self: None)
    campaign, attempt = _run(world, _snapshot(world), _public())
    assert world.status(campaign["id"]) == "failed", dump(world, campaign["id"])
    assert world.events_of(EVENT_TYPE) == []
    assert attempt["status"] == "failed_permanent"
    assert attempt["failure_class"] == PUBLIC_FETCH_UNREPORTED
    assert attempt["failure_detail"].startswith("public_fetch_unreported:")
    assert attempt["exit_code"] == 0  # the program itself exited cleanly
    entry = _receipt_attempt(world, campaign["id"])
    assert entry["failure_class"] == PUBLIC_FETCH_UNREPORTED
    public = entry["public_fetch"]
    assert public["refused"]["code"] == PUBLIC_FETCH_UNREPORTED
    assert public["refused"]["reason"].startswith("public_fetch_unreported:")
    (file,) = public["files"]
    assert file["evidence"] == "declared_only" and file["bytes"] is None
    assert len(world.worker_runs) == 1  # a permanent failure: never retried


def test_a_report_that_contradicts_the_plan_is_refused_and_never_passes(world, monkeypatch):
    original = CpuRuntime._fetch_public

    def contradicting(self, operation_id, plan):
        fetched = original(self, operation_id, plan)
        return {**fetched, "rows": [_row(source_host="evil.example.org")]}

    monkeypatch.setattr(CpuRuntime, "_fetch_public", contradicting)
    campaign = world.campaign(_spec2(_v2_stage(world, "a", _snapshot(world), _public())))
    for _ in range(40):
        world.runner.tick()
        world.poller.run(max_cycles=1)
        if world.status(campaign["id"]) in {"succeeded", "failed", "stopped", "cancelled"}:
            break
    # Control refused the report (no event); the worker stops before outputs or a receipt.
    assert world.events_of(EVENT_TYPE) == []
    assert world.status(campaign["id"]) != "succeeded"
    assert all(a["status"] != "succeeded" for a in world.attempts(campaign["id"]))
    assert (
        world.env.query(
            "SELECT 1 FROM execution_run_receipts WHERE grant_id IN "
            "(SELECT grant_id FROM campaign_stage_attempts WHERE campaign_id=?)",
            campaign["id"],
        )
        == []
    )


def test_a_stage_without_pins_is_unchanged(world, monkeypatch):
    monkeypatch.setattr(CpuRuntime, "_report_public_fetch", lambda self: None)
    campaign, attempt = _run(world, _snapshot(world))
    assert world.status(campaign["id"]) == "succeeded", dump(world, campaign["id"])
    assert attempt["failure_class"] is None
    assert "public_fetch" not in _receipt_attempt(world, campaign["id"])


# -- the check itself -------------------------------------------------------------------------


def test_a_report_recorded_after_the_run_receipt_does_not_count(world):
    campaign, attempt = _run(world, _snapshot(world), _public())
    assert world.status(campaign["id"]) == "succeeded"
    rt = world.runner  # the real facade: stage_for and one over the same database
    attempt = dict(attempt)
    assert public_fetch_reported(rt, attempt)
    assert not public_fetch_reported(rt, {**attempt, "operation_id": None})
    # The event must be in the attempt's campaign's project: an unknown campaign matches none.
    stage = rt.stage_for(attempt)
    rt_other = SimpleNamespace(stage_for=lambda _: stage, one=rt.one)
    assert not public_fetch_reported(rt_other, {**attempt, "campaign_id": "no-such-campaign"})
    database = world.env.state / "tessera.sqlite3"
    with _connect(database) as connection:
        # Move the report after the receipt: a late report never rescues an exited attempt.
        top = connection.execute("SELECT max(cursor) FROM events").fetchone()[0]
        connection.execute("UPDATE events SET cursor=? WHERE event_type=?", (top + 1, EVENT_TYPE))
    assert not public_fetch_reported(rt, attempt)
    observed = WorkerGrantProvider(SimpleNamespace()).observe(rt, attempt)
    assert observed.outcome == "failed_permanent"
    assert observed.failure_class == PUBLIC_FETCH_UNREPORTED


def test_the_code_is_a_closed_failure_class():
    assert PUBLIC_FETCH_UNREPORTED in FAILURE_CLASSES


# -- Salad ------------------------------------------------------------------------------------


def test_salad_has_no_report_channel_and_its_pinned_stage_still_passes():
    """Salad posts no fetch report (the endpoint accepts local-cpu operations only), so the
    requirement is scoped to the worker-grant provider: a Salad attempt with pins and no report
    is judged by its own verdict, and its receipt files stay declared_only, with no refusal."""
    from tessera_control.campaign_provider_salad import SaladRouteProvider

    class NoDatabase:
        def __getattr__(self, name):
            raise AssertionError(f"Salad observation must not consult {name}")

    attempt = {
        "salad_verdict": "ok",
        "salad_evidence_digest": "e" * 64,
        "manifest_id": "m1",
        "operation_id": "op-salad",
    }
    observed = SaladRouteProvider.observe(SimpleNamespace(), NoDatabase(), attempt)
    assert observed.outcome == "succeeded" and observed.failure_class is None
    stage = SimpleNamespace(
        inputs=(
            SimpleNamespace(
                kind="public_artifact",
                name="weights",
                redirect_hosts=(),
                public_files=(("w", "https://models.example.com/w.bin", "c" * 64, 1000),),
            ),
        )
    )
    entry = public_fetch_entry("salad", stage, None)
    assert entry["files"][0]["evidence"] == "declared_only"
    assert "refused" not in entry
