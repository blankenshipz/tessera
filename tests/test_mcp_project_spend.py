"""``tessera_project_spend``: the project's limits and campaign totals, read-only, scoped."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import httpx
from campaign_fixture import Env
from fastapi.testclient import TestClient
from test_mcp_campaign_client_path import _names as _campaign_names  # noqa: F401
from test_mcp_campaign_tools import _call, _server

from tessera_control.api import create_app
from tessera_mcp.client import ControlClient
from tessera_mcp.config import Config
from tessera_mcp.server import create_server

DEST = {"X-Agent-Destination": "planner.local"}


def _token(name):
    return {"Authorization": f"Bearer synthetic-{name}-token-" + "x" * 30}


def _world(tmp_path):
    env = Env(tmp_path)
    env.submit()
    env.agent(["budget:read"], "spender")
    env.agent(["campaign:read"], "reader")
    return env, TestClient(create_app(env.state))


def test_project_spend_reports_limits_and_unfinished_totals(tmp_path):
    env, client = _world(tmp_path)
    env.execute("UPDATE execution_project_limits SET held_cents=3, settled_cents=2")
    path = f"/projects/{env.project['id']}/agent-spend"
    body = client.get(path, headers={**_token("spender"), **DEST}).json()
    limits = body["limits"]
    assert limits["remaining_cents"] == limits["monetary_limit_cents"] - 5
    assert set(limits) == {
        "currency",
        "monetary_limit_cents",
        "max_concurrency",
        "held_cents",
        "settled_cents",
        "remaining_cents",
    }
    assert sum(row["campaigns"] for row in body["campaigns_by_status"]) == 1
    assert body["unfinished"]["campaigns"] == 1 and body["unfinished"]["cap_cents"] == 10
    assert "no tool raises them" in body["boundary"]
    # Read-only: the limits are exactly as the operator left them.
    row = env.query("SELECT held_cents, settled_cents FROM execution_project_limits")[0]
    assert (row[0], row[1]) == (3, 2)


def test_project_spend_needs_budget_scope_and_the_credentials_project(tmp_path):
    env, client = _world(tmp_path)
    path = f"/projects/{env.project['id']}/agent-spend"
    assert client.get(path, headers={**_token("reader"), **DEST}).status_code == 403
    assert client.get(path, headers=_token("spender")).status_code == 403  # no destination
    other = client.get("/projects/not-a-project/agent-spend", headers={**_token("spender"), **DEST})
    assert other.status_code in {403, 404}


def test_project_spend_tool_hits_its_route_read_only(tmp_path):
    seen: list = []
    server = _server(tmp_path, seen)
    _call(server, "tessera_project_spend", {})
    assert [(m, p) for m, p, _, _ in seen] == [("GET", "/projects/proj1/agent-spend")]
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert tools["tessera_project_spend"].annotations.readOnlyHint is True


def test_project_spend_is_registered_only_with_budget_read(tmp_path):
    from test_mcp_campaign_client_path import _server as scoped_server

    with_scope = scoped_server(tmp_path, [], ["budget:read"])
    without = scoped_server(tmp_path, [], ["campaign:read"])
    assert "tessera_project_spend" in {t.name for t in asyncio.run(with_scope.list_tools())}
    assert "tessera_project_spend" not in {t.name for t in asyncio.run(without.list_tools())}


def test_steering_coverage_doc_matches_the_registered_tools(tmp_path):
    """The table names only registered tools, except those it marks pending; a pending tool that
    is registered (its change landed) must lose the marker, so the table cannot go stale."""
    registered = {t.name for t in asyncio.run(_server(tmp_path, []).list_tools())}
    text = Path("docs/mcp-steering-gaps.md").read_text()
    named = set(re.findall(r"`(tessera_[a-z_]+)`", text)) - {"tessera_mcp"}
    pending = set(re.findall(r"`(tessera_[a-z_]+)` \(pending\)", text))
    # Operator-enabled tools are registered on their own servers, not the campaign one.
    reference = tmp_path / "approver-token"
    reference.write_text("synthetic-token")
    reference.chmod(0o600)
    client = ControlClient(
        Config("http://127.0.0.1:8765", reference, "proj1", "dest"),
        httpx.MockTransport(lambda request: httpx.Response(200, json={})),
    )
    approval = create_server(client, approval_tool=True)
    approval_names = {t.name for t in asyncio.run(approval.list_tools())}
    assert "tessera_approve_campaign" in approval_names
    optional = create_server(
        client, review_tools=True, review_decision_tool=True, agent_loop_marker=tmp_path / "kill"
    )
    optional_names = {t.name for t in asyncio.run(optional.list_tools())}
    assert {"tessera_submit_review_job", "tessera_pause_agent_loop"} <= optional_names
    everything = registered | approval_names | optional_names
    assert pending.isdisjoint(everything), "remove the (pending) marker: " + str(
        pending & everything
    )
    assert named - pending <= everything


def test_project_spend_without_limits_and_with_finished_campaigns(tmp_path):
    env, client = _world(tmp_path)
    env.execute("DELETE FROM execution_project_limits")
    done = env.submit()
    env.execute("UPDATE campaigns SET status='failed', settled_cents=4 WHERE id=?", done["id"])
    path = f"/projects/{env.project['id']}/agent-spend"
    body = client.get(path, headers={**_token("spender"), **DEST}).json()
    assert body["limits"] is None
    by = {row["status"]: row for row in body["campaigns_by_status"]}
    assert by["failed"]["settled_cents"] == 4 and by["failed"]["campaigns"] == 1
    assert sum(row["campaigns"] for row in body["campaigns_by_status"]) == 2
    # Finished campaigns are excluded from the unfinished totals.
    assert body["unfinished"]["campaigns"] == 1 and body["unfinished"]["settled_cents"] == 0


def test_steering_doc_links_to_decisions_that_exist():
    """A relative link to a decision record must resolve, so a doc cannot point at an ADR that
    is not in the tree (an unlanded ADR is named in plain text, marked pending)."""
    doc = Path("docs/mcp-steering-gaps.md")
    links = re.findall(r"\]\((decisions/[^)#]+)", doc.read_text())
    assert links
    assert [link for link in links if not (doc.parent / link).is_file()] == []


def test_project_spend_sums_every_campaign_status_without_nulls(tmp_path):
    """One campaign per status: the money columns are NOT NULL, so every group sums to a number,
    and only the non-terminal statuses count as unfinished."""
    from tessera_control.campaign_models import CAMPAIGN_STATUSES, TERMINAL_CAMPAIGN

    env, client = _world(tmp_path)
    first = env.query("SELECT id FROM campaigns")[0]["id"]
    ids = [first]
    for number in range(1, len(CAMPAIGN_STATUSES)):
        ids.append(env.submit(spec=env.spec(name=f"status-{number}"))["id"])
    for number, (campaign_id, status) in enumerate(
        zip(ids, CAMPAIGN_STATUSES, strict=True), start=1
    ):
        env.execute(
            "UPDATE campaigns SET status=?, held_cents=?, settled_cents=? WHERE id=?",
            status,
            number,
            number * 10,
            campaign_id,
        )
    path = f"/projects/{env.project['id']}/agent-spend"
    body = client.get(path, headers={**_token("spender"), **DEST}).json()
    by = {row["status"]: row for row in body["campaigns_by_status"]}
    assert set(by) == set(CAMPAIGN_STATUSES)
    for row in by.values():
        assert all(
            isinstance(row[key], int) for key in ("cap_cents", "held_cents", "settled_cents")
        )
    open_rows = [row for status, row in by.items() if status not in TERMINAL_CAMPAIGN]
    assert body["unfinished"]["campaigns"] == len(open_rows)
    assert body["unfinished"]["settled_cents"] == sum(row["settled_cents"] for row in open_rows)


def test_project_spend_refuses_another_real_project_with_the_same_org(tmp_path):
    """A budget:read credential is bound to its own project: a second, real project in the same
    organization is refused, and the response carries none of that project's figures."""
    env, client = _world(tmp_path)
    org = env.query("SELECT org_id FROM projects WHERE id=?", env.project["id"])[0][0]
    env.execute(
        "INSERT INTO projects (id, org_id, name, created_at) VALUES (?,?,?,?)",
        "project_second",
        org,
        "Second",
        "2026-01-01T00:00:00Z",
    )
    response = client.get(
        "/projects/project_second/agent-spend", headers={**_token("spender"), **DEST}
    )
    assert response.status_code in {403, 404}
    assert "campaigns_by_status" not in response.text
