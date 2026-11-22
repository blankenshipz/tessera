"""Static checks for the example systemd units under deploy/systemd."""

import configparser
import importlib.util
import re
import shlex
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UNIT_DIR = ROOT / "deploy" / "systemd"
UNIT_FILES = sorted(p for p in UNIT_DIR.iterdir() if p.suffix in {".service", ".timer", ".target"})
SERVICES = sorted(UNIT_DIR.glob("tessera-*-*.service"))
LONG_RUNNING = [
    UNIT_DIR / "tessera-development-poll.service",
    UNIT_DIR / "tessera-salad-executor.service",
    UNIT_DIR / "tessera-salad-guardian.service",
]
HARDENING = {
    "NoNewPrivileges": "yes",
    "PrivateTmp": "yes",
    "ProtectSystem": "strict",
}


def parse(path):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read(path, encoding="utf-8")
    return parser


def test_expected_units_exist():
    names = {p.name for p in UNIT_FILES}
    assert "tessera.target" in names
    assert {p.name for p in LONG_RUNNING} <= names


@pytest.mark.parametrize("path", LONG_RUNNING, ids=lambda p: p.name)
def test_service_shape_and_hardening(path):
    service = parse(path)["Service"]
    assert service["Type"] == "simple"
    assert service["Restart"] == "on-failure"
    assert service["EnvironmentFile"].startswith("-/etc/tessera/")
    assert "ReadWritePaths" in service
    for key, value in HARDENING.items():
        assert service[key] == value
    assert parse(path)["Install"]["WantedBy"] == "tessera.target"


def test_target_wants_every_service():
    wants = parse(UNIT_DIR / "tessera.target")["Unit"]["Wants"].split()
    assert sorted(wants) == sorted(p.name for p in LONG_RUNNING)


@pytest.mark.parametrize("path", LONG_RUNNING, ids=lambda p: p.name)
def test_exec_start_references_real_entry_point(path):
    words = shlex.split(parse(path)["Service"]["ExecStart"])
    assert words[1] == "-m"
    module = words[2]
    assert importlib.util.find_spec(module) is not None
    source = importlib.util.find_spec(module).origin
    text = Path(source).read_text(encoding="utf-8")
    assert "def main" in text
    for flag in (w for w in words[3:] if w.startswith("--")):
        assert flag in text or flag == "--run", flag
    if module == "tessera_worker.cli":
        assert "development-poll" in words
        assert '"development-poll"' in text


def test_health_check_script_present_and_probed_services_have_health_file():
    assert (UNIT_DIR / "tessera-health-check").stat().st_mode & 0o111
    for name in ("development-poll", "salad-executor"):
        exec_start = parse(UNIT_DIR / f"tessera-{name}.service")["Service"]["ExecStart"]
        assert f"--health-file /var/lib/tessera/health/{name}.json" in exec_start
        assert (UNIT_DIR / f"tessera-health-check-{name}.timer").exists()


@pytest.mark.parametrize(
    "path", UNIT_FILES + [UNIT_DIR / "README.md", UNIT_DIR / "tessera-health-check"]
)
def test_no_personal_paths_or_secrets(path):
    text = path.read_text(encoding="utf-8")
    assert "/home/" not in text and "/Users/" not in text
    assert not re.search(r"(?i)(password|secret|token|api[_-]?key)\s*=\s*\S", text)
    assert not re.search(r"(sk|ghp|xox[bp])[-_][A-Za-z0-9]{16,}", text)
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text)


def test_salad_services_have_console_scripts_and_launchd_templates() -> None:
    import plistlib
    import tomllib

    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts["tessera-salad-executor"] == "tessera_control.campaign_salad_executor:main"
    assert scripts["tessera-salad-guardian"] == "tessera_control.salad_safety_service:main"
    for name in ("salad-executor", "salad-guardian"):
        plist = plistlib.loads(
            (ROOT / "deploy" / "launchd" / f"dev.tessera.{name}.plist").read_bytes()
        )
        assert plist["ProgramArguments"][0].endswith(f"tessera-{name}")
        assert "/Users/" not in str(plist) and "/home/" not in str(plist)
    guardian = plistlib.loads(
        (ROOT / "deploy" / "launchd" / "dev.tessera.salad-guardian.plist").read_bytes()
    )
    assert "--run" in guardian["ProgramArguments"]
