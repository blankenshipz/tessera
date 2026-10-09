"""Synthetic process/Kubernetes/TLS seam checks; never starts kubectl or a route."""

import json
import socket
import ssl
import stat
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tessera_control import connection_tunnel as module
from tessera_control.cli import parser
from tessera_control.connection_tunnel import (
    ConnectionTunnel,
    IdentityChanged,
    KubectlTarget,
    PortUnavailable,
    TargetUnavailable,
    TunnelConfig,
    TunnelError,
)


@pytest.fixture
def config(tmp_path, monkeypatch):
    kubeconfig, ca = tmp_path / "operator-config", tmp_path / "public-ca"
    kubeconfig.write_text("synthetic credential reference")
    ca.write_text("synthetic public CA reference")
    monkeypatch.setattr(
        module.ssl, "create_default_context", lambda *, cafile: ("verified-ca", cafile)
    )
    monkeypatch.setattr(module, "_port_owners", lambda _: None)
    return TunnelConfig(
        kubeconfig,
        "explicit-context",
        "lab",
        "namespace-uid",
        "gateway",
        "deployment-uid",
        ca,
        tmp_path / "private-state",
        max_seconds=18,
    )


class Clock:
    value = 0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class Child:
    pid = 12345

    def __init__(self):
        self.exit = None
        self.terminations = self.kills = 0

    def poll(self):
        return self.exit

    def terminate(self):
        self.terminations += 1
        self.exit = 0

    def kill(self):
        self.kills += 1
        self.exit = -9

    def wait(self, timeout):
        assert timeout == 5
        return self.exit


class Target:
    def __init__(self):
        self.reads = 0
        self.selections = 0

    def select(self):
        self.selections += 1
        return {"name": "gateway-pod", "uid": "pod-uid"}

    def command(self, identity):
        assert identity["uid"] == "pod-uid"
        return ["kubectl", "owned-command"]

    def validate_active(self, identity):
        assert identity["uid"] == "pod-uid"
        self.reads += 1


def supervisor(config, *, probe=None, fingerprint=None, available=None, target=None):
    clock, children, target = Clock(), [], target or Target()

    def launch(args):
        assert args == ["kubectl", "owned-command"]
        child = Child()
        children.append(child)
        return child

    tunnel = ConnectionTunnel(
        config,
        target=target,
        launch=launch,
        probe=probe or (lambda *_: True),
        fingerprint=fingerprint or (lambda _: "same-owned-start-command"),
        available=available or (lambda _: None),
        monotonic=clock.now,
        sleep=clock.sleep,
    )
    return tunnel, children, target, clock


def receipt(config):
    return json.loads((config.state_dir / "connection-tunnel.json").read_text())


def test_normal_foreground_deadline_receipt_private_and_only_own_child_cleaned(config):
    tunnel, children, target, clock = supervisor(config)
    assert tunnel.run() == 0
    assert clock.value == config.max_seconds
    assert len(children) == 1 and children[0].terminations == 1 and children[0].kills == 0
    assert target.reads >= 2
    saved = receipt(config)
    assert saved["stop_reason"] == "deadline"
    assert saved["ready"] is False and saved["state"] == "stopped"
    assert saved["origin"] == "https://localhost:18443"
    assert saved["namespace_uid"] == "namespace-uid" and saved["gateway_pod_uid"] == "pod-uid"
    assert saved["child_fingerprint"] == "same-owned-start-command"
    assert stat.S_IMODE(config.state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((config.state_dir / "connection-tunnel.json").stat().st_mode) == 0o600
    assert not list(config.state_dir.glob(".connection-tunnel-*"))
    assert "synthetic credential reference" not in json.dumps(saved)


def test_one_probe_failure_does_not_restart_or_tear_active_connections(config):
    answers = iter([True, False, True, True])
    tunnel, children, _, _ = supervisor(config, probe=lambda *_: next(answers))
    assert tunnel.run() == 0
    assert len(children) == 1
    assert children[0].terminations == 1  # shutdown only, not the failed probe


def test_three_probe_failures_restart_only_owned_forward_with_bounded_backoff(config):
    answers = iter([False, False, False, True, True])
    tunnel, children, _, clock = supervisor(
        replace(config, max_seconds=24), probe=lambda *_: next(answers, True)
    )
    assert tunnel.run() == 0
    assert len(children) == 2
    assert all(child.terminations == 1 and child.kills == 0 for child in children)
    assert clock.value == 24


def test_default_no_deadline_stops_on_operator_signal_without_stale_pid_adoption(config):
    tunnel, children, _, _ = supervisor(replace(config, max_seconds=None))

    def probe(*_):
        tunnel.stop()
        return True

    tunnel.probe = probe
    assert tunnel.run() == 0
    saved = receipt(config)
    assert saved["expires_at"] is None and saved["stop_reason"] == "operator_stop"
    assert children[0].terminations == 1
    # Old receipt PIDs are data only: a new run creates its own child and never
    # sends a signal to a stale recorded PID.
    saved["child_pid"] = 99999
    module._receipt(config.state_dir / "connection-tunnel.json", saved)
    again, own_children, _, _ = supervisor(config)
    assert again.run() == 0 and own_children[0].terminations == 1


def test_busy_port_never_launches_or_kills_another_project(config):
    def occupied(_):
        raise PortUnavailable("private unrelated process data must not be logged")

    tunnel, children, _, _ = supervisor(config, available=occupied)
    assert tunnel.run() == 1 and not children
    assert receipt(config)["error_class"] == "PortUnavailable"
    assert "unrelated process data" not in json.dumps(receipt(config))


def test_changed_child_fingerprint_refuses_kill_and_reports_precise_lifecycle(config):
    answers = iter(["original-start-command", "different-project-start-command"])
    tunnel, children, _, _ = supervisor(config, fingerprint=lambda _: next(answers))
    assert tunnel.run() == 1
    assert children[0].terminations == children[0].kills == 0
    assert receipt(config)["stop_reason"] == "cleanup_refused"
    assert receipt(config)["cleanup_refused"] is True


def test_certificate_identity_failure_is_fatal_not_insecure_retry(config):
    def failed_tls(*_):
        raise ssl.SSLCertVerificationError("private certificate details are not logged")

    tunnel, children, _, _ = supervisor(config, probe=failed_tls)
    assert tunnel.run() == 1 and len(children) == 1
    assert children[0].terminations == 1
    assert receipt(config)["error_class"] == "SSLCertVerificationError"
    assert "private certificate" not in json.dumps(receipt(config))


def test_namespace_or_deployment_identity_change_stops_owned_child(config):
    target = Target()

    def changed(_):
        raise IdentityChanged("secret response not persisted")

    target.validate_active = changed
    tunnel, children, _, _ = supervisor(config, target=target)
    assert tunnel.run() == 1 and len(children) == 1 and children[0].terminations == 1
    assert receipt(config)["error_class"] == "IdentityChanged"


def objects(config):
    def owner(kind, name, uid):
        return [{"kind": kind, "name": name, "uid": uid, "controller": True}]

    metadata = {
        "name": "gateway-pod",
        "uid": "pod-uid",
        "namespace": config.namespace,
        "labels": {"app": "gateway"},
        "ownerReferences": owner("ReplicaSet", "replica", "rs-uid"),
    }
    return {
        "namespace": {"metadata": {"name": config.namespace, "uid": config.namespace_uid}},
        "deployment": {
            "metadata": {
                "name": config.gateway_deployment,
                "uid": config.gateway_deployment_uid,
                "namespace": config.namespace,
            },
            "spec": {"selector": {"matchLabels": {"app": "gateway"}}},
        },
        "replicaset": {
            "metadata": {
                "name": "replica",
                "uid": "rs-uid",
                "namespace": config.namespace,
                "ownerReferences": owner(
                    "Deployment", config.gateway_deployment, config.gateway_deployment_uid
                ),
            }
        },
        "pod": {
            "kind": "Pod",
            "apiVersion": "v1",
            "metadata": metadata,
            "status": {
                "phase": "Running",
                "containerStatuses": [{"ready": True}],
                "conditions": [{"type": "Ready", "status": "True"}],
            },
        },
    }


def kubernetes(config, monkeypatch):
    monkeypatch.setattr(module.shutil, "which", lambda _: "/usr/local/bin/kubectl")
    target, values = KubectlTarget(config), objects(config)

    def get(kind, name=None, selector=None):
        if kind == "pods":
            assert selector == "app=gateway"
            return {"kind": "PodList", "metadata": {}, "items": [values["pod"]]}
        return values[kind]

    monkeypatch.setattr(target, "get", get)
    return target, values


def test_gateway_selection_requires_exact_namespace_deployment_replica_and_pod_uids(
    config, monkeypatch
):
    target, values = kubernetes(config, monkeypatch)
    identity = target.select()
    assert identity == {"name": "gateway-pod", "uid": "pod-uid"}
    target.validate_active(identity)
    command = target.command(identity)
    assert command[:5] == [
        "/usr/local/bin/kubectl",
        "--kubeconfig",
        str(config.kubeconfig),
        "--context",
        "explicit-context",
    ]
    assert "--address=127.0.0.1" in command and "18443:443" in command
    assert "--insecure-skip-tls-verify=false" in command
    assert "--insecure-skip-tls-verify=true" not in " ".join(command)
    values["namespace"]["metadata"]["uid"] = "replacement"
    with pytest.raises(IdentityChanged):
        target.select()
    values["namespace"]["metadata"]["uid"] = config.namespace_uid
    values["replicaset"]["metadata"]["ownerReferences"][0]["uid"] = "other-deployment"
    with pytest.raises(IdentityChanged):
        target.select()


def test_pinned_pod_and_explicit_selector_cannot_widen_owned_deployment(config, monkeypatch):
    target, _ = kubernetes(replace(config, gateway_selector="other=broad"), monkeypatch)
    with pytest.raises(IdentityChanged):
        target.select()
    target, _ = kubernetes(
        replace(config, gateway_pod="gateway-pod", gateway_pod_uid="wrong-uid"), monkeypatch
    )
    with pytest.raises(IdentityChanged):
        target.select()
    target, values = kubernetes(config, monkeypatch)
    values["pod"]["status"]["containerStatuses"][0]["ready"] = False
    with pytest.raises(TargetUnavailable):
        target.select()


def generic_list(config, monkeypatch):
    target, values = kubernetes(config, monkeypatch)
    original = target.get
    listing = {"kind": "List", "apiVersion": "v1", "metadata": {}, "items": [values["pod"]]}

    def get(kind, name=None, selector=None):
        if kind == "pods":
            assert selector == "app=gateway"
            return listing
        return original(kind, name, selector)

    monkeypatch.setattr(target, "get", get)
    return target, values, listing


def test_normal_kubectl_generic_list_selects_same_owned_gateway(config, monkeypatch):
    target, _, _ = generic_list(config, monkeypatch)
    identity = target.select()
    assert identity == {"name": "gateway-pod", "uid": "pod-uid"}
    target.validate_active(identity)
    assert "pod/gateway-pod" in target.command(identity)


@pytest.mark.parametrize(
    "item",
    [
        {"kind": "Service", "apiVersion": "v1"},
        {"kind": "Pod", "apiVersion": "apps/v1"},
        {"kind": "Pod"},
        "malformed-object",
    ],
)
def test_generic_list_rejects_every_mixed_or_untyped_item(config, monkeypatch, item):
    target, _, listing = generic_list(config, monkeypatch)
    listing["items"].append(item)
    with pytest.raises(TargetUnavailable, match="GatewayListIncomplete"):
        target.select()


@pytest.mark.parametrize("uid", [None, "", True, 123, [], {}, "space uid", "--flag"])
def test_generic_list_rejects_malformed_pod_uid(config, monkeypatch, uid):
    target, values, _ = generic_list(config, monkeypatch)
    values["pod"]["metadata"]["uid"] = uid
    with pytest.raises(IdentityChanged, match="GatewayPodChanged"):
        target.select()


def test_generic_list_preserves_namespace_label_owner_and_ambiguity_fences(config, monkeypatch):
    target, values, listing = generic_list(config, monkeypatch)
    metadata = values["pod"]["metadata"]
    metadata["namespace"] = "outside"
    with pytest.raises(IdentityChanged):
        target.select()
    metadata["namespace"] = config.namespace
    metadata["labels"] = {"app": "different"}
    with pytest.raises(IdentityChanged):
        target.select()
    metadata["labels"] = {"app": "gateway"}
    values["replicaset"]["metadata"]["ownerReferences"][0]["uid"] = "foreign-deployment"
    with pytest.raises(IdentityChanged):
        target.select()
    values["replicaset"]["metadata"]["ownerReferences"][0]["uid"] = config.gateway_deployment_uid
    listing["items"].append(values["pod"])
    with pytest.raises(TargetUnavailable, match="GatewaySelectionAmbiguous"):
        target.select()


def test_kubectl_failure_sanitizes_plugin_output_and_uses_no_implicit_context(config, monkeypatch):
    monkeypatch.setattr(module.shutil, "which", lambda _: "/kubectl")
    target = KubectlTarget(config)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1, stdout=b"private plugin response", stderr=b"secret")

    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(TargetUnavailable, match="KubernetesReadUnavailable"):
        target.get("namespace", config.namespace)
    assert "--context" in calls[0] and "explicit-context" in calls[0]
    assert "--kubeconfig" in calls[0]


def test_tls_probe_uses_literal_loopback_verified_hostname_and_no_authorization(
    config, monkeypatch
):
    calls = []

    class Socket:
        def close(self):
            calls.append("close")

    class TLS:
        def wrap_socket(self, sock, server_hostname):
            assert server_hostname == "localhost"
            calls.append(("hostname", server_hostname))
            return sock

    class HTTP:
        def __init__(self, host, port, timeout):
            assert host == "127.0.0.1" and port == 18443 and timeout == 3

        def request(self, method, path, headers):
            assert method == "GET" and path == "/projects"
            assert headers == {"Host": "localhost:18443"}

        def getresponse(self):
            return SimpleNamespace(status=401)

        def close(self):
            calls.append("http-close")

    def connect(address, timeout):
        assert address == ("127.0.0.1", 18443) and timeout == 3
        return Socket()

    monkeypatch.setattr(module.socket, "create_connection", connect)
    monkeypatch.setattr(module.http.client, "HTTPConnection", HTTP)
    assert module._probe(config, TLS()) is True
    assert ("hostname", "localhost") in calls


def test_cli_help_credential_free_and_required_explicit_references(capsys):
    with pytest.raises(SystemExit) as result:
        parser().parse_args(["connection-tunnel", "--help"])
    assert result.value.code == 0
    help_text = capsys.readouterr().out
    assert "--namespace-uid" in help_text and "--gateway-deployment-uid" in help_text
    assert "until operator stop" in " ".join(help_text.split())
    with pytest.raises(SystemExit):
        parser().parse_args(["connection-tunnel"])
    assert (
        parser().parse_args(["serve", "--state-dir", "state", "--port", "8080"]).command == "serve"
    )


def test_unsafe_state_lock_port_and_reference_validation(config, tmp_path):
    with pytest.raises(TunnelError):
        replace(config, context="--insecure-skip-tls-verify").validate()
    with pytest.raises(TunnelError):
        replace(config, gateway_pod="gateway-pod").validate()
    config.state_dir.mkdir(mode=0o700)
    (config.state_dir / "connection-tunnel.lock").symlink_to(tmp_path / "not-owned")
    tunnel, children, _, _ = supervisor(config)
    with pytest.raises(OSError):
        tunnel.run()
    assert not children
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        with pytest.raises(PortUnavailable):
            module._port_available(listener.getsockname()[1])


def test_port_owner_race_refuses_foreign_listener_without_killing_it(config, monkeypatch):
    monkeypatch.setattr(module, "_port_owners", lambda _: [99999])
    tunnel, children, _, _ = supervisor(config)
    assert tunnel.run() == 1 and len(children) == 1
    assert children[0].terminations == 1  # only the Popen child, never foreign PID
    assert receipt(config)["error_class"] == "IdentityChanged"


def test_exclusive_state_lock_refuses_second_operator_process(config):
    import fcntl

    config.state_dir.mkdir(mode=0o700)
    lock = config.state_dir / "connection-tunnel.lock"
    lock.touch(mode=0o600)
    with lock.open("r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        tunnel, children, _, _ = supervisor(config)
        with pytest.raises(TunnelError, match="TunnelAlreadyRunning"):
            tunnel.run()
        assert not children


def test_exited_child_restarts_with_bounded_backoff_not_a_busy_loop(config):
    tunnel, children, _, clock = supervisor(replace(config, max_seconds=24))
    original_launch = tunnel.launch

    def exited(args):
        child = original_launch(args)
        child.exit = 1
        return child

    tunnel.launch = exited
    assert tunnel.run() == 0
    assert 1 < len(children) < 5
    assert clock.value == 24
    assert all(child.terminations == child.kills == 0 for child in children)
