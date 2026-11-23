# ruff: noqa: F811
"""ADR 0097 on the local worker-grant path: spec, service config and the public-only refusal."""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import pytest
from campaign_runner_harness import World
from test_campaign_models_v2 import _spec2
from test_campaign_public_artifact_spec import (  # noqa: F401
    _public,
    _snapshot,
    _steps,
    _v2_stage,
    local_worker,
)
from test_development_worker_service import _delivery, _service

from tessera_control.campaign_models import (
    normalize,
    parse_spec,
    public_fetch_policy,
    stage_execution_spec,
)
from tessera_worker.grant_poller import GrantPollerConfig, load_poller_config
from tessera_worker.service import DevelopmentWorkerError, operator_private_directory

DIGEST = "0" * 64


def _stage_spec(world: World, *inputs: dict):
    return parse_spec(_spec2(_v2_stage(world, "a", *inputs)))


def test_mixed_stage_spec_carries_public_fetch_and_changes_the_plan_digest(local_worker):
    world = local_worker
    mixed = _stage_spec(world, _snapshot(world), _public())
    plain = _stage_spec(world, _snapshot(world))
    with_fetch = stage_execution_spec(mixed, "a", DIGEST)
    without = stage_execution_spec(plain, "a", DIGEST)
    assert with_fetch["public_fetch"] == public_fetch_policy(normalize(mixed).stage("a"))
    assert with_fetch["profile"] == "local-cpu"
    assert "public_fetch" not in without
    assert json.dumps(with_fetch, sort_keys=True) != json.dumps(without, sort_keys=True)
    # Everything but the fetch plan is the same: stages without a public input are unchanged.
    assert {k: v for k, v in with_fetch.items() if k != "public_fetch"} == without


def test_public_only_stage_fails_closed_with_an_explicit_reason(local_worker):
    world = local_worker
    campaign = world.campaign(_spec2(_v2_stage(world, "a", _public())))
    world.tick_all(campaign["id"], limit=40)
    assert world.status(campaign["id"]) != "succeeded"
    attempt = world.row("SELECT * FROM campaign_stage_attempts WHERE campaign_id=?", campaign["id"])
    assert attempt["status"] == "failed_permanent"
    assert attempt["failure_class"] == "step_refused"
    assert attempt["failure_detail"].startswith("public_only_stage_needs_a_snapshot_input:")
    assert world.status(campaign["id"]) == "failed"
    steps = _steps(world, campaign["id"])
    assert not steps & {"issue_grant", "release_snapshot", "compose_snapshot"}


def test_mixed_stage_task_revision_carries_the_public_fetch(local_worker):
    world = local_worker
    world.campaign(_spec2(_v2_stage(world, "a", _snapshot(world), _public())))
    query = "SELECT execution_spec_json FROM task_revisions WHERE execution_spec_json LIKE ?"
    world.tick_until(
        lambda: bool(world.env.query(query, '%"public_fetch"%')), worker=False, limit=60
    )
    rows = world.env.query(
        "SELECT execution_spec_json FROM task_revisions WHERE execution_spec_json LIKE ?",
        '%"public_fetch"%',
    )
    assert rows
    plan = json.loads(rows[-1]["execution_spec_json"])["public_fetch"]
    assert plan["files"][0]["sha256"] == "c" * 64 and plan["cache"] is False


# -- service / poller config --------------------------------------------------------------------


def test_service_builds_adapter_and_runtime_with_the_same_public_root(tmp_path, monkeypatch):
    from tessera_worker import service as service_module

    state, _, _, _, grant, _ = _delivery(tmp_path)
    built: dict = {}

    class Capture:
        def __init__(self, *args, **kwargs):
            built.update(kwargs)
            raise RuntimeError("stop after adapter construction")

    monkeypatch.setattr(service_module, "DockerCpuAdapter", Capture)
    service = _service(tmp_path, state, grant)
    root = tmp_path / "public"
    cache = tmp_path / "cache"
    service.config = dataclasses.replace(service.config, public_root=root, public_cache_dir=cache)
    service.public_root = operator_private_directory(root)
    service.public_cache_dir = operator_private_directory(cache)
    service.adapter = None
    with pytest.raises(RuntimeError, match="stop after"):
        service.run()
    assert built["public_root"] == root.resolve()
    assert (os.stat(root).st_mode & 0o777) == 0o700


def test_service_without_public_root_passes_none(tmp_path, monkeypatch):
    from tessera_worker import service as service_module

    state, _, _, _, grant, _ = _delivery(tmp_path)
    built: dict = {}

    class Capture:
        def __init__(self, *args, **kwargs):
            built.update(kwargs)
            raise RuntimeError("stop after adapter construction")

    monkeypatch.setattr(service_module, "DockerCpuAdapter", Capture)
    service = _service(tmp_path, state, grant)
    service.adapter = None
    with pytest.raises(RuntimeError, match="stop after"):
        service.run()
    assert built["public_root"] is None


def test_public_directory_is_created_private_and_refused_when_shared_or_linked(tmp_path):
    made = operator_private_directory(tmp_path / "a" / "public")
    assert (os.stat(made).st_mode & 0o777) == 0o700
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o755)
    with pytest.raises(DevelopmentWorkerError, match="private 0700"):
        operator_private_directory(shared)
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(DevelopmentWorkerError, match="private 0700"):
        operator_private_directory(link)


def test_poller_config_file_accepts_public_paths(tmp_path):
    config = tmp_path / "poll.json"
    config.write_text(
        json.dumps(
            {
                "schema": "tessera.worker.development-poll.v1",
                "control_url": "http://127.0.0.1:1",
                "credential_file": str(tmp_path / "cred"),
                "workspace_root": str(tmp_path / "w"),
                "development_output_root": str(tmp_path / "o"),
                "allowed_images": ["img@sha256:" + "a" * 64],
                "public_root": str(tmp_path / "pub"),
                "public_cache_dir": str(tmp_path / "cache"),
            }
        )
    )
    config.chmod(0o600)
    loaded = load_poller_config(config)
    assert isinstance(loaded, GrantPollerConfig)
    assert loaded.public_root == Path(tmp_path / "pub")
    assert loaded.public_cache_dir == Path(tmp_path / "cache")
