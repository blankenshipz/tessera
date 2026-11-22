"""Synthetic-only activation/route authorization boundary tests."""

import json
import socket

import pytest

from tessera_operator import activation, private_import
from tessera_operator import kubernetes as operator


def test_conditional_activate_writes_intent_before_patch(tmp_path, monkeypatch):
    marker = tmp_path / "activation.json"
    monkeypatch.setattr(activation, "INTENT", marker)
    image = "docker.io/library/tessera-local-persistent@sha256:" + "a" * 64
    monkeypatch.setattr(
        activation,
        "_activation_identity",
        lambda: (
            {"namespace_uid": "ns", "pvc_uid": "pvc", "pv_uid": "pv"},
            {"metadata": {"uid": "deployment", "resourceVersion": "rv-1"}},
            image,
        ),
    )
    seen = []

    def request(method, uri, patch, *, content_type):
        assert marker.exists()
        seen.append((method, uri, patch, content_type))
        return 200

    monkeypatch.setattr(private_import, "_api_request", request)
    assert activation.activate()["state"] == "activation_patch_submitted; readiness_not_proven"
    assert seen[0][0] == "PATCH" and seen[0][1].startswith(
        "/apis/apps/v1/namespaces/tessera-local-persistent/"
    )
    assert seen[0][3] == "application/json-patch+json"
    assert [(step["path"], step["value"]) for step in seen[0][2]] == [
        ("/metadata/uid", "deployment"),
        ("/metadata/resourceVersion", "rv-1"),
        ("/spec/replicas", 0),
        ("/spec/template/spec/containers/0/image", image),
        ("/spec/replicas", 1),
    ]
    with pytest.raises(operator.AdmissionError, match="prior activation"):
        activation.activate()
    assert len(seen) == 1


def test_partial_activation_retained_and_never_retries(tmp_path, monkeypatch):
    marker = tmp_path / "activation.json"
    monkeypatch.setattr(activation, "INTENT", marker)
    monkeypatch.setattr(
        activation,
        "_activation_identity",
        lambda: (
            {"namespace_uid": "ns", "pvc_uid": "pvc", "pv_uid": "pv"},
            {"metadata": {"uid": "deployment", "resourceVersion": "rv-1"}},
            "image",
        ),
    )
    attempts = []
    monkeypatch.setattr(private_import, "_api_request", lambda *_a, **_k: attempts.append(1) or 409)
    with pytest.raises(operator.AdmissionError, match="conditional activation failed"):
        activation.activate()
    assert marker.exists()
    with pytest.raises(operator.AdmissionError, match="prior activation"):
        activation.activate()
    assert len(attempts) == 1


def test_source_revived_denies_activation_before_patch(tmp_path, monkeypatch):
    marker = tmp_path / ".authority-moved"
    marker.write_text(json.dumps({"namespace_uid": "ns"}))
    marker.chmod(0o600)
    monkeypatch.setattr(operator, "FENCE_MARKER", marker)
    monkeypatch.setattr(operator, "SOURCE", tmp_path)
    monkeypatch.setattr(operator, "QUARANTINE", tmp_path / "quarantine")
    monkeypatch.setattr(operator, "FENCED_BACKUP", tmp_path / "backup")
    (tmp_path / "owner.header").write_text("SYNTHETIC_PRIVATE")
    monkeypatch.setattr(operator, "_source_listener_absent", lambda: None)
    monkeypatch.setattr(
        private_import,
        "_owned",
        lambda: (
            {"namespace_uid": "ns"},
            {"metadata": {"uid": "pvc"}},
            {
                "metadata": {"uid": "deployment", "resourceVersion": "rv"},
                "spec": {"template": {"spec": {"containers": [{"image": "immutable"}]}}},
            },
        ),
    )
    monkeypatch.setattr(
        activation.transfer,
        "_fixed_path",
        lambda: ({"namespace_uid": "ns", "pvc_uid": "pvc", "pv_uid": "pv"}, "/fixed"),
    )
    monkeypatch.setattr(activation.transfer, "RECEIPT", tmp_path / "import.json")
    monkeypatch.setattr(activation.transfer, "COMPLETE", tmp_path / "complete.json")
    for path, data in (
        (
            activation.transfer.RECEIPT,
            {"namespace_uid": "ns", "pvc_uid": "pvc", "pv_uid": "pv", "snapshot_sha256": "a"},
        ),
        (
            activation.transfer.COMPLETE,
            {
                "namespace_uid": "ns",
                "pvc_uid": "pvc",
                "pv_uid": "pv",
                "snapshot_sha256": "a",
                "owner_sha256": "b",
            },
        ),
    ):
        path.write_text(json.dumps(data))
        path.chmod(0o600)
    with pytest.raises(operator.AdmissionError, match="source authority revived"):
        activation._activation_identity()
    assert "SYNTHETIC_PRIVATE" not in str(activation.INTENT)


def test_route_denies_occupied_loopback_port_before_intent(tmp_path, monkeypatch):
    monkeypatch.setattr(activation, "ROUTE", tmp_path / "route.json")
    monkeypatch.setattr(activation, "ready_pod", lambda: {"pod_uid": "owned", "pod_name": "owned"})
    monkeypatch.setattr(operator, "_kubeconfig_identity", lambda: {})
    monkeypatch.setattr(operator, "_docker_identity", lambda: "unix")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        monkeypatch.setattr(activation, "PORT", listener.getsockname()[1])
        with pytest.raises(operator.AdmissionError, match="port occupied"):
            activation.route()
    assert not activation.ROUTE.exists()


def test_route_reconnect_retires_only_exact_owned_process(tmp_path, monkeypatch):
    monkeypatch.setattr(activation, "ROUTE", tmp_path / "route.json")
    monkeypatch.setattr(activation, "ROUTE_HISTORY", tmp_path / "history.jsonl")
    old = {
        "pod_name": "previous-owned",
        "pod_uid": "old-pod",
        "port": activation.PORT,
        "process_id": 12345,
        "started": "Wed Oct 14 21:07:04 2026",
        "command_sha256": "digest",
    }
    activation.ROUTE.write_text(json.dumps(old))
    activation.ROUTE.chmod(0o600)
    state = {"alive": True, "killed": []}
    monkeypatch.setattr(
        activation,
        "_process_identity",
        lambda pid, name: (
            {"started": old["started"], "command_sha256": old["command_sha256"]}
            if state["alive"]
            else None
        ),
    )
    monkeypatch.setattr(activation, "_route_port_owned", lambda pid: None)

    def kill(pid, sig):
        state["killed"].append((pid, sig))
        state["alive"] = False

    monkeypatch.setattr(activation.os, "kill", kill)
    assert activation._prior_route({"pod_uid": "new-pod"}) is None
    assert state["killed"] == [(12345, activation.signal.SIGTERM)]
    assert json.loads(activation.ROUTE_HISTORY.read_text()) == old


def test_route_reconnect_refuses_foreign_pid_without_killing(tmp_path, monkeypatch):
    monkeypatch.setattr(activation, "ROUTE", tmp_path / "route.json")
    activation.ROUTE.write_text(
        json.dumps(
            {
                "pod_name": "previous-owned",
                "pod_uid": "old-pod",
                "port": activation.PORT,
                "process_id": 12345,
                "started": "previous",
                "command_sha256": "previous",
            }
        )
    )
    activation.ROUTE.chmod(0o600)
    monkeypatch.setattr(
        activation,
        "_process_identity",
        lambda *_: {"started": "foreign", "command_sha256": "foreign"},
    )
    monkeypatch.setattr(activation.os, "kill", lambda *_: pytest.fail("foreign process killed"))
    with pytest.raises(operator.AdmissionError, match="start changed"):
        activation._prior_route({"pod_uid": "new-pod"})


def test_route_listener_rejects_other_project_even_with_ready_health(monkeypatch):
    class OtherService:
        returncode = 0
        stdout = (
            b"COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
            b"Python 678 user 3u IPv4 0 0t0 TCP 127.0.0.1:18765 (LISTEN)\n"
        )

    monkeypatch.setattr(activation.subprocess, "run", lambda *_a, **_k: OtherService())
    with pytest.raises(operator.AdmissionError, match="another service"):
        activation._route_port_owned(12345)


def test_route_pid_identity_is_exact_not_ambient(monkeypatch):
    command = " ".join(
        [
            *operator.KUBECTL,
            "-n",
            operator.NAMESPACE,
            "port-forward",
            "--address=127.0.0.1",
            "pod/owned",
            "18765:8765",
        ]
    )

    class Running:
        returncode = 0
        stdout = ("S Wed Oct 14 21:07:04 2026 " + command + "\n").encode()

    monkeypatch.setattr(activation.subprocess, "run", lambda *_a, **_k: Running())
    assert activation._process_identity(12345, "owned")["started"] == "Wed Oct 14 21:07:04 2026"
    with pytest.raises(operator.AdmissionError, match="different process"):
        activation._process_identity(12345, "foreign")
