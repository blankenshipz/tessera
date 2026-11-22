"""CampaignSubmitAction against the real control routes (synthetic data only)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_campaign_submissions import Sub

from tessera_agent.campaign_submit_action import (
    CampaignSubmitAction,
    SubmitPolicy,
    SubmitRefused,
)
from tessera_agent.standing_loop import KillSwitch
from tessera_control import program_budget
from tessera_control.api import create_app

AGENT = {
    "Authorization": "Bearer synthetic-agent-token-" + "x" * 30,
    "X-Agent-Destination": "planner.local",
}


class HttpAdapter:
    """list_pending/submit over the real routes via the test client."""

    def __init__(self, client, project_id):
        self.client, self.project_id = client, project_id
        self.submit_calls = []
        self.list_responses = []

    def list_pending(self, project_id):
        response = self.client.get(
            f"/projects/{project_id}/submission-approvals?status=pending&offset=0&limit=100",
            headers=AGENT,
        )
        assert response.status_code == 200
        self.list_responses.append(response.json())
        return response.json()

    def submit(self, approval_id, expected_revision, idempotency_key):
        self.submit_calls.append(approval_id)
        response = self.client.post(
            f"/submission-approvals/{approval_id}/submit",
            json={"expected_revision": expected_revision},
            headers={**AGENT, "Idempotency-Key": idempotency_key},
        )
        if response.status_code == 409:
            raise SubmitRefused(response.json()["reason"])
        assert response.status_code in {200, 201}, response.text
        return response.json()


@pytest.fixture
def world(tmp_path):
    program_budget.reset()
    sub = Sub(tmp_path)
    sub.agent()  # issues the synthetic agent token
    app = create_app(sub.env.state)
    app.state.agent_submission = sub.on
    adapter = HttpAdapter(TestClient(app), sub.env.project["id"])
    events = []
    action = CampaignSubmitAction(
        adapter, KillSwitch(sub.marker), events.append, SubmitPolicy(adapter.project_id), environ={}
    )
    yield sub, app, adapter, action, events
    program_budget.reset()


def test_submits_once_with_the_human_as_approver(world):
    sub, _app, adapter, action, events = world
    record = sub.create()
    action.tick()
    assert adapter.submit_calls == [record["id"]]
    assert set(adapter.list_responses[0]) >= {"submission_approvals"}
    assert [e["event"] for e in events] == ["submitted"]
    assert sub.campaigns() == 1
    (approval,) = sub.env.query("SELECT * FROM campaign_approvals")
    assert approval["approving_principal_id"] == sub.env.human()["id"]
    action.tick()
    assert adapter.submit_calls == [record["id"]] and sub.campaigns() == 1


def test_kill_switch_stops_before_any_submit_route(world):
    sub, _app, adapter, action, events = world
    sub.create()
    sub.marker.write_text("")
    action.tick()
    assert adapter.submit_calls == [] and sub.campaigns() == 0
    assert events == [{"event": "submit_skipped", "reason": "kill_switch"}]


def test_disabled_feature_reason_propagates(world):
    sub, app, adapter, action, events = world
    record = sub.create()
    app.state.agent_submission = None
    action.tick()
    assert events == [
        {
            "event": "submit_refused",
            "approval_id": record["id"],
            "revision": 1,
            "reason": "feature_disabled",
        }
    ]
    assert sub.campaigns() == 0


@pytest.mark.parametrize("status", [409, 403, 500])
def test_mcp_adapter_maps_only_409_to_a_stable_refusal(tmp_path, status):
    import httpx

    from tessera_mcp.client import ControlClient, SubmissionActionClient
    from tessera_mcp.config import Config

    token = tmp_path / "token"
    token.write_text("synthetic-agent-token")
    token.chmod(0o600)
    body = {"detail": "d", "reason": "kill_switch_engaged", "approval_id": "a", "permanent": False}
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json=body))
    adapter = SubmissionActionClient(
        ControlClient(Config("http://127.0.0.1:8765", token, "p", "dest"), transport)
    )
    if status == 409:
        with pytest.raises(SubmitRefused) as caught:
            adapter.submit("a", 1, "k")
        assert caught.value.reason == "kill_switch_engaged"
    else:
        with pytest.raises(Exception) as caught:
            adapter.submit("a", 1, "k")
        assert not isinstance(caught.value, SubmitRefused)
