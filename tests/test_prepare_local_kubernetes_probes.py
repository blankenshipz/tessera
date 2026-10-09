"""Synthetic Kubernetes boundaries; only selected public certificate data is read."""

import base64
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from subprocess import CompletedProcess

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location(
    "prepare_local_kubernetes_probes", SCRIPTS / "prepare_local_kubernetes_probes.py"
)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

IMAGE = "docker.io/library/tessera-local-lab@sha256:" + "a" * 64
UIDS = {module.CONTROL: "control-uid", module.WORKLOAD: "workload-uid"}
NONCE = "b" * 32


def test_create_only_owned_probe_pair_and_public_ca_projection(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.write_text("fixture")
    monkeypatch.setattr(module.secrets, "token_hex", lambda count: "a" * (count * 2))
    commands = []
    manifests = []
    verified = []
    monkeypatch.setattr(module, "verify_public_chain", lambda ca, leaf: verified.append((ca, leaf)))

    def run(cmd, **kwargs):
        commands.append(cmd)
        assert cmd[:5] == ["kubectl", "--kubeconfig", str(config), "--context", "docker-desktop"]
        if "create" in cmd:
            manifest = json.loads(kwargs["input"])
            manifests.append(manifest)
            obj = {"metadata": {"name": manifest["metadata"]["name"], "uid": str(len(manifests))}}
        else:
            kind, name = cmd[cmd.index("get") + 1 : cmd.index("get") + 3]
            if kind == "namespace":
                obj = {
                    "metadata": {
                        "name": name,
                        "uid": UIDS[name],
                        "labels": {module.OWNER_LABEL: NONCE},
                    }
                }
            elif kind == "pod":
                obj = {
                    "metadata": {
                        "name": name,
                        "uid": "gateway-uid",
                        "namespace": module.CONTROL,
                        "labels": {
                            "app.kubernetes.io/name": "tessera-control-tls",
                            "tessera.io/control-hop": "true",
                        },
                    },
                    "status": {"phase": "Running", "podIP": "10.1.0.5"},
                    "spec": {"volumes": [{"secret": {"secretName": "gateway-tls"}}]},
                }
            elif kind == "secret":
                template = cmd[cmd.index("-o") + 1]
                assert "go-template=" in template and "tls.key" not in template
                assert template.endswith("}}")
                key = "ca.crt" if name == "ca-local" else "tls.crt"
                assert f'(index .data "{key}")' in template
                obj = {
                    "name": name,
                    "uid": name + "-uid",
                    "namespace": module.CONTROL,
                    "owner": NONCE if name == "ca-local" else "",
                    "certificate": base64.b64encode(name.encode()).decode(),
                }
            elif kind == "service":
                obj = {
                    "metadata": {"name": name, "uid": "service-uid", "namespace": module.CONTROL},
                    "spec": {
                        "selector": {"app.kubernetes.io/name": "tessera-control-tls"},
                        "ports": [{"port": 443, "protocol": "TCP"}],
                    },
                }
            else:
                obj = {
                    "metadata": {"name": name, "uid": "policy-uid"},
                    "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
                }
        return CompletedProcess(cmd, 0, json.dumps(obj), "")

    result = module.prepare(
        config, UIDS, NONCE, IMAGE, "ca-local", "gateway", "gateway-tls", run=run
    )
    assert result["state"] == "probe_clients_created_not_network_proven"
    assert [manifest["kind"] for manifest in manifests] == [
        "NetworkPolicy",
        "NetworkPolicy",
        "Pod",
        "Pod",
    ]
    assert [
        p["metadata"]["labels"].get("tessera.io/tls-control-client") for p in manifests[2:]
    ] == ["true", None]
    assert all(p["spec"]["containers"][0]["image"] == IMAGE for p in manifests[2:])
    assert verified == [(b"ca-local", b"gateway-tls")]
    assert all(
        p["spec"]["volumes"][0]["secret"]
        == {"secretName": "ca-local", "items": [{"key": "ca.crt", "path": "ca.crt"}]}
        for p in manifests[2:]
    )
    assert [cmd[cmd.index("get") + 2] for cmd in commands if "secret" in cmd] == [
        "ca-local",
        "gateway-tls",
    ]
    assert not any("apply" in cmd or "delete" in cmd for cmd in commands)


def test_refuse_wrong_namespace_before_create(tmp_path):
    config = tmp_path / "config"
    config.write_text("fixture")
    commands = []

    def run(cmd, **kwargs):
        commands.append(cmd)
        name = cmd[cmd.index("get") + 2]
        return CompletedProcess(
            cmd, 0, json.dumps({"metadata": {"name": name, "uid": "wrong"}}), ""
        )

    with pytest.raises(module.ProbeSetupError, match="owned namespace changed"):
        module.prepare(config, UIDS, NONCE, IMAGE, "ca-local", "gateway", "gateway-tls", run=run)
    assert len(commands) == 1 and not any("create" in cmd for cmd in commands)


def test_bad_image_does_not_call_kubernetes(tmp_path):
    config = tmp_path / "config"
    config.write_text("fixture")

    def forbidden(*args, **kwargs):
        pytest.fail("must not contact Kubernetes")

    with pytest.raises(module.ProbeSetupError, match="pinned"):
        module.prepare(
            config, UIDS, NONCE, "lab:latest", "ca-local", "gateway", "gateway-tls", run=forbidden
        )


def test_refuse_leaf_secret_as_ca_before_kubernetes(tmp_path):
    config = tmp_path / "config"
    config.write_text("fixture")
    with pytest.raises(module.ProbeSetupError, match="distinct"):
        module.prepare(
            config,
            UIDS,
            NONCE,
            IMAGE,
            "gateway-tls",
            "gateway",
            "gateway-tls",
            run=lambda *a, **kw: pytest.fail("must not contact Kubernetes"),
        )


def test_refuse_leaf_certificate_as_trust_root():
    with pytest.raises(module.ProbeSetupError, match="distinct"):
        module.verify_public_chain(
            b"public leaf",
            b"public leaf",
            run=lambda *a, **kw: pytest.fail("must not invoke openssl"),
        )


@pytest.mark.parametrize("owner,certificate", [("other", "Y2E="), (NONCE, "")])
def test_missing_or_unowned_ca_blocks_before_create(tmp_path, owner, certificate):
    config = tmp_path / "config"
    config.write_text("fixture")
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        assert "create" not in cmd
        kind, name = cmd[cmd.index("get") + 1 : cmd.index("get") + 3]
        if kind == "namespace":
            obj = {
                "metadata": {"name": name, "uid": UIDS[name], "labels": {module.OWNER_LABEL: NONCE}}
            }
        elif kind == "pod":
            obj = {
                "metadata": {
                    "name": name,
                    "uid": "gateway-uid",
                    "namespace": module.CONTROL,
                    "labels": {
                        "app.kubernetes.io/name": "tessera-control-tls",
                        "tessera.io/control-hop": "true",
                    },
                },
                "status": {"phase": "Running", "podIP": "10.1.0.5"},
                "spec": {"volumes": [{"secret": {"secretName": "gateway-tls"}}]},
            }
        elif kind == "service":
            obj = {
                "metadata": {"name": name, "uid": "service-uid", "namespace": module.CONTROL},
                "spec": {
                    "selector": {"app.kubernetes.io/name": "tessera-control-tls"},
                    "ports": [{"port": 443}],
                },
            }
        else:
            assert kind == "secret" and name in {"ca-local", "gateway-tls"}
            obj = {
                "name": name,
                "uid": name + "-uid",
                "namespace": module.CONTROL,
                "owner": owner if name == "ca-local" else "",
                "certificate": certificate if name == "ca-local" else "bGVhZg==",
            }
        return CompletedProcess(cmd, 0, json.dumps(obj), "")

    with pytest.raises(module.ProbeSetupError, match="ownership mismatch|certificates required"):
        module.prepare(config, UIDS, NONCE, IMAGE, "ca-local", "gateway", "gateway-tls", run=run)
    assert not any("create" in cmd for cmd in calls)


def test_public_chain_and_hostname_offline(tmp_path):
    ca, key, csr, leaf = (tmp_path / part for part in ("ca.pem", "ca.key", "leaf.csr", "leaf.pem"))
    leaf_key = tmp_path / "leaf.key"
    extensions = tmp_path / "leaf.ext"
    extensions.write_text(
        "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        f"subjectAltName=DNS:{module.GATEWAY_HOST}\n"
    )
    for command in (
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(ca),
            "-days",
            "1",
            "-subj",
            "/CN=Fixture CA",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
        ],
        [
            "openssl",
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(leaf_key),
            "-out",
            str(csr),
            "-subj",
            f"/CN={module.GATEWAY_HOST}",
        ],
        [
            "openssl",
            "x509",
            "-req",
            "-in",
            str(csr),
            "-CA",
            str(ca),
            "-CAkey",
            str(key),
            "-CAcreateserial",
            "-out",
            str(leaf),
            "-days",
            "1",
            "-extfile",
            str(extensions),
        ],
    ):
        subprocess.run(command, check=True, capture_output=True, timeout=12)
    module.verify_public_chain(ca.read_bytes(), leaf.read_bytes())
    with pytest.raises(module.ProbeSetupError, match="not an issuing CA"):
        module.verify_public_chain(leaf.read_bytes(), ca.read_bytes())
    other_ca = tmp_path / "other.pem"
    other_key = tmp_path / "other.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(other_key),
            "-out",
            str(other_ca),
            "-days",
            "1",
            "-subj",
            "/CN=Other CA",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
        ],
        check=True,
        capture_output=True,
        timeout=12,
    )
    with pytest.raises(module.ProbeSetupError, match="validation failed"):
        module.verify_public_chain(other_ca.read_bytes(), leaf.read_bytes())
    extensions.write_text(
        "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:wrong.example\n"
    )
    subprocess.run(
        [
            "openssl",
            "x509",
            "-req",
            "-in",
            str(csr),
            "-CA",
            str(ca),
            "-CAkey",
            str(key),
            "-out",
            str(leaf),
            "-days",
            "1",
            "-extfile",
            str(extensions),
        ],
        check=True,
        capture_output=True,
        timeout=12,
    )
    with pytest.raises(module.ProbeSetupError, match="subject alternative name missing"):
        module.verify_public_chain(ca.read_bytes(), leaf.read_bytes())
