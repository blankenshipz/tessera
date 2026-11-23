"""A systemd unit that sets TESSERA_CONTROL_STATE_DIR and passes --state-dir keeps them equal."""

from __future__ import annotations

import re
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
ENVIRONMENT = re.compile(r"^Environment=\"?TESSERA_CONTROL_STATE_DIR=([^\s\"]+)\"?\s*$", re.M)
ARGUMENT = re.compile(r"^ExecStart=.*--state-dir[ =](\S+)", re.M)


def test_the_environment_and_state_dir_argument_of_every_unit_are_equal():
    checked = []
    for unit in sorted(DEPLOY.rglob("*.service")):
        text = unit.read_text()
        environment = ENVIRONMENT.findall(text)
        arguments = ARGUMENT.findall(text)
        if environment and arguments:
            checked.append(unit.name)
            assert set(environment) == set(arguments), unit.name
    assert "tessera-control.service" in checked
