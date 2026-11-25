"""A write on a campaign the caller may not read answers exactly like a missing id (audit O-05).

Before this, the write routes found a campaign by organization only and refused a non-submitter
agent afterwards with 403, so 403-versus-404 told another agent that a campaign exists."""

from __future__ import annotations

import pytest
from campaign_fixture import Env
from fastapi.testclient import TestClient

from tessera_control.api import create_app

SCOPES = ["campaign:read", "campaign:cancel", "campaign:submit"]
MISSING = "camp_does_not_exist"
REASON = "synthetic reason"


def _token(name):
    return {"Authorization": f"Bearer synthetic-{name}-token-" + "x" * 30}


def _routes(campaign):
    rev = campaign["revision"]
    return {
        "cancel": ("", {"expected_revision": rev, "reason": REASON}),
        "approve": (
            "",
            {"expected_revision": rev, "expected_spec_digest": campaign["spec_digest"]},
        ),
        "budget-reduction": (
            "",
            {"expected_revision": rev, "budget_cap_cents": 1, "reason": REASON},
        ),
        "pause": ("", {"expected_revision": rev, "reason": REASON}),
        "resume": ("", {"expected_revision": rev, "reason": REASON}),
        "concurrency-limit": (
            "",
            {"expected_revision": rev, "max_concurrent_stages": 1, "reason": REASON},
        ),
        "stages/s1/cancel": ("", {"expected_revision": rev, "reason": REASON}),
        "placement-steering": (
            "",
            {
                "expected_revision": rev,
                "stage_key": "s1",
                "exclude": ["some-provider"],
                "reason": REASON,
            },
        ),
        "releases/rel_x/revoke": ("", {"expected_revision": rev, "reason": REASON}),
        "gate-decision": ("", {"expected_revision": rev, "decision": "go", "note": "synthetic"}),
    }


@pytest.fixture
def world(tmp_path):
    env = Env(tmp_path)
    campaign = env.submit()
    env.human()
    submitter = env.agent(SCOPES, "owner-agent")
    env.agent(SCOPES, "other-agent")
    env.execute(
        "UPDATE campaigns SET submitted_by_principal_id=? WHERE id=?",
        submitter["id"],
        campaign["id"],
    )
    return env, TestClient(create_app(env.state)), campaign


def _post(client, name, campaign_id, route, body, key):
    return client.post(
        f"/campaigns/{campaign_id}/{route}",
        json=body,
        headers={**_token(name), "X-Agent-Destination": "planner.local", "Idempotency-Key": key},
    )


@pytest.mark.parametrize("route", list(_routes({"revision": 1, "spec_digest": "d" * 64})))
def test_non_submitter_agent_cannot_tell_a_real_campaign_from_a_missing_one(world, route):
    _env, client, campaign = world
    suffix, body = _routes(campaign)[route]
    real = _post(client, "other-agent", campaign["id"], route, body, f"k-real-{route}")
    missing = _post(client, "other-agent", MISSING, route, body, f"k-miss-{route}")
    assert real.status_code == missing.status_code == 404
    assert real.json() == missing.json()


@pytest.mark.parametrize("route", list(_routes({"revision": 1, "spec_digest": "d" * 64})))
def test_submitter_and_human_are_not_hidden_from_the_campaign(world, route):
    _env, client, campaign = world
    _suffix, body = _routes(campaign)[route]
    for name in ("owner-agent", "human"):
        response = _post(client, name, campaign["id"], route, body, f"k-{name}-{route}")
        # Whatever the route decides (state conflict, forbidden for the role), the campaign
        # itself is found: 404 only for the releases route's unknown release id.
        if route.startswith("releases/"):
            continue
        assert response.status_code != 404, (name, route, response.json())


def test_submitter_agent_and_human_cancel_succeed(world):
    env, client, campaign = world
    body = {"expected_revision": campaign["revision"], "reason": REASON}
    assert _post(client, "owner-agent", campaign["id"], "cancel", body, "k-a").status_code == 200
    second = env.submit()
    body = {"expected_revision": second["revision"], "reason": REASON}
    assert _post(client, "human", second["id"], "cancel", body, "k-h").status_code == 200


def test_submitter_without_the_scope_still_gets_forbidden(world):
    env, client, campaign = world
    env.agent(["campaign:read"], "no-cancel")
    env.execute(
        "UPDATE campaigns SET submitted_by_principal_id=? WHERE id=?",
        env.agent(None, "no-cancel")["id"],
        campaign["id"],
    )
    body = {"expected_revision": campaign["revision"], "reason": REASON}
    assert _post(client, "no-cancel", campaign["id"], "cancel", body, "k-s").status_code == 403
