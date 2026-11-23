"""Enrolling the scope that acts inside an approval delegation is its own opt-in (ADR 0122)."""

import json

from test_enroll_local_mcp import FakeHTTP, args, helper  # noqa: F401

DEFAULT = ["context:read", "proposal:read", "notebook:read"]


def _actions(args):  # noqa: F811
    fake = FakeHTTP()
    helper.enroll(args, fake.factory)
    return json.loads(fake.calls[-1][2])["external_agent"]["actions"]


def test_delegated_is_separate_from_campaign_flags(args):  # noqa: F811
    args.allow_campaigns = True
    args.allow_campaign_submit_cleared = True
    assert "campaign:decide_delegated" not in _actions(args)


def test_delegated_flag_adds_exact_actions(args):  # noqa: F811
    args.allow_delegated_decisions = True
    assert _actions(args) == [*DEFAULT, "campaign:decide_delegated", "campaign:read"]


def test_delegated_combined_has_no_duplicates(args):  # noqa: F811
    args.allow_campaigns = True
    args.allow_campaign_submit_cleared = True
    args.allow_delegated_decisions = True
    actions = _actions(args)
    assert len(actions) == len(set(actions)) and "campaign:decide_delegated" in actions
