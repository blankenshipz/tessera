"""Static portable profile and isolated source staging; no Docker/host contact."""

import importlib.util
from pathlib import Path

import pytest
import yaml

from tessera_agent import service_startup

ROOT = Path(__file__).parents[1]
PROFILE = ROOT / "deploy/remote-agent-platform/docker"


def test_profile_is_scoped_private_bounded_and_no_credential_values():
    data = yaml.safe_load((PROFILE / "compose.yaml").read_text())
    assert data["name"] == "tessera-remote-agent-platform"
    assert data["networks"]["private"]["internal"] is True
    assert set(data["services"]) == {"control", "peer-a", "peer-b", "research"}
    for name, service in data["services"].items():
        assert service["platform"] == "linux/amd64"
        assert service["user"] == "65532:65532" and service["read_only"]
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert 0 < service["cpus"] <= 0.5 and service["pids_limit"] <= 128
        assert service["pull_policy"] == "never"
        assert "env_file" not in service
        assert set(service.get("environment", {})) <= {"CREDENTIALS_DIRECTORY"}
        for mount in service["volumes"]:
            assert mount["source"].startswith("/srv/tessera-remote/")
            assert mount["bind"]["create_host_path"] is False
            assert "sock" not in mount["source"]
            if mount["target"].startswith("/run/credentials"):
                assert mount["read_only"]
        if name != "control":
            assert "ports" not in service
            assert service["network_mode"] == "service:control"
    assert data["services"]["control"]["ports"] == ["127.0.0.1:18765:8765"]
    assert data["services"]["research"]["restart"] == "no"
    assert data["services"]["research"]["profiles"] == ["research-admission-required"]


def test_image_uses_existing_pins_installed_extras_and_no_workspace_copy():
    docker = (PROFILE / "Dockerfile").read_text()
    original = (ROOT / "Dockerfile").read_text()
    for line in docker.splitlines()[:2]:
        assert line in original
    assert "--locked --no-dev --no-editable --extra mcp --extra a2a" in docker
    assert "USER 65532:65532" in docker and "COPY . ." not in docker


def staging_module():
    spec = importlib.util.spec_from_file_location("staging", PROFILE / "stage_sources.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_selected_staging_excludes_state_auth_hidden_files_and_symlinks(tmp_path):
    module = staging_module()
    source = tmp_path / "source"
    source.mkdir()
    required = list(module.FILES) + [f"skills/{name}/SKILL.md" for name in module.SKILLS]
    required += [f"deploy/remote-agent-platform/docker/{name}" for name in module.DOCKER_FILES]
    for name in required + [
        "src/package/main.py",
        "src/.cache/hidden.py",
        "src/auth.json",
        "src/credentials/private.py",
        ".tessera/local.db",
        "src/__pycache__/cache.py",
        "src/local.sqlite",
    ]:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic fixture")
    paths = module.stage(source, tmp_path / "staged")
    assert Path("src/package/main.py") in paths
    assert all(".cache" not in str(path) and "__pycache__" not in str(path) for path in paths)
    assert not (tmp_path / "staged/src/auth.json").exists()
    assert not (tmp_path / "staged/src/credentials/private.py").exists()
    assert not (tmp_path / "staged/.tessera").exists()
    (source / "src/package/link.py").symlink_to(source / "src/package/main.py")
    with pytest.raises(ValueError, match="symlinks"):
        module.selected(source)


def test_research_mapper_validates_reference_config_before_credentials_or_harness(
    monkeypatch, capsys
):
    from tessera_agent import research_service

    reads = []

    def forbidden(*args):
        raise AssertionError("credential reader must not run")

    monkeypatch.setattr(service_startup, "credential_root", lambda *a: Path("/synthetic/private"))
    monkeypatch.setattr(service_startup, "token_reference", forbidden)

    def config_only(path):
        reads.append(str(path))
        return b'{"schema_version":"unsupported"}'

    monkeypatch.setattr(research_service, "private_file", config_only)
    assert service_startup.main(["research", "--state-dir", "/state/research"]) == 78
    assert "startup blocked" in capsys.readouterr().err
    assert reads == ["/synthetic/private/profile"]
