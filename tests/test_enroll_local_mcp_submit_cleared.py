"""Enrolling the scope that consumes an approved campaign submission is its own opt-in."""

import json

from test_enroll_local_mcp import FakeHTTP, args, helper  # noqa: F401

DEFAULT = ["context:read", "proposal:read", "notebook:read"]


def _actions(args):  # noqa: F811
    fake = FakeHTTP()
    helper.enroll(args, fake.factory)
    return json.loads(fake.calls[-1][2])["external_agent"]["actions"]


def test_submit_cleared_is_separate_from_allow_campaigns(args):  # noqa: F811
    args.allow_campaigns = True
    assert "campaign:submit_cleared" not in _actions(args)


def test_submit_cleared_flag_adds_exact_actions(args):  # noqa: F811
    args.allow_campaign_submit_cleared = True
    assert _actions(args) == [
        *DEFAULT,
        "campaign:submit_cleared",
        "campaign:read",
        "campaign:cancel",
    ]


def test_submit_cleared_with_campaigns_has_no_duplicates(args):  # noqa: F811
    args.allow_campaigns = True
    args.allow_campaign_submit_cleared = True
    actions = _actions(args)
    assert len(actions) == len(set(actions)) and "campaign:submit_cleared" in actions
