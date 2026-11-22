"""tessera_approve_campaign: a thin client of the control API's approval (no bypass).

The mcp extra is required: a missing one fails this module at import instead of skipping it.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from campaign_fixture import Env
from mcp.server.fastmcp.exceptions import ToolError
from test_campaign_gates import write_budget

from tessera_control import program_budget
from tessera_control.api import create_app
from tessera_mcp.client import ControlClient
from tessera_mcp.config import Config
from tessera_mcp.server import CAMPAIGN_ACTIONS, CAMPAIGN_TOOL_SCOPES, create_server

DIGEST = "d" * 64
ACTIONS = sorted(CAMPAIGN_ACTIONS)


def _server(tmp_path, handler, *, approval_tool=True, name="tok"):
    reference = tmp_path / name
    reference.write_text("synthetic-token")
    reference.chmod(0o600)
    client = ControlClient(
        Config("http://127.0.0.1:8765", reference, "proj1", "dest"), httpx.MockTransport(handler)
    )
    return create_server(client, campaign_tools=True, approval_tool=approval_tool)


def _call(server, name, arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    return result[1] if isinstance(result, tuple) else result


def _names(server):
    return {t.name for t in asyncio.run(server.list_tools())}


def test_the_tool_is_off_unless_the_operator_enables_it(tmp_path):
    def handler(request):
        return httpx.Response(200, json={})

    assert "tessera_approve_campaign" not in _names(_server(tmp_path, handler, approval_tool=False))
    assert "tessera_approve_campaign" in _names(_server(tmp_path, handler))
    assert "tessera_approve_campaign" not in CAMPAIGN_TOOL_SCOPES  # no enrollable scope grants it
    assert not [n for n in CAMPAIGN_ACTIONS if "approve" in n]


def test_the_approval_tool_registers_on_its_own_without_submit_or_cancel(tmp_path):
    def handler(request):
        return httpx.Response(200, json={})

    reference = tmp_path / "solo"
    reference.write_text("synthetic-token")
    reference.chmod(0o600)
    client = ControlClient(
        Config("http://127.0.0.1:8765", reference, "proj1", "dest"), httpx.MockTransport(handler)
    )
    names = _names(create_server(client, approval_tool=True))
    assert "tessera_approve_campaign" in names
    assert not {n for n in names if "campaign" in n} - {"tessera_approve_campaign"}


def test_the_command_line_flag_alone_makes_an_approve_only_server(tmp_path, monkeypatch):
    from tessera_mcp import server as module

    reference = tmp_path / "cli.token"
    reference.write_text("synthetic-token")
    reference.chmod(0o600)
    for name, value in {
        "TESSERA_CONTROL_URL": "http://127.0.0.1:8765",
        "TESSERA_AGENT_TOKEN_FILE": str(reference),
        "TESSERA_PROJECT_ID": "proj1",
        "TESSERA_AGENT_DESTINATION": "dest",
    }.items():
        monkeypatch.setenv(name, value)
    built: list = []

    class Stub:
        def run(self, transport):
            return None

    def fake_create(client, **kwargs):
        built.append(kwargs)
        return Stub()

    monkeypatch.setattr(module, "create_server", fake_create)
    monkeypatch.setattr(module, "_has_provider_trust_scope", lambda client: False)
    monkeypatch.setattr(module, "_live_actions", lambda client: pytest.fail("no live scope read"))
    assert module.main(["--approval-tool"]) == 0
    assert built == [{"campaign_tools": False, "approval_tool": True}]
    built.clear()
    assert module.main(["--approval-tool", "--campaign-tools"]) == 0
    assert built == [{"campaign_tools": True, "approval_tool": True}]


def test_it_reads_the_revision_then_posts_with_a_fresh_key_per_call(tmp_path):
    seen: list = []

    def handler(request):
        seen.append((request.method, request.url.path, request.headers.get("idempotency-key")))
        if request.method == "GET":
            return httpx.Response(200, json={"revision": 7, "spec_digest": DIGEST})
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "running"})

    server = _server(tmp_path, handler)
    args = {"campaign_id": "campaign_1", "expected_spec_digest": DIGEST}
    assert _call(server, "tessera_approve_campaign", args) == {"status": "running"}
    _call(server, "tessera_approve_campaign", args)
    _call(server, "tessera_approve_campaign", {**args, "idempotency_key": "mine-1"})
    posts = [row for row in seen if isinstance(row, tuple) and row[0] == "POST"]
    keys = [row[2] for row in posts]
    assert keys[0].startswith("approve-") and keys[1].startswith("approve-")
    assert keys[0] != keys[1] and keys[2] == "mine-1"
    assert {row[1] for row in posts} == {"/campaigns/campaign_1/approve"}
    assert {"expected_revision": 7, "expected_spec_digest": DIGEST} in seen
    assert seen[0] == ("GET", "/campaigns/campaign_1", None)  # the revision is read first


def test_a_digest_the_caller_did_not_name_sends_nothing(tmp_path):
    seen: list = []

    def handler(request):
        seen.append(request.method)
        return httpx.Response(200, json={"revision": 7, "spec_digest": "e" * 64})

    server = _server(tmp_path, handler)
    with pytest.raises(ToolError, match="nothing was sent"):
        _call(
            server, "tessera_approve_campaign", {"campaign_id": "c", "expected_spec_digest": DIGEST}
        )
    assert seen == ["GET"]


# -- against the real control app: the platform decides, the tool bypasses nothing ---------------


class World:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.env = Env(tmp_path)
        self.agent = self.env.agent(ACTIONS, "steerer")
        self.human = self.env.human("approver")
        self.app = create_app(self.env.state)
        self.campaign = self.env.submit(self.agent)

    def server(self, who):  # "steerer" (agent credential) or "approver" (a person's session)
        reference = self.tmp / f"{who}.token"
        reference.write_text(f"synthetic-{who}-token-" + "x" * 30)
        reference.chmod(0o600)
        client = ControlClient(
            Config("http://127.0.0.1:8765", reference, self.env.project["id"], "planner.local"),
            httpx.ASGITransport(app=self.app),
        )
        return create_server(client, campaign_tools=True, approval_tool=True)

    def approve(self, who, **extra):
        return _call(
            self.server(who),
            "tessera_approve_campaign",
            {
                "campaign_id": self.campaign["id"],
                "expected_spec_digest": self.campaign["spec_digest"],
                **extra,
            },
        )

    def status(self):
        row = self.env.query("SELECT status FROM campaigns WHERE id=?", self.campaign["id"])[0]
        return row[0]

    def approvals(self):
        return self.env.query(
            "SELECT * FROM campaign_approvals WHERE campaign_id=?", self.campaign["id"]
        )


@pytest.fixture(autouse=True)
def _program_budget_reset():
    program_budget.reset()
    yield
    program_budget.reset()


def test_a_persons_credential_approves_and_the_approval_record_comes_back(tmp_path):
    world = World(tmp_path)
    assert world.status() == "submitted"
    record = world.approve("approver")
    assert record["status"] in {"approved", "running"}
    assert record["approval"]["decision_class"] == "human_session"
    assert record["approval"]["spec_digest"] == world.campaign["spec_digest"]
    assert len(world.approvals()) == 1 and world.status() == record["status"]
    assert world.env.events("campaign.approved")


def test_an_agent_credential_cannot_approve_even_through_the_tool(tmp_path):
    world = World(tmp_path)
    with pytest.raises(ToolError, match="HTTP 403"):
        world.approve("steerer")
    assert world.status() == "submitted" and world.approvals() == []
    assert not world.env.events("campaign.approved")


def test_the_same_client_key_replays_the_same_approval_once(tmp_path):
    world = World(tmp_path)
    revision = world.env.query("SELECT revision FROM campaigns WHERE id=?", world.campaign["id"])[
        0
    ][0]
    args = {"idempotency_key": "approve-mine", "expected_revision": revision}
    first = world.approve("approver", **args)
    second = world.approve("approver", **args)  # the answer was "lost": same key, same request
    assert second == first
    assert len(world.approvals()) == 1 and len(world.env.events("campaign.approved")) == 1


def test_a_second_call_with_a_fresh_key_is_refused_not_double_approved(tmp_path):
    world = World(tmp_path)
    world.approve("approver")
    with pytest.raises(ToolError, match="HTTP 409"):
        world.approve("approver")
    assert len(world.approvals()) == 1


def test_a_key_used_for_another_campaign_is_refused(tmp_path):
    world = World(tmp_path)
    other = world.env.submit(world.agent)
    world.approve("approver", idempotency_key="shared-key")
    server = world.server("approver")
    with pytest.raises(ToolError, match="HTTP 409"):
        _call(
            server,
            "tessera_approve_campaign",
            {
                "campaign_id": other["id"],
                "expected_spec_digest": other["spec_digest"],
                "idempotency_key": "shared-key",
            },
        )
    assert world.env.query("SELECT status FROM campaigns WHERE id=?", other["id"])[0][0] == (
        "submitted"
    )


def test_the_program_cap_still_applies_and_a_refusal_writes_nothing(tmp_path):
    world = World(tmp_path)
    cap = world.campaign["budget_cap_cents"] - 1
    program_budget.configure(
        program_budget.load_program_budget_file(
            write_budget(tmp_path, [{"id": "main", "cap_cents": cap}])
        )
    )
    with pytest.raises(ToolError, match="HTTP 409"):
        world.approve("approver")
    assert world.status() == "submitted" and world.approvals() == []
    assert not world.env.events("campaign.approved")
    program_budget.reset()
    world.approve("approver")  # the same call passes once the cap fits: nothing was bypassed
    assert len(world.approvals()) == 1


def test_a_stale_or_wrong_digest_never_approves(tmp_path):
    world = World(tmp_path)
    server = world.server("approver")
    with pytest.raises(ToolError, match="nothing was sent"):
        _call(
            server,
            "tessera_approve_campaign",
            {"campaign_id": world.campaign["id"], "expected_spec_digest": "0" * 64},
        )
    assert world.status() == "submitted"
