"""The persistent-control Kubernetes fixture is inert unless explicitly enabled."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


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
