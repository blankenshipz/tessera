"""Worker-side egress objects for the Agent Sandbox (ADR 0034 phase 2). Synthetic only."""

import base64
import json
from copy import deepcopy

import pytest
from test_agent_sandbox import FakeCluster, approved_plan, resource

from tessera_control.agent_sandbox_models import SandboxPlan
from tessera_control.store import canonical_digest
from tessera_worker import egress_proxy
from tessera_worker.agent_sandbox import (
    EGRESS_PROFILE_VERSION,
    EGRESS_PROXY_PORT,
    AgentSandboxBackend,
    SandboxUnsafe,
    egress_manifests,
    envelope,
    manifest,
)

OP = "ab" * 16
IMAGE = "registry.example/proxy@sha256:" + "e" * 64
LABELS = {"tessera.dev/operation": OP}


def package(egress=False):
    plan, witness = approved_plan()
    dump = plan.model_dump()
    if egress:
        dump["target"]["egress_proxy_image"] = IMAGE
    plan = SandboxPlan.model_validate(dump)
    value = {
        "operation_id": OP,
        "lease_id": "lease",
        "epoch": 1,
        "sandbox_expires_at": "2030-01-01T00:00:00Z",
        "pinned_image": "img@sha256:" + "f" * 64,
        "output_paths": ["out.txt"],
        "plan": {"max_duration_seconds": 60, "execution_spec": {"argv": ["run"]}},
        "sandbox": plan.model_dump(),
        "profile_version": "agent-sandbox-network-none-v1",
    }
    if egress:
        value |= {
            "profile_version": EGRESS_PROFILE_VERSION,
            "network_profile": "allowlist-proxy-v1",
            "egress": {
                "profile": "allowlist-proxy-v1",
                "destinations": [
                    {"host": "b.example", "port": 8443},
                    {"host": "a.example", "port": 443},
                    {"host": "c.example", "port": 443},
                ],
                "max_egress_bytes": 1000,
            },
        }
    return value, plan, witness


def test_network_none_is_unchanged():
    value, _, _ = package()
    env = envelope(value)
    assert env.target.profile_version == "agent-sandbox-network-none-v1"
    container = manifest(value)["spec"]["podTemplate"]["spec"]["containers"][0]
    assert "env" not in container
    assert set(container) == {
        "name",
        "image",
        "imagePullPolicy",
        "command",
        "workingDir",
        "resources",
        "securityContext",
        "volumeMounts",
    }
    with pytest.raises(SandboxUnsafe):
        egress_manifests(value)


def test_egress_envelope_and_env():
    value, _, _ = package(True)
    assert envelope(value).target.profile_version == EGRESS_PROFILE_VERSION
    spec = manifest(value)["spec"]["podTemplate"]["spec"]
    proxy = f"http://tessera-egress-{OP}:3128"
    assert spec["containers"][0]["env"] == [
        {"name": "HTTPS_PROXY", "value": proxy},
        {"name": "HTTP_PROXY", "value": proxy},
        {"name": "NO_PROXY", "value": ""},
    ]
    clean, _, _ = package()
    other = manifest(clean)["spec"]["podTemplate"]["spec"]
    del spec["containers"][0]["env"]
    assert spec == other


def test_egress_manifests_exact_and_deterministic():
    value, _, _ = package(True)
    first = egress_manifests(value)
    assert first == egress_manifests(deepcopy(value))
    assert set(first) == {"sandbox_policy", "proxy_policy", "service", "proxy_pod"}
    digest = canonical_digest(value)
    for item in first.values():
        assert item["metadata"]["labels"]["tessera.dev/egress-operation"] == OP
        assert item["metadata"]["annotations"] == {"tessera.dev/package": digest}
    dns = {
        "to": [
            {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                },
                "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
            }
        ],
        "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
    }
    proxy_sel = {"matchLabels": {"tessera.dev/egress-proxy": OP}}
    sandbox_sel = {"matchLabels": LABELS}
    sp = first["sandbox_policy"]
    assert sp["metadata"]["name"] == "tessera-egress-" + OP
    assert sp["spec"] == {
        "podSelector": sandbox_sel,
        "policyTypes": ["Egress"],
        "egress": [
            {"to": [{"podSelector": proxy_sel}], "ports": [{"protocol": "TCP", "port": 3128}]},
            dns,
        ],
    }
    pp = first["proxy_policy"]
    assert pp["metadata"]["name"] == "tessera-egress-proxy-" + OP
    assert pp["spec"]["podSelector"] == proxy_sel
    assert pp["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert pp["spec"]["ingress"] == [
        {"from": [{"podSelector": sandbox_sel}], "ports": [{"protocol": "TCP", "port": 3128}]}
    ]
    assert pp["spec"]["egress"] == [
        {
            "to": [
                {
                    "ipBlock": {
                        "cidr": "0.0.0.0/0",
                        "except": [
                            "10.0.0.0/8",
                            "172.16.0.0/12",
                            "192.168.0.0/16",
                            "100.64.0.0/10",
                            "169.254.0.0/16",
                            "127.0.0.0/8",
                            "0.0.0.0/8",
                        ],
                    }
                },
                {"ipBlock": {"cidr": "::/0", "except": ["fc00::/7", "fe80::/10", "::1/128"]}},
            ],
            "ports": [{"protocol": "TCP", "port": 443}, {"protocol": "TCP", "port": 8443}],
        },
        dns,
    ]
    svc = first["service"]
    assert svc["metadata"]["name"] == "tessera-egress-" + OP
    assert svc["spec"] == {
        "type": "ClusterIP",
        "selector": {"tessera.dev/egress-proxy": OP},
        "ports": [{"protocol": "TCP", "port": 3128, "targetPort": 3128}],
    }
    pod = first["proxy_pod"]
    assert pod["metadata"]["name"] == "tessera-egress-proxy-" + OP
    assert pod["metadata"]["labels"] == {
        "tessera.dev/egress-proxy": OP,
        "tessera.dev/egress-operation": OP,
    }
    spec = pod["spec"]
    assert spec["runtimeClassName"] == "gvisor"
    assert spec["activeDeadlineSeconds"] == 60
    assert spec["automountServiceAccountToken"] is False
    assert spec["enableServiceLinks"] is False
    assert not (spec["hostNetwork"] or spec["hostPID"] or spec["hostIPC"])
    assert spec["restartPolicy"] == "Never"
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["securityContext"]["runAsUser"] == 65532
    (container,) = spec["containers"]
    assert container["name"] == "proxy" and container["image"] == IMAGE
    assert container["ports"] == [{"containerPort": EGRESS_PROXY_PORT}]
    assert container["securityContext"] == {
        "readOnlyRootFilesystem": True,
        "allowPrivilegeEscalation": False,
        "privileged": False,
        "capabilities": {"drop": ["ALL"]},
    }
    assert container["resources"]["requests"] == container["resources"]["limits"]
    command = container["command"]
    assert command[:3] == ["python", "-m", "tessera_worker.egress_proxy"]
    assert command[3] == "--config-b64"
    assert command[5:] == ["--journal", "-", "--listen", "0.0.0.0:3128"]
    config = json.loads(base64.b64decode(command[4]))
    assert config == {
        "destinations": value["egress"]["destinations"],
        "max_egress_bytes": 1000,
    }
    assert (
        base64.b64decode(command[4])
        == json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    )


def backend(policies, plan, witness):
    cluster = FakeCluster(plan)
    cluster.policies = policies
    cluster.backend = AgentSandboxBackend(cluster, lambda target: deepcopy(witness))
    return cluster


def policy(name, uid, spec):
    return resource("NetworkPolicy", name, uid, spec)


DENY = policy(
    "deny-all",
    "p1",
    {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
)


def test_preflight_requires_exact_expected_policy():
    value, plan, witness = package(True)
    wanted = egress_manifests(value)["sandbox_policy"]
    live = policy(wanted["metadata"]["name"], "p2", deepcopy(wanted["spec"]))
    ok = backend([DENY, live], plan, witness)
    ok.backend.preflight(plan.target, LABELS, wanted)

    missing = backend([DENY], plan, witness)
    with pytest.raises(SandboxUnsafe, match="missing"):
        missing.backend.preflight(plan.target, LABELS, wanted)

    drift = deepcopy(live)
    drift["spec"]["egress"][0]["ports"][0]["port"] = 9999
    with pytest.raises(SandboxUnsafe, match="drift"):
        backend([DENY, drift], plan, witness).backend.preflight(plan.target, LABELS, wanted)

    allow_all = policy(
        "allow-all", "p3", {"podSelector": {}, "policyTypes": ["Egress"], "egress": [{}]}
    )
    with pytest.raises(SandboxUnsafe, match="additive"):
        backend([DENY, live, allow_all], plan, witness).backend.preflight(
            plan.target, LABELS, wanted
        )

    # The run policy alone does not provide ingress default-deny.
    with pytest.raises(SandboxUnsafe, match="default-deny"):
        backend([live], plan, witness).backend.preflight(plan.target, LABELS, wanted)


def test_preflight_none_is_unchanged():
    value, plan, witness = package(True)
    wanted = egress_manifests(value)["sandbox_policy"]
    live = policy(wanted["metadata"]["name"], "p2", deepcopy(wanted["spec"]))
    backend([DENY], plan, witness).backend.preflight(plan.target, LABELS)
    with pytest.raises(SandboxUnsafe, match="additive"):
        backend([DENY, live], plan, witness).backend.preflight(plan.target, LABELS)


def test_paths_service_pod_policy():
    _, plan, witness = package(True)
    b = backend([], plan, witness).backend
    ns = plan.target.namespace
    assert b.path(plan.target, "service", "s") == f"/api/v1/namespaces/{ns}/services/s"
    assert b.path(plan.target, "pod", "p") == f"/api/v1/namespaces/{ns}/pods/p"
    assert b.path(plan.target, "policy", "n").endswith("/networkpolicies/n")


class RawClient:
    def __init__(self, cluster, data):
        self.cluster, self.data, self.paths = cluster, data, []

    def request_raw(self, path, max_bytes):
        self.paths.append((path, max_bytes))
        return self.data


def test_proxy_log_raw_read_and_bound():
    _, plan, witness = package(True)
    cluster = backend([], plan, witness)
    log = b'{"decision":"allow"}\n'
    client = RawClient(cluster.cluster, log)
    b = AgentSandboxBackend(client, None)
    assert b.proxy_log(plan.target, OP) == log
    path, bound = client.paths[0]
    assert path == (
        f"/api/v1/namespaces/{plan.target.namespace}/pods/tessera-egress-proxy-{OP}"
        f"/log?container=proxy&limitBytes={2 * 1024 * 1024}"
    )
    assert bound == 2 * 1024 * 1024
    client.data = b"x" * 100
    with pytest.raises(SandboxUnsafe, match="bound"):
        b.proxy_log(plan.target, OP, max_bytes=100)
    client.data = None
    with pytest.raises(SandboxUnsafe):
        b.proxy_log(plan.target, OP)


def test_egress_proxy_config_b64_and_stdout_journal(capsys):
    config = {"destinations": [{"host": "a.example", "port": 443}], "max_egress_bytes": 5}
    encoded = base64.b64encode(json.dumps(config).encode()).decode()
    assert egress_proxy.load_config_b64(encoded) == ([("a.example", 443)], 5)
    journal = egress_proxy.EgressJournal("-")
    journal.record("a.example", 443, "allow", "closed", 3, 4, 1)
    journal.record("evil.example", 443, "deny", "not_allowlisted")
    journal.close()
    out = capsys.readouterr().out
    assert out.count("\n") == 2
    summary = egress_proxy.summarize_journal_bytes(out.encode() + b"garbage\n")
    assert summary["connections_allowed"] == 1 and summary["connections_denied"] == 1
    assert summary["bytes_up"] == 3 and summary["bytes_down"] == 4
    assert {d["host"] for d in summary["destinations"]} == {"a.example", "evil.example"}
    assert egress_proxy.journal_bytes_sha256(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_egress_proxy_cli_requires_one_config(monkeypatch):
    monkeypatch.setattr("sys.argv", ["egress_proxy", "--journal", "-"])
    with pytest.raises(SystemExit):
        egress_proxy.main()


def test_egress_predicate_is_shared_and_fails_closed_on_one_marker() -> None:
    import pytest

    from tessera_control.agent_sandbox import _egress
    from tessera_control.store import Conflict
    from tessera_worker.agent_sandbox import EGRESS_PROFILE_VERSION, is_egress_package

    assert is_egress_package({}) is False
    assert _egress({}) is None
    both = {"profile_version": EGRESS_PROFILE_VERSION, "network_profile": "allowlist-proxy-v1"}
    assert is_egress_package(both) is True
    for half in (
        {"profile_version": EGRESS_PROFILE_VERSION},
        {"network_profile": "allowlist-proxy-v1"},
    ):
        with pytest.raises(ValueError):
            is_egress_package(half)
        with pytest.raises(Conflict):
            _egress(half)
