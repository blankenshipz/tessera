"""Offline checks for the read-only remote target prerequisite gate."""

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

_script = runpy.run_path(str(Path(__file__).parents[1] / "dev/check_dispatch_target.py"))
TargetError = _script["TargetError"]
inspect_target = _script["inspect_target"]


def _runner(*, bound=True, deny=True):
    observed = []

    def run(command, **kwargs):
        assert kwargs["timeout"] == 8
        assert command[:4] == ["kubectl", "--kubeconfig", command[2], "--context"]
        assert "--request-timeout=5s" in command
        assert "secret" not in command and "logs" not in command
        observed.append(command)
        kind, name = command[command.index("get") + 1 : command.index("get") + 3]
        uid = f"{kind}-{name}-uid"
        value = {"metadata": {"uid": uid, "name": name}}
        if kind != "namespace":
            value["metadata"]["namespace"] = command[command.index("--namespace") + 1]
        if kind == "pvc":
            value.update(
                status={"phase": "Bound" if bound else "Pending"},
                spec={"accessModes": ["ReadWriteOnce"]},
            )
        if kind == "networkpolicy":
            value["spec"] = (
                {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}
                if deny
                else {"podSelector": {}}
            )
        return SimpleNamespace(stdout=json.dumps(value))

    return run, observed


def test_preflight_names_exact_resources_and_reports_only_nonsecret_identity(tmp_path: Path):
    config = tmp_path / "kubeconfig"
    config.touch()
    run, observed = _runner()
    result = inspect_target(config, "docker-desktop", "control", "workload", run=run)
    assert result["status"] == "prerequisites_observed_not_enforcement_proven"
    assert len(result["output_pvc_spec_sha256"]) == 64
    assert len(observed) == 4
    assert all("--context" in command for command in observed)


@pytest.mark.parametrize("bound,deny", [(False, True), (True, False)])
def test_preflight_rejects_unready_pvc_or_absent_default_deny(tmp_path, bound, deny):
    config = tmp_path / "kubeconfig"
    config.touch()
    run, _ = _runner(bound=bound, deny=deny)
    with pytest.raises(TargetError):
        inspect_target(config, "docker-desktop", "control", "workload", run=run)


def test_preflight_rejects_same_namespace_before_any_cluster_call(tmp_path):
    config = tmp_path / "kubeconfig"
    config.touch()
    run, observed = _runner()
    with pytest.raises(TargetError, match="must differ"):
        inspect_target(config, "docker-desktop", "control", "control", run=run)
    assert not observed


@pytest.mark.parametrize("bad_namespace", ["-invalid", "INVALID", "a_underscore", "a" * 64])
def test_preflight_rejects_invalid_names_without_a_cluster_call(tmp_path, bad_namespace):
    config = tmp_path / "kubeconfig"
    config.touch()
    run, observed = _runner()
    with pytest.raises(TargetError, match="DNS labels"):
        inspect_target(config, "docker-desktop", "control", bad_namespace, run=run)
    assert not observed


def test_workload_example_is_distinct_deny_by_default_and_quota_bounded():
    root = Path(__file__).parents[1]
    namespace, policy, pvc, quota = yaml.safe_load_all(
        (root / "deploy/kubernetes/workload-target.example.yaml").read_text()
    )
    assert namespace["metadata"]["name"] != "tessera-lab"
    assert policy["spec"] == {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}
    assert pvc["metadata"]["namespace"] == namespace["metadata"]["name"]
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
    # Existing bench ceiling permits one retained historical Job and two variants;
    # this administrative bound still creates no per-dispatch approval.
    assert quota["spec"]["hard"]["count/jobs.batch"] == "3"
    assert quota["spec"]["hard"]["count/pods"] == "4"
