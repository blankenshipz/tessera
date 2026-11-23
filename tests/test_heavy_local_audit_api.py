"""Heavy-local audit read: control route and MCP tool, scope-gated, tenant-safe, read-only."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from campaign_fixture import Env
from fastapi.testclient import TestClient
from mcp_required import require_mcp

from tessera_control import campaign_heavy_guard as guard
from tessera_control.api import create_app
from tessera_control.store import _utc_string, canonical_digest, utc_now

require_mcp()

from tessera_mcp.client import ControlClient  # noqa: E402
from tessera_mcp.config import Config  # noqa: E402

DESTINATION = "planner.local"
EXPLAINED = sorted(guard.EXPLAINED_REASONS)[0]


def ago(seconds: int) -> str:
    return _utc_string(datetime.now(UTC) - timedelta(seconds=seconds))


class World:
    def __init__(self, tmp_path):
        self.env = Env(tmp_path)
        self.db = self.env.state / "tessera.sqlite3"
        self.org = self.env.owner["org_id"]
        self.project = self.env.project["id"]
        self.path = f"/projects/{self.project}/campaign-stuck/heavy-local-audit"
        self.client = TestClient(create_app(self.env.state, campaign_runner_interval=None))
        self.campaign = self.env.submit()["id"]
        self.stage = self.sql("SELECT stage_key FROM campaign_stages")[0][0]
        self.sql("UPDATE campaigns SET status='running', updated_at=?", ago(5))
        self.issued: dict = {}

    def sql(self, statement, *args):
        with sqlite3.connect(self.db) as db:
            return db.execute(statement, args).fetchall()

    def seed(self):
        detail = [{"provider_id": "salad", "reason": "over_cap"}]
        self.sql(
            """INSERT INTO events (org_id, project_id, task_id, event_type, resource_revision,
            actor_principal_id, created_at, payload_json) VALUES (?,?,NULL,?,1,?,?,?)""",
            self.org, self.project, "campaign.heavy_stage_local_fallback", self.env.owner["id"],
            ago(60),
            json.dumps({"campaign_id": self.campaign, "stage_key": self.stage, "attempt": 1,
                        "provider_id": "garage", "signals": ["heavy_tag"],
                        "reasons": [EXPLAINED], "detail": detail, "mode": "warn"}),
        )  # fmt: skip
        self.sql(
            """INSERT INTO campaign_stage_attempts (id, campaign_id, stage_key, attempt, status,
            hold_cents, revision, created_at, updated_at) VALUES (?,?,?,?,?,0,1,?,?)""",
            f"{self.campaign}_a1", self.campaign, self.stage, 1, "running", ago(5), ago(5),
        )  # fmt: skip
        return detail

    def credential(self, name, **body):
        if name not in self.issued:
            body = {
                "audience": "control",
                "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
                **body,
            }
            token = f"synthetic-{name}-token-" + "x" * 30
            self.env.plain.issue_credential(
                self.env.owner, f"cred-{name}", canonical_digest(body), body, token
            )
            self.issued[name] = (token, {"Authorization": f"Bearer {token}",
                                         "X-Agent-Destination": DESTINATION})  # fmt: skip
        return self.issued[name]

    def human(self):
        return self.credential("human", kind="human_session")[1]

    def agent(self, actions):
        return self.credential(
            "agent-" + "-".join(a.replace(":", "_") for a in actions),
            kind="agent",
            project_id=self.project,
            external_agent={"actions": actions, "destination": DESTINATION},
        )

    def get(self, headers=None, project=None):
        path = self.path if project is None else self.path.replace(self.project, project)
        return self.client.get(path, headers=headers or self.human())

    def counts(self):
        return [
            self.sql(f"SELECT COUNT(*) FROM {t}")[0][0]
            for t in ("events", "campaigns", "campaign_stage_attempts", "campaign_stuck_findings")
        ]


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_a_heavy_local_attempt_is_listed_with_reasons_and_detail(world):
    detail = world.seed()
    body = world.get().json()
    assert body["count"] == 1 and body["project_id"] == world.project
    (item,) = body["items"]
    assert item["campaign_id"] == world.campaign and item["provider_id"] == "garage"
    assert item["reasons"] == [EXPLAINED] and item["detail"] == detail
    assert item["explained"] is True and item["attempt_id"] == f"{world.campaign}_a1"


def test_nothing_running_lists_nothing(world):
    assert world.get().json()["items"] == [] and world.get().json()["count"] == 0


def test_read_only_scope_agent_may_read_and_others_are_refused(world):
    world.seed()
    assert world.get(world.agent(["campaign:read"])[1]).json()["count"] == 1
    assert world.get(world.agent(["context:read"])[1]).status_code == 403
    assert world.get(world.agent(["campaign:cancel"])[1]).status_code == 403
    assert world.client.get(world.path).status_code in (401, 403)


def test_the_read_writes_nothing(world):
    world.seed()
    before = world.counts()
    world.get()
    world.get(world.agent(["campaign:read"])[1])
    assert world.counts() == before


def test_a_foreign_or_unknown_project_gets_the_same_404_as_the_stuck_read(world):
    world.seed()
    other = "11111111-1111-1111-1111-111111111111"
    unknown = world.get(project=other)
    stuck_unknown = world.client.get(f"/projects/{other}/campaign-stuck", headers=world.human())
    assert unknown.status_code == stuck_unknown.status_code == 404
    assert unknown.content == stuck_unknown.content


def test_another_organization_gets_404(world):
    from tessera_control.store import Store, _token_hash

    token = "other-org-owner-" + "z" * 40
    with sqlite3.connect(world.db) as db:
        db.execute("INSERT INTO organizations VALUES (?,?,?)", ("org_b", "B", utc_now()))
        db.execute(
            "INSERT INTO principals VALUES (?,?,?,?,?,?)",
            ("owner_b", "org_b", "human", _token_hash(token), None, utc_now()),
        )
        db.execute(
            "INSERT INTO credentials VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("cred_b", "owner_b", "local_owner", "control", None, None, None, None,
             utc_now(), None),
        )  # fmt: skip
    owner_b = Store(world.env.state).authenticate(token)
    body = {
        "audience": "control",
        "kind": "human_session",
        "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
    }
    session = "other-org-session-" + "y" * 40
    world.env.plain.issue_credential(owner_b, "cred-b", canonical_digest(body), body, session)
    theirs = {"Authorization": f"Bearer {session}", "X-Agent-Destination": DESTINATION}
    world.seed()
    assert world.get(theirs).status_code == 404


def test_the_mcp_tool_returns_the_same_items(world, tmp_path):
    from mcp.server.fastmcp import FastMCP

    from tessera_mcp.server import register_campaign_tools

    world.seed()
    token, headers = world.agent(["campaign:read"])
    reference = tmp_path / "token"
    reference.write_text(token)
    reference.chmod(0o600)

    def handler(request: httpx.Request) -> httpx.Response:
        response = world.client.get(request.url.raw_path.decode(), headers=headers)
        return httpx.Response(response.status_code, json=response.json())

    client = ControlClient(
        Config("http://127.0.0.1:8765", reference, world.project, DESTINATION),
        httpx.MockTransport(handler),
    )
    server = FastMCP("t", log_level="CRITICAL")
    register_campaign_tools(server, client, frozenset({"campaign:read"}))
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert tools["tessera_heavy_local_audit"].annotations.readOnlyHint is True
    result = asyncio.run(server.call_tool("tessera_heavy_local_audit", {}))
    result = result[1] if isinstance(result, tuple) else result
    assert result == world.get().json()

    narrow = FastMCP("t", log_level="CRITICAL")
    register_campaign_tools(narrow, client, frozenset({"snapshot:read"}))
    assert "tessera_heavy_local_audit" not in {t.name for t in asyncio.run(narrow.list_tools())}
