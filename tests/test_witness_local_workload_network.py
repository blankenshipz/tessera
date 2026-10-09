"""Isolated Kubernetes subprocess boundary for the opt-in workload witness."""

import importlib.util
import json
import sys
from pathlib import Path
from subprocess import CompletedProcess

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location(
    "witness_local_workload_network", SCRIPTS / "witness_local_workload_network.py"
)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
IMAGE = "docker.io/library/tessera-local-lab@sha256:" + "a" * 64
NONCE = "b" * 32


def _fixture(
    tmp_path,
    monkeypatch,
    denied="denied_connect",
    change_policy=False,
    denied_error="ConnectionRefusedError",
    first_allowed=None,
):
    config = tmp_path / "config"
    config.write_text("fixture")
    monkeypatch.setattr(module.secrets, "token_hex", lambda n: "a" * (n * 2))
    commands = []
    resources = {}
    policy_reads = 0
    readouts = iter((*(first_allowed or ()), "connected", denied, "connected"))

    def run(cmd, **kwargs):
        nonlocal policy_reads
        commands.append(cmd)
        assert cmd[:5] == ["kubectl", "--kubeconfig", str(config), "--context", "docker-desktop"]
        assert kwargs["timeout"] <= 30
        if "create" in cmd:
            data = json.loads(kwargs["input"])
            name = data["metadata"]["name"]
            obj = {
                **data,
                "metadata": {**data["metadata"], "uid": f"uid-{name}"},
            }
            if data["kind"] == "Pod":
                obj["status"] = {"phase": "Running", "podIP": "10.1.0.3"}
            resources[(data["kind"].lower(), name)] = obj
        elif "get" in cmd:
            kind, name = cmd[cmd.index("get") + 1 : cmd.index("get") + 3]
            if kind == "namespace":
                uid = "cluster-uid" if name == "kube-system" else "workload-uid"
                obj = {
                    "metadata": {"name": name, "uid": uid, "labels": {module.OWNER_LABEL: NONCE}}
                }
            elif kind == "networkpolicy" and name == "default-deny":
                policy_reads += 1
                obj = {
                    "metadata": {
                        "name": name,
                        "namespace": module.WORKLOAD,
                        "uid": "policy-uid"
                        + ("-changed" if change_policy and policy_reads > 5 else ""),
                    },
                    "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
                }
            elif kind == "pvc":
                obj = {
                    "metadata": {"name": name, "namespace": module.WORKLOAD, "uid": "pvc-uid"},
                    "status": {"phase": "Bound"},
                    "spec": {"accessModes": ["ReadWriteOnce"]},
                }
            else:
                obj = resources[(kind, name)]
        elif "wait" in cmd:
            obj = None
        elif "exec" in cmd:
            obj = {"outcome": next(readouts)}
            if obj["outcome"] == "denied_connect":
                obj["error_type"] = denied_error
        else:
            pytest.fail("unexpected Kubernetes operation")
        return CompletedProcess(cmd, 0, "" if obj is None else json.dumps(obj), "")

    return config, run, commands


def test_witness_uses_real_readback_contract_and_brackets_denied(tmp_path, monkeypatch):
    config, run, calls = _fixture(tmp_path, monkeypatch)
    result = module.witness(config, "workload-uid", NONCE, IMAGE, run=run)
    assert result["state"] == "workload_network_witness_not_dispatch_authority"
    assert result["readings"] == (
        {"outcome": "connected"},
        {"outcome": "denied_connect", "error_type": "ConnectionRefusedError"},
        {"outcome": "connected"},
    )
    evidence = result["network_evidence"]
    assert evidence["cluster_uid"] == "cluster-uid"
    assert evidence["namespace_uid"] == "workload-uid"
    assert evidence["policy_uid"] == "policy-uid"
    assert (
        evidence["allowed_connectivity"]["destination_pod_uid"]
        == evidence["denied_connectivity"]["destination_pod_uid"]
    )
    assert (
        evidence["allowed_connectivity"]["source_pod_uid"]
        != evidence["denied_connectivity"]["source_pod_uid"]
    )
    assert len([cmd for cmd in calls if "create" in cmd]) == 5
    assert len([cmd for cmd in calls if "exec" in cmd]) == 3
    assert not any("delete" in cmd or "secret" in cmd for cmd in calls)


def test_connected_denied_pod_blocks_without_job(tmp_path, monkeypatch):
    config, run, calls = _fixture(tmp_path, monkeypatch, denied="connected")
    with pytest.raises(module.WorkloadWitnessError, match="witness failed"):
        module.witness(config, "workload-uid", NONCE, IMAGE, run=run)
    assert not any("job" in cmd or "delete" in cmd for cmd in calls)


def test_timeout_denied_pod_blocks_without_job(tmp_path, monkeypatch):
    config, run, calls = _fixture(tmp_path, monkeypatch, denied_error="TimeoutError")
    with pytest.raises(module.WorkloadWitnessError, match="TimeoutError"):
        module.witness(config, "workload-uid", NONCE, IMAGE, run=run)
    assert not any("job" in cmd or "delete" in cmd for cmd in calls)


def test_waits_for_initial_positive_before_sampling_denial(tmp_path, monkeypatch):
    config, run, calls = _fixture(tmp_path, monkeypatch, first_allowed=("denied_connect",))
    result = module.witness(config, "workload-uid", NONCE, IMAGE, run=run)
    assert result["readings"][0] == {"outcome": "connected"}
    probes = [cmd[cmd.index("exec") + 1] for cmd in calls if "exec" in cmd]
    assert probes[0] == probes[1] and "allowed" in probes[0]
    assert "denied" in probes[2]


def test_changed_policy_blocks(tmp_path, monkeypatch):
    config, run, calls = _fixture(tmp_path, monkeypatch, change_policy=True)
    with pytest.raises(module.WorkloadWitnessError, match="policy/PVC identity changed"):
        module.witness(config, "workload-uid", NONCE, IMAGE, run=run)
    assert not any("job" in cmd for cmd in calls)


def test_bad_image_blocks_before_kubernetes(tmp_path):
    config = tmp_path / "config"
    config.write_text("fixture")

    def forbidden(*args, **kwargs):
        pytest.fail("no Kubernetes calls allowed")

    with pytest.raises(module.WorkloadWitnessError, match="pinned"):
        module.witness(config, "workload-uid", NONCE, "latest", run=forbidden)


@pytest.mark.parametrize("failure", [None, "grant_timeout", "revoke_connected", "wrong_uid"])
def test_causal_workload_grant_revoke_same_pod(tmp_path, monkeypatch, failure):
    config, original, calls = _fixture(tmp_path, monkeypatch, denied_error="TimeoutError")
    active = set()
    executions = 0
    if failure in {"grant_timeout", "revoke_connected"}:
        ticks = iter(range(0, 1000, 2))
        monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
        monkeypatch.setattr(module.time, "sleep", lambda _: None)

    def run(cmd, **kwargs):
        nonlocal executions
        if "delete" in cmd:
            name = cmd[cmd.index("delete") + 1].rsplit("/", 1)[-1]
            assert json.loads(kwargs["input"])["preconditions"]["uid"] == "uid-" + name
            active.remove(name)
            return CompletedProcess(cmd, 0, '{"kind":"Status","status":"Success"}', "")
        if "exec" in cmd:
            executions += 1
            if executions > 3:
                pod = cmd[cmd.index("exec") + 1]
                if "allowed" in pod:
                    outcome = "connected"
                elif len(active) == 2:
                    outcome = "denied_connect" if failure == "grant_timeout" else "connected"
                else:
                    outcome = "connected" if failure == "revoke_connected" else "denied_connect"
                result = {"outcome": outcome}
                if outcome == "denied_connect":
                    result["error_type"] = "TimeoutError"
                return CompletedProcess(cmd, 0, json.dumps(result), "")
        result = original(cmd, **kwargs)
        if "create" in cmd:
            name = json.loads(kwargs["input"])["metadata"]["name"]
            if "causal" in name:
                active.add(name)
        if "get" in cmd and failure == "wrong_uid" and any(n in cmd for n in active):
            value = json.loads(result.stdout)
            value["metadata"]["uid"] += "-changed"
            return CompletedProcess(cmd, 0, json.dumps(value), "")
        return result

    if failure:
        with pytest.raises(module.WorkloadWitnessError):
            module.witness(config, "workload-uid", NONCE, IMAGE, causal=True, run=run)
    else:
        receipt = module.witness(config, "workload-uid", NONCE, IMAGE, causal=True, run=run)
        assert [x["phase"] for x in receipt["causal_transitions"]] == [
            "grant",
            "revoke",
            "grant",
            "revoke",
        ]
        assert not active
        assert len(receipt["retained"]) == 5
