"""A refused exit resolution on one attempt must not stop a cancel_requested campaign.

``_resolve_exited_attempts`` and ``_close_unreported_exits`` run before ``_drive_cancel`` in the
campaign pass; a ``StepFailed`` or ``FrameRefused`` from one attempt used to abort the pass, so the
cancel never finished. Fakes only: in-process control."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from campaign_runner_harness import World, dump, stage_spec

from tessera_control import execution_frames as frames
from tessera_control.campaign_runner import CampaignRunner
from tessera_control.campaign_steps import StepFailed

METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]
TERMINAL = {"succeeded", "stopped", "failed", "cancelled"}


def _cancelled_world(tmp_path):
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC)]
    campaign = world.campaign(spec)
    cid = campaign["id"]
    world.tick_until(
        lambda: any(a["status"] == "admitted" for a in world.attempts(cid)), worker=False
    )
    world.cancel(campaign)
    return world, cid


def _refuse_settlement(ctx, attempt):
    raise StepFailed("reconcile_grant", "settlement_unavailable", "provider is not registered")


def _refuse_frame(ctx, attempt):
    raise frames.FrameRefused("frame is not open")


@pytest.mark.parametrize(
    ("method", "refusal"),
    [
        ("_resolve_exited", _refuse_settlement),
        ("_close_unreported_exit", _refuse_frame),
        ("_resolve_exited", _refuse_frame),
        ("_close_unreported_exit", _refuse_settlement),
    ],
)
def test_a_cancel_is_still_driven_when_an_exit_resolution_is_refused(
    tmp_path, monkeypatch, method, refusal
):
    world, cid = _cancelled_world(tmp_path)
    assert world.status(cid) == "cancel_requested", dump(world, cid)
    refused = []

    def refuse(self, ctx, attempt):
        refused.append(attempt["id"])
        return refusal(ctx, attempt)

    monkeypatch.setattr(CampaignRunner, method, refuse)
    # The admitted attempt stands in for one whose exit was seen (the loops' own row filters are
    # covered by the exit-resolution tests); only the per-attempt refusal is under test here.
    rows = CampaignRunner._rows

    def admitted_rows(self, sql, params=()):
        if sql.lstrip().startswith("SELECT * FROM campaign_stage_attempts WHERE campaign_id=?"):
            sql = "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND status='admitted'"
        return rows(self, sql, params)

    monkeypatch.setattr(CampaignRunner, "_rows", admitted_rows)
    driven = []
    original = CampaignRunner._drive_cancel
    monkeypatch.setattr(
        CampaignRunner,
        "_drive_cancel",
        lambda self, ctx: (driven.append(ctx.campaign["id"]), original(self, ctx))[1],
    )
    for _ in range(6):
        world.runner.tick()  # must not raise: the refusal is logged and the pass goes on
        world.poller.run(max_cycles=1)
    assert refused, "the refusal was never exercised\n" + dump(world, cid)
    assert driven, "the cancel was never driven\n" + dump(world, cid)
    assert world.status(cid) in {"cancel_requested", *TERMINAL}, dump(world, cid)


@pytest.mark.parametrize(
    ("runner_method", "item_method", "refusal"),
    [
        ("_resolve_exited_attempts", "_resolve_exited", _refuse_settlement),
        ("_resolve_exited_attempts", "_resolve_exited", _refuse_frame),
        ("_close_unreported_exits", "_close_unreported_exit", _refuse_settlement),
        ("_close_unreported_exits", "_close_unreported_exit", _refuse_frame),
    ],
)
def test_the_next_attempt_is_still_processed_after_one_is_refused(
    tmp_path, monkeypatch, caplog, runner_method, item_method, refusal
):
    runner = CampaignRunner(World(tmp_path).env.state)
    rows = [{"id": "attempt-a"}, {"id": "attempt-b"}]
    monkeypatch.setattr(runner, "_rows", lambda sql, params=(): rows)
    seen = []

    def one(ctx, attempt):
        seen.append(attempt["id"])
        if attempt["id"] == "attempt-a":
            return refusal(ctx, attempt)
        return True

    monkeypatch.setattr(runner, item_method, one)
    ctx = SimpleNamespace(campaign={"id": "campaign-x"})
    with caplog.at_level(logging.WARNING, logger="tessera.campaign_runner"):
        getattr(runner, runner_method)(ctx)
    assert seen == ["attempt-a", "attempt-b"]
    assert any("attempt-a" in record.getMessage() for record in caplog.records)
