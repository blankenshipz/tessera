"""Deployment-boundary checks; no cluster or provider effects."""

from pathlib import Path

import pytest
import yaml

from tessera_control.deployment import ControlClient


def test_coordinator_requires_tls_ca_and_rejects_ambiguous_origins(tmp_path: Path) -> None:
    ca = tmp_path / "missing-ca.crt"
    for origin in (
        "http://control.example.test",
        "https://control.example.test/extra",
        "https://user:password@control.example.test",
        "https://control.example.test?x=1",
        "https://control.example.test#fragment",
        "https://control.example.test:",
    ):
        with pytest.raises(ValueError):
            ControlClient(origin, ca)
    with pytest.raises(OSError):
        ControlClient("https://control.example.test", ca)


def test_opt_in_manifest_has_external_authority_prerequisites_and_no_provider() -> None:
    path = (
        Path(__file__).parents[1] / "deploy/kubernetes/provider-disabled-coordinator.example.yaml"
    )
    deployment, policy = yaml.safe_load_all(path.read_text())
    pod = deployment["spec"]["template"]
    assert pod["metadata"]["labels"]["tessera.io/tls-control-client"] == "true"
    assert "tessera.io/control-hop" not in pod["metadata"]["labels"]
    assert pod["spec"]["automountServiceAccountToken"] is False
    assert pod["spec"]["securityContext"]["runAsNonRoot"] is True
    container = pod["spec"]["containers"][0]
    assert container["command"] == [
        "python",
        "-c",
        "from tessera_control.deployment import main; main()",
    ]
    assert "https://tessera-control-tls.tessera-lab.svc.cluster.local" in container["args"]
    assert "--max-cycles" not in container["args"]
    assert pod["spec"]["volumes"][0]["secret"]["secretName"] == (
        "tessera-existing-coordinator-credential"
    )
    assert policy["spec"]["egress"][0]["ports"] == [{"protocol": "TCP", "port": 443}]


def test_credential_pods_have_gateway_egress_without_plaintext_control_route() -> None:
    root = Path(__file__).parents[1] / "deploy/kubernetes"
    profile = list(yaml.safe_load_all((root / "lab-control-plane.yaml").read_text()))
    policies = [item["spec"] for item in profile if item["kind"] == "NetworkPolicy"]
    for name in ("provider-disabled-coordinator", "remote-dispatch-worker"):
        documents = list(yaml.safe_load_all((root / f"{name}.example.yaml").read_text()))
        deployment = next(item for item in documents if item["kind"] == "Deployment")
        labels = deployment["spec"]["template"]["metadata"]["labels"]
        assert labels["tessera.io/tls-control-client"] == "true"
        assert labels.get("tessera.io/control-hop") != "true"
        policies_for_pod = [
            policy
            for policy in policies
            if all(
                labels.get(key) == value
                for key, value in policy["podSelector"].get("matchLabels", {}).items()
            )
        ]
        assert not any(
            port["port"] == 8765
            for policy in policies_for_pod
            for rule in policy.get("egress", [])
            for port in rule.get("ports", [])
        )
        own_policies = [item["spec"] for item in documents if item["kind"] == "NetworkPolicy"]
        assert all(
            port["port"] != 8765
            for policy in own_policies
            for rule in policy.get("egress", [])
            for port in rule.get("ports", [])
        )
        assert any(
            rule["to"]
            == [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "tessera-control-tls"}}}]
            and rule["ports"] == [{"protocol": "TCP", "port": 443}]
            for policy in documents
            if policy["kind"] == "NetworkPolicy"
            for rule in policy["spec"].get("egress", [])
        )
