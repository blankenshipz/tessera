"""``tessera reasoning install``: the plan, the refusals and the exit codes (ADR 0119)."""

from __future__ import annotations

import json

import pytest
from test_reasoning_install import SENTINEL_MODEL, SENTINEL_REF, env  # noqa: F401 (fixture)

from tessera_control.cli import main


def _argv(options_env, *extra):
    options, src = options_env
    return [
        "tessera", "reasoning", "install", "--root", str(options.root),
        "--profile-template", str(src / "template.json"),
        "--prompt-source", str(src / "prompt.txt"),
        "--max-cost-cents-per-call", "10",
        "--daily-cost-cap-cents", "100",
        "--daily-token-cap", "10000",
        *extra,
    ]  # fmt: skip


def test_the_default_is_a_plan_that_changes_nothing(env, capsys, monkeypatch):  # noqa: F811
    options, _ = env
    monkeypatch.setattr("sys.argv", _argv(env))
    assert main() == 0
    out = capsys.readouterr().out
    assert "[missing] service_user" in out and "nothing is changed; pass --apply" in out
    assert list(options.root.iterdir()) == []  # the staging root is untouched


def test_the_json_plan_names_each_step_and_no_profile_value(env, capsys, monkeypatch):  # noqa: F811
    monkeypatch.setattr("sys.argv", _argv(env, "--plan", "--format", "json"))
    assert main() == 0
    text = capsys.readouterr().out
    body = json.loads(text)
    names = [step["name"] for step in body["steps"]]
    assert body["mode"] == "plan" and names[0] == "service_user" and "systemd_unit" in names
    assert SENTINEL_MODEL not in text and SENTINEL_REF not in text


def test_apply_and_plan_cannot_be_combined(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr("sys.argv", _argv(env, "--plan", "--apply"))
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2


def test_apply_with_a_staging_root_refuses_without_touching_the_system(env, capsys, monkeypatch):  # noqa: F811
    options, _ = env
    monkeypatch.setattr("sys.argv", _argv(env, "--apply"))
    assert main() == 2  # a staging root needs injected accounts and runner: no real useradd
    assert "needs injected accounts" in capsys.readouterr().err
    assert list(options.root.iterdir()) == []


def test_a_missing_template_is_a_clear_refusal_not_a_guess(env, capsys, monkeypatch):  # noqa: F811
    options, _ = env
    monkeypatch.setattr(
        "sys.argv",
        ["tessera", "reasoning", "install", "--root", str(options.root),
         "--prompt-source", str(env[1] / "prompt.txt")],
    )  # fmt: skip
    assert main() == 2
    err = capsys.readouterr().err
    assert "reasoning install:" in err and "--profile-template" in err


def test_the_install_never_offers_a_way_to_pass_a_key_or_token():
    from tessera_control.cli import parser

    install = parser().parse_args(["reasoning", "install"])
    flags = {
        action.dest
        for action in parser()
        ._subparsers._group_actions[0]
        .choices["reasoning"]
        ._subparsers._group_actions[0]
        .choices["install"]
        ._actions
    }
    assert install.reasoning_action == "install"
    secretish = {n for n in flags if n.endswith(("_key", "_secret", "_password")) or n == "token"}
    assert not secretish and not {n for n in flags if "api_key" in n or "agent_token" in n}
