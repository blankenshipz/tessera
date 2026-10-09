"""The persistent-control Kubernetes fixture is inert unless explicitly enabled."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts" / "check_kubernetes_lab_experiment_e2e.py"
    spec = importlib.util.spec_from_file_location("lab_experiment_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_lab_experiment_runner_skips_without_run(monkeypatch, capsys) -> None:
    module = _module()
    monkeypatch.setattr(sys, "argv", ["runner"])
    assert module.main() == 1
    assert json.loads(capsys.readouterr().out)["fixture_verdict"] == "skipped"


def test_lab_experiment_runner_requires_a_fixture_owned_namespace(monkeypatch, capsys) -> None:
    module = _module()
    monkeypatch.setattr(
        sys, "argv", ["runner", "--run", "--kubeconfig", "/tmp/config", "--namespace", "shared"]
    )
    assert module.main() == 1
    assert json.loads(capsys.readouterr().out)["state"] == "not_started"


def test_lab_experiment_runner_materializes_only_verified_outputs_in_fresh_workspace(
    tmp_path: Path,
) -> None:
    module = _module()
    content = b"fixture output\n"
    bundle = {
        "outputs": [
            {
                "relative_path": "result.txt",
                "content_base64": base64.b64encode(content).decode("ascii"),
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        ],
        "missing_materials": [
            {
                "kind": "input_bytes",
                "reason": "not_exported: input snapshots remain separately released",
            },
            {"kind": "skill_material", "reason": "restricted: digest only"},
            {"kind": "environment_image", "reason": "missing: reference only"},
        ],
    }

    result = module._materialize_fixture_reproduction_bundle(bundle, tmp_path / "fresh")

    assert (tmp_path / "fresh" / "result.txt").read_bytes() == content
    assert result == {
        "outputs": [
            {
                "relative_path": "result.txt",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        ],
        "missing_material_kinds": ["environment_image", "input_bytes", "skill_material"],
    }


@pytest.mark.parametrize(
    "bundle",
    [
        {
            "outputs": [],
            "missing_materials": [
                {"kind": "input_bytes", "reason": "not_exported: input"},
                {"kind": "skill_material", "reason": "restricted: digest only"},
                {"kind": "environment_image", "reason": "missing: reference only"},
            ],
        },
        {
            "outputs": [
                {
                    "relative_path": "../escape",
                    "content_base64": "eA==",
                    "sha256": hashlib.sha256(b"x").hexdigest(),
                    "size": 1,
                }
            ],
            "missing_materials": [
                {"kind": "input_bytes", "reason": "not_exported: input"},
                {"kind": "skill_material", "reason": "restricted: digest only"},
                {"kind": "environment_image", "reason": "missing: reference only"},
            ],
        },
    ],
)
def test_lab_experiment_runner_rejects_incomplete_or_unsafe_fresh_workspace_bundle(
    tmp_path: Path, bundle: dict[str, object]
) -> None:
    module = _module()

    with pytest.raises(module.LabExperimentError):
        module._materialize_fixture_reproduction_bundle(bundle, tmp_path / "fresh")
