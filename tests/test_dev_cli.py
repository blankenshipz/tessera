"""The `tessera dev` group is shared: every developer helper registers into one parser."""

from __future__ import annotations

from tessera_control.cli import parser


def test_focused_tests_and_smoke_credentials_share_one_dev_group():
    cli = parser()
    focused = cli.parse_args(["dev", "focused-tests", "a..b", "--jobs", "2"])
    assert focused.command == "dev" and focused.dev_action == "focused-tests"
    smoke = cli.parse_args(["dev", "smoke-credentials", "list", "--state-dir", "/tmp/state"])
    assert smoke.command == "dev" and smoke.dev_action == "smoke-credentials"
    assert smoke.smoke_action == "list"


def test_registering_the_dev_group_twice_adds_to_it_instead_of_failing():
    import argparse

    from tessera_control.dev_cli import dev_actions

    root = argparse.ArgumentParser()
    commands = root.add_subparsers(dest="command")
    first = dev_actions(commands)
    assert dev_actions(commands) is first
    first.add_parser("one")
    dev_actions(commands).add_parser("two")
    assert root.parse_args(["dev", "two"]).dev_action == "two"
