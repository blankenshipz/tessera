"""Trusted-guard account-route evidence per enrolled destination (synthetic fixtures)."""

import copy

import pytest

from tessera_agent import model_destinations as md
from tessera_mcp.harness_observer import _account_route_admitted

OPENAI = md.DESTINATIONS["openai"]
ANTHROPIC = md.DESTINATIONS["anthropic"]


def account_for(dest, **changes):
    value = {
        "connection_id": "public-account-id",
        "method": "oauth",
        "provider_id": dest.provider_id,
        "model_id": "model",
        "route": dest.billing_route,
        "policy": dest.guard_policy(),
    }
    value.update(changes)
    return value


def test_openai_evidence_is_unchanged():
    assert OPENAI.billing_route == "chatgpt-included"
    assert OPENAI.guard_policy() == {
        "allowed_auth_methods": ["oauth"],
        "billing_routes": ["chatgpt-included"],
        "on_unknown": "deny",
        "on_exhausted": "stop",
    }
    assert _account_route_admitted(account_for(OPENAI), OPENAI)


def test_anthropic_route_unreachable_on_opencode_harness():
    # The guard's account-route check is only reached for destinations that
    # require_runtime admits on OpenCode; Anthropic is refused before it.
    for dest in (ANTHROPIC,):
        with pytest.raises(md.DestinationRefused, match="destination_requires_other_runtime"):
            md.require_runtime(dest, md.RUNTIME_OPENCODE)
    assert md.require_runtime(OPENAI, md.RUNTIME_OPENCODE) is OPENAI


@pytest.mark.parametrize(
    ("evidence", "mode"),
    [
        (ANTHROPIC, OPENAI),
        (OPENAI, ANTHROPIC),
    ],
)
def test_cross_provider_evidence_refused(evidence, mode):
    assert not _account_route_admitted(account_for(evidence), mode)


@pytest.mark.parametrize("dest", [OPENAI])
@pytest.mark.parametrize(
    "change",
    [
        {"provider_id": "other"},
        {"provider_id": "openai"},
        {"provider_id": "anthropic"},
        {"method": "key"},
        {"route": "paid-api"},
        {"route": "chatgpt-included"},
        {"route": "claude-subscription"},
    ],
)
def test_mismatched_account_fields_refused(dest, change):
    account = account_for(dest, **change)
    if account["provider_id"] == dest.provider_id and account["route"] == dest.billing_route:
        if account["method"] == "oauth":
            pytest.skip("identical to the admitted shape")
    assert not _account_route_admitted(account, dest)


@pytest.mark.parametrize("dest", [OPENAI])
@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["allowed_auth_methods"].append("key"),
        lambda p: p["billing_routes"].append("paid-api"),
        lambda p: p["billing_routes"].append("chatgpt-included"),
        lambda p: p.update(on_unknown="paid-fallback"),
        lambda p: p.update(on_exhausted="continue"),
        lambda p: p.update(extra=True),
        lambda p: p.pop("on_exhausted"),
    ],
)
def test_policy_must_match_destination_exactly(dest, mutate):
    account = account_for(dest)
    policy = copy.deepcopy(account["policy"])
    mutate(policy)
    if policy == dest.guard_policy():
        pytest.skip("mutation was a no-op")
    account["policy"] = policy
    assert not _account_route_admitted(account, dest)


def test_unenrolled_account_mode_refused():
    with pytest.raises(md.DestinationRefused):
        md.destination_for_account_mode("paid-api-key")
