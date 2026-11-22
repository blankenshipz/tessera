"""Offline checks for the dedicated garage control deployment profile."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "deploy" / "garage-control"


def _compose_config(
    *,
    host: str | None = "control.example.invalid",
    origin: str | None = "https://control.example.invalid",
    profiles: bool = False,
):
    if not shutil.which("docker"):
        pytest.skip("Docker Compose CLI not installed; offline source checks remain available")
    environment = os.environ.copy()
    environment.pop("TESSERA_TRUSTED_HOST", None)
    environment.pop("TESSERA_PUBLIC_ORIGIN", None)
    if host is not None:
        environment["TESSERA_TRUSTED_HOST"] = host
    if origin is not None:
        environment["TESSERA_PUBLIC_ORIGIN"] = origin
    args = ["docker", "compose", "-f", str(PROFILE / "compose.yaml")]
    if profiles:
        args.extend(["--profile", "maintenance"])
    args.extend(["config", "--format", "json"])
    return subprocess.run(
        args, cwd=ROOT, env=environment, capture_output=True, text=True, check=False, timeout=15
    )


@pytest.mark.parametrize(
    "host,origin", [(None, "https://control.example.invalid"), ("control.example.invalid", None)]
)
def test_requires_explicit_remote_origin_and_host(host, origin):
    result = _compose_config(host=host, origin=origin)
    assert result.returncode != 0


def test_default_is_single_control_service_private_persistent_and_restartable():
    result = _compose_config()
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    assert config["name"] == "tessera-garage-control"
    assert set(config["services"]) == {"control"}
    control = config["services"]["control"]
    assert control["restart"] == "unless-stopped"
    assert control["ports"] == [
        {
            "mode": "ingress",
            "target": 8765,
            "published": "18766",
            "protocol": "tcp",
            "host_ip": "127.0.0.1",
        }
    ]
    assert control["command"][-4:] == [
        "--trusted-host",
        "control.example.invalid",
        "--public-origin",
        "https://control.example.invalid",
    ]
    assert control["user"] == "65532:65532"
    assert control["read_only"] is True
    assert control["cap_drop"] == ["ALL"]
    assert control["pull_policy"] == "never"
    assert control["volumes"] == [
        {
            "type": "bind",
            "source": "/srv/tessera-garage/control",
            "target": "/state/control",
            "bind": {"create_host_path": False},
        }
    ]
    assert "owner.header" not in (PROFILE / "compose.yaml").read_text()


def test_backup_is_opt_in_networkless_and_separate_from_state():
    result = _compose_config(profiles=True)
    assert result.returncode == 0, result.stderr
    backup = json.loads(result.stdout)["services"]["backup"]
    assert backup["restart"] == "no"
    assert backup["network_mode"] == "none"
    assert not backup.get("ports")
    assert {volume["source"]: volume["target"] for volume in backup["volumes"]} == {
        "/srv/tessera-garage/control": "/state/control",
        "/srv/tessera-garage/backups": "/backups",
    }
    assert all(volume["bind"]["create_host_path"] is False for volume in backup["volumes"])


def test_build_context_allowlist_excludes_runtime_and_unrelated_sources():
    exclusions = (PROFILE / "Dockerfile.dockerignore").read_text().splitlines()
    assert exclusions[0] == "**"
    assert "!src/**" in exclusions
    assert not any(
        ".tessera" in line or "!desktop" in line or "!tests" in line for line in exclusions
    )
    dockerfile = (PROFILE / "Dockerfile").read_text()
    assert "COPY src ./src" in dockerfile
    assert "COPY . ." not in dockerfile
