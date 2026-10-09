"""No Kubernetes calls: synthetic command responses test the fail-closed live gate."""

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from subprocess import CompletedProcess

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location(
    "gate_local_kubernetes_network", SCRIPTS / "gate_local_kubernetes_network.py"
)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def _fixture(tmp_path, readings):
    config = tmp_path / "config"
    config.write_text("fixture")
    nonce = "a" * 32
    uids = {module.CONTROL: "control-uid", module.WORKLOAD: "workload-uid"}
    calls = []
    outcomes = iter(readings)
    allowed = "tessera-tls-probe-allowed-aaaaaaaaaa"
    denied = "tessera-tls-probe-denied-aaaaaaaaaa"

    def run(cmd, **kwargs):
        calls.append(cmd)
        if "exec" in cmd:
            return CompletedProcess(cmd, 0, json.dumps(next(outcomes)), "")
        name = cmd[cmd.index("get") + 2]
        kind = cmd[cmd.index("get") + 1]
        if kind == "namespace":
            obj = {
                "metadata": {"name": name, "uid": uids[name], "labels": {module.OWNER_LABEL: nonce}}
            }
        elif kind == "networkpolicy":
            selector = {"app.kubernetes.io/name": "tessera-control-tls"}
            source = {"tessera.io/probe-run": "aaaaaaaaaa", "tessera.io/probe-role": "allowed"}
            specs = {
                "default-deny": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
                "tessera-tls-probe-ingress-aaaaaaaaaa": {
                    "podSelector": {"matchLabels": selector},
                    "policyTypes": ["Ingress"],
                    "ingress": [
                        {
                            "from": [{"podSelector": {"matchLabels": source}}],
                            "ports": [{"protocol": "TCP", "port": 443}],
                        }
                    ],
                },
                "tessera-tls-probe-egress-aaaaaaaaaa": {
                    "podSelector": {"matchLabels": source},
                    "policyTypes": ["Egress"],
                    "egress": [
                        {
                            "to": [{"podSelector": {"matchLabels": selector}}],
                            "ports": [{"protocol": "TCP", "port": 443}],
                        }
                    ],
                },
            }
            obj = {
                "metadata": {"name": name, "uid": name + "-uid", "namespace": module.CONTROL},
                "spec": specs[name],
            }
        elif kind == "service":
            obj = {
                "metadata": {"name": name, "uid": "service-uid", "namespace": module.CONTROL},
                "spec": {
                    "selector": {"app.kubernetes.io/name": "tessera-control-tls"},
                    "ports": [{"protocol": "TCP", "port": 443}],
                },
            }
        else:
            labels = {
                allowed: {
                    "tessera.io/tls-control-client": "true",
                    "tessera.io/probe-run": "aaaaaaaaaa",
                    "tessera.io/probe-role": "allowed",
                },
                denied: {"tessera.io/probe-run": "aaaaaaaaaa", "tessera.io/probe-role": "denied"},
                "gateway": {
                    "app.kubernetes.io/name": "tessera-control-tls",
                    "tessera.io/control-hop": "true",
                },
            }
            obj = {
                "metadata": {
                    "name": name,
                    "uid": name + "-uid",
                    "namespace": module.CONTROL,
                    "labels": labels[name],
                    "creationTimestamp": datetime.now(UTC).isoformat(),
                },
                "status": {"phase": "Running", "podIP": "10.1.0.2"},
            }
            if name != "gateway":
                obj["spec"] = {
                    "automountServiceAccountToken": False,
                    "containers": [
                        {
                            "imagePullPolicy": "Never",
                            "securityContext": {"capabilities": {"drop": ["ALL"]}},
                            "volumeMounts": [
                                {"name": "ca", "mountPath": "/run/tessera-ca", "readOnly": True}
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "ca", "secret": {"items": [{"key": "ca.crt", "path": "ca.crt"}]}}
                    ],
                }
        return CompletedProcess(cmd, 0, json.dumps(obj), "")

    return config, uids, nonce, allowed, denied, run, calls


def _gate(fixture):
    config, uids, nonce, allowed, denied, run, _calls = fixture
    return module.gate(
        config, uids, nonce, allowed, denied, "gateway", "/run/tessera-ca/ca.crt", run=run
    )


def test_gate_brackets_denial_with_https_and_rechecks_identity(tmp_path):
    fixture = _fixture(
        tmp_path,
        (
            {"outcome": "https_ready"},
            {"outcome": "denied_connect", "error_type": "ConnectionRefusedError"},
            {"outcome": "https_ready"},
        ),
    )
    result = _gate(fixture)
    config, _uids, _nonce, allowed, denied, _run, calls = fixture
    assert result["state"] == "network_https_probe_passed_not_dispatch_authority"
    assert result["denied_error_type"] == "ConnectionRefusedError"
    execs = [cmd for cmd in calls if "exec" in cmd]
    assert [cmd[cmd.index("exec") + 1] for cmd in execs] == [allowed, denied, allowed]
    assert all(
        cmd[:5] == ["kubectl", "--kubeconfig", str(config), "--context", "docker-desktop"]
        for cmd in calls
    )
    assert all("delete" not in cmd and "create" not in cmd for cmd in calls)


@pytest.mark.parametrize(
    "denied",
    [
        {"outcome": "connected"},
        {"outcome": "failed", "error_type": "FileNotFoundError"},
        {"outcome": "denied_connect", "error_type": "TimeoutError"},
    ],
)
def test_negative_success_or_probe_failure_blocks(tmp_path, denied):
    fixture = _fixture(tmp_path, ({"outcome": "https_ready"}, denied, {"outcome": "https_ready"}))
    with pytest.raises(module.GateError, match="witness failed"):
        _gate(fixture)


def test_changed_policy_uid_blocks(tmp_path):
    fixture = _fixture(
        tmp_path,
        (
            {"outcome": "https_ready"},
            {"outcome": "denied_connect", "error_type": "ConnectionRefusedError"},
            {"outcome": "https_ready"},
        ),
    )
    config, uids, nonce, allowed, denied, run, calls = fixture
    counter = 0

    def changed(cmd, **kwargs):
        nonlocal counter
        response = run(cmd, **kwargs)
        if "networkpolicy" in cmd and "default-deny" in cmd:
            counter += 1
            obj = json.loads(response.stdout)
            obj["metadata"]["uid"] += str(counter)
            return CompletedProcess(cmd, 0, json.dumps(obj), "")
        return response

    with pytest.raises(module.GateError, match="identities changed"):
        module.gate(
            config, uids, nonce, allowed, denied, "gateway", "/run/tessera-ca/ca.crt", run=changed
        )


def test_leaf_key_mounted_as_ca_is_rejected_before_probing(tmp_path):
    fixture = _fixture(tmp_path, ())
    config, uids, nonce, allowed, denied, run, calls = fixture

    def old_leaf_projection(cmd, **kwargs):
        response = run(cmd, **kwargs)
        if "get" in cmd and cmd[cmd.index("get") + 1 : cmd.index("get") + 3] == ["pod", allowed]:
            obj = json.loads(response.stdout)
            obj["spec"]["volumes"][0]["secret"]["items"][0]["key"] = "tls.crt"
            return CompletedProcess(cmd, 0, json.dumps(obj), "")
        return response

    with pytest.raises(module.GateError, match="expected isolation"):
        module.gate(
            config,
            uids,
            nonce,
            allowed,
            denied,
            "gateway",
            "/run/tessera-ca/ca.crt",
            run=old_leaf_projection,
        )
    assert not any("exec" in cmd for cmd in calls)


def test_refuse_missing_identity_before_any_command(tmp_path):
    config = tmp_path / "config"
    config.write_text("fixture")

    def forbidden(*args, **kwargs):
        pytest.fail("kubectl should not be called")

    with pytest.raises(module.GateError, match="UIDs required"):
        module.gate(
            config,
            {},
            "a" * 32,
            "allowed",
            "denied",
            "gateway",
            "/run/tessera-ca/ca.crt",
            run=forbidden,
        )


@pytest.mark.parametrize(
    "failure", [None, "all_timeout", "grant_timeout", "revoke_connected", "stale_uid"]
)
def test_causal_probe_transitions_and_uid_restoration(tmp_path, failure, monkeypatch):
    if failure in {"grant_timeout", "revoke_connected"}:
        ticks = iter(range(0, 1000, 2))
        monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
        monkeypatch.setattr(module.time, "sleep", lambda _: None)
    baseline = [
        {"outcome": "https_ready"},
        {"outcome": "denied_connect", "error_type": "TimeoutError"},
        {"outcome": "https_ready"},
    ]
    fixture = _fixture(tmp_path, baseline)
    config, uids, nonce, allowed, denied, original, calls = fixture
    grants = {}
    phase = 0

    def run(cmd, **kwargs):
        nonlocal phase
        if "create" in cmd:
            manifest = json.loads(kwargs["input"])
            name = manifest["metadata"]["name"]
            assert manifest["spec"]["policyTypes"] in (["Ingress"], ["Egress"])
            assert manifest["metadata"]["labels"] == {module.OWNER_LABEL: nonce}
            grants[name] = name + "-uid"
            return CompletedProcess(
                cmd, 0, json.dumps({"metadata": {"name": name, "uid": grants[name]}}), ""
            )
        if "delete" in cmd:
            name = cmd[cmd.index("delete") + 1].rsplit("/", 1)[-1]
            assert json.loads(kwargs["input"])["preconditions"]["uid"] == grants[name]
            del grants[name]
            return CompletedProcess(cmd, 0, '{"kind":"Status","status":"Success"}', "")
        if "exec" in cmd:
            pod = cmd[cmd.index("exec") + 1]
            if phase < 3:
                item = baseline[phase]
            elif pod == allowed:
                item = {"outcome": "https_ready"}
            elif len(grants) == 2:
                item = (
                    {"outcome": "denied_connect", "error_type": "TimeoutError"}
                    if failure == "grant_timeout"
                    else {"outcome": "https_ready"}
                )
            else:
                item = (
                    {"outcome": "connected"}
                    if failure == "revoke_connected"
                    else {"outcome": "denied_connect", "error_type": "TimeoutError"}
                )
            if failure == "all_timeout" and pod == allowed:
                item = {"outcome": "failed", "error_type": "TimeoutError"}
            phase += 1
            return CompletedProcess(cmd, 0, json.dumps(item), "")
        if "get" in cmd and "networkpolicy" in cmd and any(n in cmd for n in grants):
            name = cmd[cmd.index("get") + 2]
            uid = grants[name] + ("-changed" if failure == "stale_uid" else "")
            return CompletedProcess(
                cmd,
                0,
                json.dumps({"metadata": {"name": name, "uid": uid, "namespace": module.CONTROL}}),
                "",
            )
        return original(cmd, **kwargs)

    if failure:
        with pytest.raises(module.GateError):
            module.gate(
                config,
                uids,
                nonce,
                allowed,
                denied,
                "gateway",
                "/run/tessera-ca/ca.crt",
                causal=True,
                run=run,
            )
    else:
        result = module.gate(
            config,
            uids,
            nonce,
            allowed,
            denied,
            "gateway",
            "/run/tessera-ca/ca.crt",
            causal=True,
            run=run,
        )
        assert [item["phase"] for item in result["causal_transitions"]] == [
            "grant",
            "revoke",
            "grant",
            "revoke",
        ]
        assert result["causal_transitions"][0]["policy_uids"]
        assert grants == {}
