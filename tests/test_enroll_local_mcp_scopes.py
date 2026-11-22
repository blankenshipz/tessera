"""Opt-in results/budget enrollment scopes; default scope is unchanged."""

import json

import pytest
from test_enroll_local_mcp import FakeHTTP, args, helper  # noqa: F401

DEFAULT = ["context:read", "proposal:read", "notebook:read"]


def _actions(args):  # noqa: F811
    fake = FakeHTTP()
    helper.enroll(args, fake.factory)
    return json.loads(fake.calls[-1][2])["external_agent"]["actions"]


def test_default_scope_unchanged(args):  # noqa: F811
    assert _actions(args) == DEFAULT


@pytest.mark.parametrize(
    ("flag", "action"), [("allow_results", "results:read"), ("allow_budget", "budget:read")]
)
def test_each_flag_adds_exactly_one_action(args, flag, action):  # noqa: F811
    setattr(args, flag, True)
    assert _actions(args) == [*DEFAULT, action]


def test_both_flags(args):  # noqa: F811
    args.allow_results = args.allow_budget = True
    assert _actions(args) == [*DEFAULT, "results:read", "budget:read"]
