import pytest

from tessera_agent.campaign_submit_action import (
    CampaignSubmitAction,
    SubmitPolicy,
    SubmitRefused,
)
from tessera_agent.standing_loop import ApiKeyPresent, KillSwitch


class FakeClient:
    def __init__(self, pending, refuse=()):
        self.pending, self.refuse, self.calls = pending, set(refuse), []

    def list_pending(self, project_id):
        return {"submission_approvals": self.pending}

    def submit(self, approval_id, expected_revision, idempotency_key):
        self.calls.append((approval_id, expected_revision, idempotency_key))
        if approval_id in self.refuse:
            raise SubmitRefused("project_spend_headroom")
        return {"campaign_id": "camp-" + approval_id}


def _action(tmp_path, client, per_tick=1, engaged=False, environ=None):
    marker = tmp_path / "kill"
    if engaged:
        marker.write_text("")
    events = []
    action = CampaignSubmitAction(
        client,
        KillSwitch(marker),
        events.append,
        SubmitPolicy("proj", per_tick),
        environ=environ or {},
    )
    return action, events


PENDING = [{"id": "a1", "revision": 1}, {"id": "a2", "revision": 3}]


def test_submits_one_per_tick_with_stable_key(tmp_path):
    client = FakeClient(PENDING)
    action, events = _action(tmp_path, client)
    action.tick()
    assert client.calls == [("a1", 1, "agent_loop:a1:1")]
    assert events == [
        {"event": "submitted", "approval_id": "a1", "revision": 1, "campaign_id": "camp-a1"}
    ]


def test_cap_raised_submits_more(tmp_path):
    client = FakeClient(PENDING)
    action, _ = _action(tmp_path, client, per_tick=2)
    action.tick()
    assert [c[2] for c in client.calls] == ["agent_loop:a1:1", "agent_loop:a2:3"]


def test_refusal_recorded_not_retried(tmp_path):
    client = FakeClient(PENDING, refuse={"a1"})
    action, events = _action(tmp_path, client, per_tick=2)
    action.tick()
    assert [c[0] for c in client.calls] == ["a1", "a2"]
    assert events[0] == {
        "event": "submit_refused",
        "approval_id": "a1",
        "revision": 1,
        "reason": "project_spend_headroom",
    }


def test_kill_switch_blocks_all_calls(tmp_path):
    client = FakeClient(PENDING)
    action, events = _action(tmp_path, client, engaged=True)
    action.tick()
    assert client.calls == []
    assert events == [{"event": "submit_skipped", "reason": "kill_switch"}]


def test_kill_switch_checked_before_each_call(tmp_path):
    client = FakeClient(PENDING)
    action, events = _action(tmp_path, client, per_tick=2)
    original = client.submit

    def submit_then_engage(*args):
        result = original(*args)
        (tmp_path / "kill").write_text("")
        return result

    client.submit = submit_then_engage
    action.tick()
    assert len(client.calls) == 1
    assert events[-1]["reason"] == "kill_switch"


def test_refuses_with_api_key_in_environment(tmp_path):
    with pytest.raises(ApiKeyPresent):
        _action(tmp_path, FakeClient([]), environ={"ANTHROPIC_API_KEY": "x"})


def test_no_approve_create_or_widen_surface(tmp_path):
    action, _ = _action(tmp_path, FakeClient([]))
    public = {n for n in dir(action) if not n.startswith("_")}
    assert public == {"tick"}


def test_malformed_list_is_recorded_and_submits_nothing(tmp_path):
    for bad in ({"items": PENDING}, PENDING, None, {"submission_approvals": "x"}):
        client = FakeClient(PENDING)
        client.list_pending = lambda project_id, bad=bad: bad
        action, events = _action(tmp_path, client)
        action.tick()
        assert client.calls == [] and events == [{"event": "list_malformed"}]


def test_refusal_does_not_starve_later_records(tmp_path):
    client = FakeClient(PENDING, refuse={"a1"})
    action, events = _action(tmp_path, client, per_tick=1)
    action.tick()
    assert [c[0] for c in client.calls] == ["a1", "a2"]
    assert [e["event"] for e in events] == ["submit_refused", "submitted"]


def test_attempt_cap_and_no_retry_within_tick(tmp_path):
    many = [{"id": f"r{i}", "revision": 1} for i in range(8)]
    client = FakeClient(many, refuse={r["id"] for r in many})
    events = []
    action = CampaignSubmitAction(
        client, KillSwitch(tmp_path / "kill"), events.append, SubmitPolicy("p", 1, 3), environ={}
    )
    action.tick()
    assert [c[0] for c in client.calls] == ["r0", "r1", "r2"]
    action.tick()  # refused ones go last: next fresh records first
    assert [c[0] for c in client.calls[3:]] == ["r3", "r4", "r5"]
