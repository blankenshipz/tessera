"""``project_spend`` privacy: an agent never receives project-wide held/settled aggregates.

The project limit row sums every agent's spend, so polling it would reveal another agent's
activity. An agent gets the operator's configuration, its own totals and a headroom boolean.
"""

from __future__ import annotations

import json

import pytest
from campaign_fixture import Env
from fastapi.testclient import TestClient

from tessera_control.api import create_app

DEST = {"X-Agent-Destination": "planner.local"}
ACTIONS = ["campaign:submit", "campaign:read", "budget:read"]
AGENT_KEYS = {
    "currency",
    "monetary_limit_cents",
    "max_concurrency",
    "own_held_cents",
    "own_settled_cents",
    "ceiling_has_headroom",
}
# Distinctive amounts so a leak of another agent's figure cannot match by coincidence.
A_HELD, A_SETTLED, B_HELD, B_SETTLED = 3, 4, 1357, 2468
LIMIT = 100000


def _token(name):
    return {"Authorization": f"Bearer synthetic-{name}-token-" + "x" * 30}


def _spend(client, env, name):
    headers = _token(name) if name == "human" else {**_token(name), **DEST}
    response = client.get(f"/projects/{env.project['id']}/agent-spend", headers=headers)
    assert response.status_code == 200
    return response.json()


@pytest.fixture
def world(tmp_path):
    env = Env(tmp_path)
    alice = env.agent(ACTIONS, "alice")
    bob = env.agent(ACTIONS, "bob")
    env.human("human")
    mine = env.approve(env.submit(alice))
    theirs = env.approve(env.submit(bob))
    env.execute(
        "UPDATE campaigns SET held_cents=?, settled_cents=? WHERE id=?",
        A_HELD,
        A_SETTLED,
        mine["id"],
    )
    env.execute(
        "UPDATE campaigns SET held_cents=?, settled_cents=? WHERE id=?",
        B_HELD,
        B_SETTLED,
        theirs["id"],
    )
    env.execute(
        "UPDATE execution_project_limits SET monetary_limit_cents=?, held_cents=?, settled_cents=?",
        LIMIT,
        A_HELD + B_HELD,
        A_SETTLED + B_SETTLED,
    )
    return env, TestClient(create_app(env.state))


def test_agent_limits_carry_configuration_and_own_totals_only(world):
    env, client = world
    limits = _spend(client, env, "alice")["limits"]
    assert set(limits) == AGENT_KEYS
    assert limits["own_held_cents"] == A_HELD and limits["own_settled_cents"] == A_SETTLED
    row = env.query(
        "SELECT currency, monetary_limit_cents, max_concurrency FROM execution_project_limits"
    )[0]
    assert (limits["currency"], limits["monetary_limit_cents"], limits["max_concurrency"]) == tuple(
        row
    )
    assert limits["ceiling_has_headroom"] is True
    for gone in ("held_cents", "settled_cents", "remaining_cents"):
        assert gone not in limits


def test_agent_response_reveals_nothing_of_the_other_agents_spend(world):
    env, client = world
    body = _spend(client, env, "alice")
    text = json.dumps(body)
    for amount in (B_HELD, B_SETTLED, B_HELD + A_HELD, B_SETTLED + A_SETTLED):
        assert f": {amount}," not in text and f": {amount}}}" not in text
    assert body["unfinished"]["held_cents"] == A_HELD
    assert body["unfinished"]["settled_cents"] == A_SETTLED


def test_headroom_flips_when_the_other_agent_exhausts_the_ceiling_without_amounts(world):
    env, client = world
    before = _spend(client, env, "alice")["limits"]
    limit = before["monetary_limit_cents"]
    env.execute("UPDATE execution_project_limits SET held_cents=0, settled_cents=?", limit)
    after = _spend(client, env, "alice")["limits"]
    assert before["ceiling_has_headroom"] is True
    assert after["ceiling_has_headroom"] is False
    assert set(after) == AGENT_KEYS
    assert after["own_held_cents"] == A_HELD and after["own_settled_cents"] == A_SETTLED
    assert str(limit) in json.dumps(after)  # only as the operator's own configured limit
    assert after["monetary_limit_cents"] == limit


def test_actor_reading_every_campaign_keeps_the_full_row(world):
    env, client = world
    limits = _spend(client, env, "human")["limits"]
    assert limits["held_cents"] == A_HELD + B_HELD
    assert limits["settled_cents"] == A_SETTLED + B_SETTLED
    assert limits["remaining_cents"] == (
        limits["monetary_limit_cents"] - A_HELD - B_HELD - A_SETTLED - B_SETTLED
    )
    assert "ceiling_has_headroom" not in limits and "own_held_cents" not in limits
