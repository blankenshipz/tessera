"""Opt-in campaign enrollment scopes; default scope is unchanged."""

import json

from test_enroll_local_mcp import FakeHTTP, args, helper  # noqa: F401

DEFAULT = ["context:read", "proposal:read", "notebook:read"]


def _actions(args):  # noqa: F811
    fake = FakeHTTP()
    helper.enroll(args, fake.factory)
    return json.loads(fake.calls[-1][2])["external_agent"]["actions"]


def test_default_unchanged_without_campaign_flags(args):  # noqa: F811
    assert _actions(args) == DEFAULT


def test_campaign_flag_adds_exact_actions(args):  # noqa: F811
    args.allow_campaigns = True
    assert _actions(args) == [*DEFAULT, "campaign:submit", "campaign:read", "campaign:cancel"]


def test_evidence_flag_adds_read_scopes_only(args):  # noqa: F811
    args.allow_campaign_evidence = True
    assert _actions(args) == [*DEFAULT, "snapshot:read", "artifact:read", "campaign:logs"]
