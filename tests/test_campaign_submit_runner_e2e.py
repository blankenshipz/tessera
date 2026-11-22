"""SubmitRunner through the real control routes (synthetic data only)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from test_campaign_submissions import Sub
from test_campaign_submit_action_contract import HttpAdapter

from tessera_agent.campaign_submit_action import CampaignSubmitAction, SubmitPolicy
from tessera_agent.campaign_submit_runner import (
    RunnerJournal,
    RunnerSettings,
    SubmitRunner,
)
from tessera_agent.standing_loop import KillSwitch
from tessera_control import program_budget
from tessera_control.api import create_app


@pytest.fixture
def world(tmp_path):
    program_budget.reset()
    sub = Sub(tmp_path)
    sub.agent()
    app = create_app(sub.env.state)
    app.state.agent_submission = sub.on
    adapter = HttpAdapter(TestClient(app), sub.env.project["id"])
    clock_now = [0.0]
    settings = RunnerSettings(tmp_path / "j.jsonl", sub.marker, max_ticks=2)
    journal = RunnerJournal(settings.journal_path, lambda: clock_now[0])
    kill = KillSwitch(sub.marker)
    runner = SubmitRunner(
        lambda record: CampaignSubmitAction(
            adapter, kill, record, SubmitPolicy(adapter.project_id), environ={}
        ),
        kill,
        journal,
        settings,
        lambda: clock_now[0],
        lambda s: clock_now.__setitem__(0, clock_now[0] + s),
    )
    yield sub, app, adapter, runner, journal
    journal.close()
    program_budget.reset()


def lines(journal):
    with open(journal.path) as f:
        return [json.loads(line) for line in f]


def test_runner_submits_once_through_real_routes(world):
    sub, _app, adapter, runner, journal = world
    record = sub.create()
    assert runner.run() == 0
    assert adapter.submit_calls == [record["id"]]  # second tick did nothing
    assert sub.campaigns() == 1
    (approval,) = sub.env.query("SELECT * FROM campaign_approvals")
    assert approval["approving_principal_id"] == sub.env.human()["id"]
    rows = lines(journal)
    assert [e["event"] for e in rows if e["event"] in {"submitted", "submit_refused"}] == [
        "submitted"
    ]
    ticks = [e for e in rows if e["event"] == "tick"]
    assert [t["started"] for t in ticks] == [1, 0]


def test_disabled_feature_is_journaled_as_refusal(world):
    sub, app, adapter, runner, journal = world
    sub.create()
    app.state.agent_submission = None
    assert runner.run() == 0
    refusals = [e for e in lines(journal) if e["event"] == "submit_refused"]
    assert refusals and all(e["reason"] == "feature_disabled" for e in refusals)
    assert sub.campaigns() == 0
