"""Isolated, no-secret checks for credential-free PVC binder and node admission."""

import base64
import json

import pytest
from test_legacy_operator_isolation import legacy_io_guard as legacy_io_guard

from tessera_operator import kubernetes as operator
from tessera_operator import node_transfer
from tessera_operator import private_import as importer
from tessera_operator import upgrade_support as support

IMAGE = "docker.io/library/tessera-local-persistent@sha256:" + "a" * 64
VOLUME = "pvc-" + "a" * 8 + "-" + "b" * 4 + "-" + "c" * 4 + "-" + "d" * 4 + "-" + "e" * 12


def fixture_target(tmp_path, monkeypatch):
    receipt = tmp_path / "operator.json"
    receipt.write_text(
        json.dumps(
            {
                "namespace_uid": "ns-uid",
                "nonce": "nonce",
                "created": [
                    {"kind": kind, "name": name, "uid": kind.lower()}
                    for kind, name in importer.EXPECTED
                ],
            }
        )
    )
    receipt.chmod(0o600)
    monkeypatch.setattr(operator, "RESOURCE_RECEIPT", receipt)
    monkeypatch.setattr(importer, "RECEIPT", tmp_path / "binder.json")
    monkeypatch.setattr(importer, "RELEASE", tmp_path / "release.json")
    state = {"phase": "Pending", "path": "expected", "calls": []}

    def kubectl(*args, **kwargs):
        state["calls"].append(args)
        if args[0] == "get" and args[1] == "namespace":
            return {
                "metadata": {
                    "uid": "ns-uid",
                    "labels": {"tessera.io/local-operator-nonce": "nonce"},
                }
            }
        if args[0] == "get" and args[1] == "pv":
            path = (
                f"/var/local-path-provisioner/{VOLUME}_{operator.NAMESPACE}_tessera-control-state"
            )
            if state["path"] != "expected":
                path = "/var/local-path-provisioner/foreign"
            return {
                "metadata": {"name": VOLUME, "uid": "pv-uid"},
                "spec": {
                    "claimRef": {
                        "uid": "persistentvolumeclaim",
                        "namespace": operator.NAMESPACE,
                        "name": "tessera-control-state",
                    },
                    "storageClassName": "standard",
                    "persistentVolumeReclaimPolicy": "Delete",
                    "hostPath": {"path": path},
                    "nodeAffinity": {
                        "required": {
                            "nodeSelectorTerms": [
                                {
                                    "matchExpressions": [
                                        {
                                            "key": "kubernetes.io/hostname",
                                            "operator": "In",
                                            "values": ["desktop-control-plane"],
                                        }
                                    ]
                                }
                            ]
                        }
                    },
                },
            }
        if args[0] == "-n" and args[2] == "create":
            assert kwargs["data"]["kind"] == "Pod"
            assert kwargs["data"]["spec"]["automountServiceAccountToken"] is False
            assert "owner.header" not in repr(kwargs["data"])
            return {"metadata": {"uid": "binder-uid"}}
        if args[0] != "-n":
            raise AssertionError(args)
        kind = args[3]
        if kind == "pvc":
            return {
                "metadata": {"uid": "persistentvolumeclaim"},
                "spec": {"storageClassName": "standard", "volumeName": VOLUME},
                "status": {"phase": state["phase"]},
            }
        if kind == "deployment":
            return {
                "metadata": {"uid": "deployment"},
                "spec": {"replicas": 0, "template": {"spec": {"containers": [{"image": IMAGE}]}}},
            }
        if kind == "networkpolicy":
            return {"metadata": {"uid": "networkpolicy"}}
        if kind == "pods":
            return {"items": []}
        if kind == "pod":
            return {
                "metadata": {
                    "uid": "binder-uid",
                    "labels": {"tessera.io/local-operator-nonce": "nonce"},
                }
            }
        raise AssertionError(args)

    monkeypatch.setattr(operator, "_kubectl", kubectl)
    monkeypatch.setattr(operator, "_docker_identity", lambda: "desktop-unix")
    return state


def test_credential_free_bind_and_exact_pv_metadata(tmp_path, monkeypatch):
    state = fixture_target(tmp_path, monkeypatch)
    result = importer.binder_create()
    assert result["binder_pod_uid"] == "binder-uid"
    assert len([call for call in state["calls"] if "create" in call]) == 1
    with pytest.raises(operator.AdmissionError, match="prior binder attempt"):
        importer.binder_create()
    state["phase"] = "Bound"
    assert importer.pv_inspect()["pv_uid"] == "pv-uid"
    state["path"] = "foreign"
    with pytest.raises(operator.AdmissionError, match="host path"):
        importer.pv_inspect()


def test_binder_denies_foreign_namespace_without_create(tmp_path, monkeypatch):
    state = fixture_target(tmp_path, monkeypatch)
    original = operator._kubectl

    def drifted(*args, **kwargs):
        if args[:2] == ("get", "namespace"):
            return {"metadata": {"uid": "foreign"}}
        return original(*args, **kwargs)

    monkeypatch.setattr(operator, "_kubectl", drifted)
    with pytest.raises(operator.AdmissionError, match="namespace identity"):
        importer.binder_create()
    assert not any("create" in call for call in state["calls"])


def test_uid_preconditioned_release_and_replacement_denial(tmp_path, monkeypatch):
    state = fixture_target(tmp_path, monkeypatch)
    importer.binder_create()
    state["phase"] = "Bound"
    original = operator._kubectl
    calls = []

    def kubectl(*args, **kwargs):
        if args[3:4] == ("pod",):
            result = original(*args, **kwargs)
            result["metadata"]["resourceVersion"] = "rv-15"
            return result
        if args[3:4] == ("pods",) and calls:
            return {"items": []}
        return original(*args, **kwargs)

    monkeypatch.setattr(operator, "_kubectl", kubectl)
    monkeypatch.setattr(importer, "_delete_binder", lambda uid, rv: calls.append((uid, rv)))
    assert importer.binder_release()["state"] == "binder_absent_no_pods; no_credential_import"
    assert calls == [("binder-uid", "rv-15")]
    with pytest.raises(operator.AdmissionError, match="prior binder release"):
        importer.binder_release()


def test_release_refuses_uid_race_before_delete(tmp_path, monkeypatch):
    state = fixture_target(tmp_path, monkeypatch)
    importer.binder_create()
    state["phase"] = "Bound"
    original = operator._kubectl

    def kubectl(*args, **kwargs):
        value = original(*args, **kwargs)
        if args[3:4] == ("pod",):
            value["metadata"]["resourceVersion"] = "rv-15"
            state["pod_reads"] = state.get("pod_reads", 0) + 1
            if state["pod_reads"] == 2:
                value["metadata"]["uid"] = "replacement"
        return value

    monkeypatch.setattr(operator, "_kubectl", kubectl)
    monkeypatch.setattr(importer, "_delete_binder", lambda *_: pytest.fail("unexpected delete"))
    with pytest.raises(operator.AdmissionError, match="binder resource version or UID"):
        importer.binder_release()


def test_node_path_rejects_hostpath_escape(tmp_path, monkeypatch):
    state = fixture_target(tmp_path, monkeypatch)
    importer.binder_create()
    state["phase"] = "Bound"
    record = importer.RELEASE
    record.write_text(json.dumps({"pv_uid": "pv-uid"}))
    record.chmod(0o600)
    state["path"] = "foreign"
    with pytest.raises(operator.AdmissionError, match="host path"):
        node_transfer._fixed_path()


def test_private_import_retry_and_partial_stream_hide_owner(tmp_path, monkeypatch):
    root = "/var/local-path-provisioner/pvc-owned"
    mapping = {"namespace_uid": "ns-uid", "pvc_uid": "claim", "pv_uid": "pv"}
    monkeypatch.setattr(node_transfer, "RECEIPT", tmp_path / "intent.json")
    monkeypatch.setattr(node_transfer, "COMPLETE", tmp_path / "complete.json")
    monkeypatch.setattr(node_transfer, "_fixed_path", lambda: (mapping, root))
    marker = tmp_path / ".authority-moved"
    marker.write_text(
        json.dumps(
            {
                "namespace_uid": "ns-uid",
                "names": ["tessera.sqlite3", "owner.header", "project-artifacts"],
            }
        )
    )
    marker.chmod(0o600)
    monkeypatch.setattr(operator, "FENCE_MARKER", marker)
    monkeypatch.setattr(operator, "QUARANTINE", tmp_path / "quarantine")
    monkeypatch.setattr(operator, "FENCED_BACKUP", tmp_path / "backup")
    monkeypatch.setattr(operator, "SOURCE", tmp_path)
    (tmp_path / "quarantine").mkdir()
    (tmp_path / "quarantine" / "owner.header").write_text("SYNTHETIC_SECRET_NEVER_OUTPUT")
    (tmp_path / "quarantine" / "owner.header").chmod(0o600)
    (tmp_path / "backup").mkdir()
    monkeypatch.setattr(operator, "_source_listener_absent", lambda: None)
    monkeypatch.setattr(operator, "validate_backup", lambda _: {"snapshot_sha256": "a" * 64})
    monkeypatch.setattr(node_transfer, "_read_manifest", lambda _: {"size": 1, "objects": []})
    monkeypatch.setattr(
        node_transfer,
        "_node",
        lambda *args: root.encode() if args[0] == "readlink" else b"directory:0:0",
    )

    class Status:
        returncode = 1

    monkeypatch.setattr(node_transfer.subprocess, "run", lambda *_a, **_k: Status())

    class Broken:
        stdin = object()

        def poll(self):
            return 1

    monkeypatch.setattr(node_transfer.subprocess, "Popen", lambda *_a, **_k: Broken())
    monkeypatch.setattr(
        node_transfer,
        "_archive",
        lambda *_a: (_ for _ in ()).throw(BrokenPipeError("SYNTHETIC_SECRET_NEVER_OUTPUT")),
    )
    with pytest.raises(operator.AdmissionError, match="private node import failed") as error:
        node_transfer.import_private()
    assert "SYNTHETIC_SECRET" not in str(error.value)
    assert node_transfer.RECEIPT.exists()
    with pytest.raises(operator.AdmissionError, match="prior node import"):
        node_transfer.import_private()


def test_private_file_symlink_and_mode_denied(tmp_path):
    secret = tmp_path / "owner"
    secret.write_text("SYNTHETIC_SECRET_NEVER_OUTPUT")
    secret.chmod(0o644)
    with pytest.raises(operator.AdmissionError, match="source file unsafe"):
        node_transfer._regular(secret, 8192)
    secret.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(secret)
    with pytest.raises(operator.AdmissionError, match="source path unsafe"):
        node_transfer._regular(link, 8192)


def test_delete_posts_uid_and_resource_version_over_verified_local_api(
    tmp_path, monkeypatch, legacy_io_guard
):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text(
        json.dumps(
            {
                "contexts": [
                    {"name": operator.CONTEXT, "context": {"user": "local", "cluster": "local"}}
                ],
                "clusters": [
                    {
                        "name": "local",
                        "cluster": {
                            "server": "https://127.0.0.1:6443",
                            "certificate-authority-data": base64.b64encode(b"CA").decode(),
                        },
                    }
                ],
                "users": [
                    {
                        "name": "local",
                        "user": {
                            "client-certificate-data": base64.b64encode(b"CERT").decode(),
                            "client-key-data": base64.b64encode(b"KEY").decode(),
                        },
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(support, "KUBECONFIG", kubeconfig)
    monkeypatch.setattr(support, "SOURCE", tmp_path)
    (tmp_path / "private").mkdir()
    monkeypatch.setattr(
        support, "_kubeconfig_identity", lambda: {"server": "https://127.0.0.1:6443"}
    )

    class TLS:
        def load_cert_chain(self, cert, key):
            assert (tmp_path / "private") in __import__("pathlib").Path(cert).parents
            assert __import__("pathlib").Path(key).stat().st_mode & 0o077 == 0

    monkeypatch.setattr(support.ssl, "create_default_context", lambda **kwargs: TLS())
    requests = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            assert kwargs["follow_redirects"] is False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def request(self, method, url, **kwargs):
            requests.append((method, url, kwargs["json"]))
            return type("Response", (), {"status_code": 200})()

    monkeypatch.setattr(support.httpx, "Client", Client)
    importer._delete_binder("uid-123", "rv-456")
    assert legacy_io_guard == []
    assert requests == [
        (
            "DELETE",
            f"https://127.0.0.1:6443/api/v1/namespaces/{operator.NAMESPACE}/pods/{importer.BINDER}",
            {
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "preconditions": {"uid": "uid-123", "resourceVersion": "rv-456"},
            },
        )
    ]
