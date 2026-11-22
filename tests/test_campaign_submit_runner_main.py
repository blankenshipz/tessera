"""The console entry point runs one scheduled pass end to end (ADR 0093)."""

import json
import os

import pytest

from tessera_agent import campaign_submit_runner as runner


class _FakeControl:
    """Stands in for ControlClient: the real route shapes, no network."""

    instances: list = []

    def __init__(self, config):
        self.config = config
        self.submitted: list = []
        _FakeControl.instances.append(self)

    async def list_pending_submissions(self, project_id, offset, limit):
        assert project_id == "p1"
        return {"submission_approvals": [{"id": "approval_1", "revision": 1}]}

    async def submit_cleared_campaign(self, approval_id, expected_revision, idempotency_key):
        self.submitted.append((approval_id, expected_revision, idempotency_key))
        return {"campaign_id": "campaign_1", "replayed": False}


@pytest.fixture
def env(tmp_path, monkeypatch):
    token = tmp_path / "agent-token"
    token.write_text("opaque-token-value-for-tests")
    os.chmod(token, 0o600)
    monkeypatch.setenv("TESSERA_CONTROL_URL", "http://localhost:9")
    monkeypatch.setenv("TESSERA_AGENT_TOKEN_FILE", str(token))
    monkeypatch.setenv("TESSERA_PROJECT_ID", "p1")
    monkeypatch.setenv("TESSERA_AGENT_DESTINATION", "local-agent")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("tessera_mcp.client.ControlClient", _FakeControl)
    _FakeControl.instances.clear()
    return tmp_path


def _journal(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_main_once_submits_a_pending_record_and_journals_it(env):
    journal, marker = env / "journal.jsonl", env / "kill"
    code = runner.main(["--journal", str(journal), "--kill-switch", str(marker), "--once"])
    assert code == 0
    (client,) = _FakeControl.instances
    assert [call[:2] for call in client.submitted] == [("approval_1", 1)]
    assert client.submitted[0][2] == "agent_loop:approval_1:1"
    events = _journal(journal)
    assert any(e["event"] == "runner_started" for e in events)
    assert any(e["event"] == "runner_stopped" for e in events)
    assert "opaque-token-value-for-tests" not in journal.read_text()
    assert oct(journal.stat().st_mode & 0o777) == "0o600"


def test_main_with_the_kill_switch_engaged_calls_nothing(env):
    journal, marker = env / "journal.jsonl", env / "kill"
    marker.write_text("")
    code = runner.main(["--journal", str(journal), "--kill-switch", str(marker), "--once"])
    assert code == 0
    (client,) = _FakeControl.instances
    assert client.submitted == []
    assert [e["event"] for e in _journal(journal)].count("kill_switch_engaged") == 1
