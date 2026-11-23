"""Static checks for the stuck-loop example systemd unit (no systemd needed)."""

import configparser
import re
import shlex
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy" / "systemd" / "tessera-stuck-loop.service"
README = ROOT / "deploy" / "systemd" / "README.md"
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def parse():
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read(UNIT, encoding="utf-8")
    return parser


def test_admission_and_manual_start_only():
    unit = parse()
    assert unit["Unit"]["ConditionPathExists"] == "/etc/tessera/stuck-loop.admitted"
    assert "Install" not in unit.sections()
    assert "PartOf" not in unit["Unit"]
    assert "no [Install] section" in UNIT.read_text(encoding="utf-8")
    target = (ROOT / "deploy" / "systemd" / "tessera.target").read_text(encoding="utf-8")
    assert "stuck-loop" not in target


def test_credential_is_loaded_not_inlined():
    service = parse()["Service"]
    assert service["LoadCredential"].startswith("agent-token:/etc/tessera/credentials/")
    assert "Environment=TESSERA_AGENT_TOKEN_FILE=%d/agent-token" in UNIT.read_text(encoding="utf-8")


def test_exec_start_matches_console_script_and_flags():
    words = shlex.split(parse()["Service"]["ExecStart"])
    scripts = PYPROJECT["project"]["scripts"]
    assert Path(words[0]).name == "tessera-stuck-loop"
    assert scripts["tessera-stuck-loop"] == "tessera_agent.stuck_loop_runner:main"
    for flag in ("--policy", "--journal", "--ledger", "--kill-switch", "--interval-seconds"):
        assert flag in words
    assert words[words.index("--kill-switch") + 1] == "/etc/tessera/stuck-loop.kill"


def test_sandbox_and_restart_policy():
    service = parse()["Service"]
    assert service["NoNewPrivileges"] == "yes"
    assert service["ProtectSystem"] == "strict"
    assert service["CapabilityBoundingSet"] == ""
    assert service["RestartPreventExitStatus"] == "2 3"
    assert service["StateDirectory"] == "tessera/stuck-loop"
    assert service["StateDirectoryMode"] == "0700"
    assert service["ReadWritePaths"] == "/var/lib/tessera/stuck-loop"


def test_listed_in_packaging_and_readme():
    data = PYPROJECT["tool"]["setuptools"]["data-files"]["share/tessera/deploy/systemd"]
    assert "deploy/systemd/tessera-stuck-loop.service" in data
    readme = README.read_text(encoding="utf-8")
    assert "| `tessera-stuck-loop.service` |" in readme
    assert "campaign:read" in readme


def test_no_secret_like_literals():
    text = UNIT.read_text(encoding="utf-8")
    assert not re.search(r"(?i)(password|secret|token|api[_-]?key)\s*=\s*\S", text)
    assert not re.search(r"(sk|ghp|xox[bp])[-_][A-Za-z0-9]{16,}", text)
    assert "/home/" not in text and "/Users/" not in text
