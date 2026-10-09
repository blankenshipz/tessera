"""Foreground operator-owned, UID-fenced loopback Kubernetes TLS forwarding.

No login/token proxy, deployment writes, daemonization or stale-PID recovery.
Existing explicit kubeconfig authentication is consumed by kubectl only.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path


class TunnelError(Exception):
    """Only a closed sanitized error class, never subprocess/HTTP bodies."""


class IdentityChanged(TunnelError):
    pass


class TargetUnavailable(TunnelError):
    pass


class PortUnavailable(TunnelError):
    pass


@dataclass(frozen=True)
class TunnelConfig:
    kubeconfig: Path
    context: str
    namespace: str
    namespace_uid: str
    gateway_deployment: str
    gateway_deployment_uid: str
    public_ca_file: Path
    state_dir: Path
    listen_port: int = 18443
    gateway_port: int = 443
    max_seconds: int | None = None
    gateway_selector: str | None = None
    gateway_pod: str | None = None
    gateway_pod_uid: str | None = None

    def __post_init__(self):
        for name in ("kubeconfig", "public_ca_file", "state_dir"):
            object.__setattr__(self, name, getattr(self, name).expanduser().absolute())

    def validate(self):
        for value in (
            self.context,
            self.namespace,
            self.namespace_uid,
            self.gateway_deployment,
            self.gateway_deployment_uid,
        ):
            if not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", value) or value.startswith("-"):
                raise TunnelError("InvalidReference")
        if not 1 <= self.listen_port <= 65535 or not 1 <= self.gateway_port <= 65535:
            raise TunnelError("InvalidPort")
        if self.max_seconds is not None and self.max_seconds < 1:
            raise TunnelError("InvalidDeadline")
        if bool(self.gateway_pod) != bool(self.gateway_pod_uid):
            raise TunnelError("PinnedPodRequiresNameAndUID")
        if self.gateway_pod and not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", self.gateway_pod):
            raise TunnelError("InvalidPodReference")
        for path in (self.kubeconfig, self.public_ca_file):
            if not path.is_file():
                raise TunnelError("ReferenceFileUnavailable")


def _now():
    return datetime.now(UTC).isoformat()


def _private_directory(path):
    path = path.expanduser().absolute()
    for part in (path, *path.parents):
        if part.is_symlink():
            raise TunnelError("UnsafeStateDirectory")
    path.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise TunnelError("UnsafeStateDirectory")
    return path


def _receipt(path, value):
    temporary = path.parent / (".connection-tunnel-" + uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _fingerprint(pid):
    """Process start time plus command hash; never persisted as plaintext arguments."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
            capture_output=True,
            timeout=3,
            check=False,
        )
        if result.returncode or not result.stdout.strip():
            return None
        return hashlib.sha256(result.stdout.strip()).hexdigest()
    except (OSError, subprocess.TimeoutExpired):
        return None


def _port_owners(port):
    executable = shutil.which("lsof")
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [executable, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            timeout=3,
            check=False,
        )
        return sorted({int(line) for line in result.stdout.splitlines() if line.isdigit()})
    except (OSError, subprocess.TimeoutExpired):
        return None


def _port_available(port):
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", port))
    except OSError as error:
        raise PortUnavailable("PortUnavailable") from error


def _owner(metadata, kind):
    owners = [
        entry
        for entry in metadata.get("ownerReferences", [])
        if entry.get("controller") is True and entry.get("kind") == kind
    ]
    if len(owners) != 1 or not owners[0].get("name") or not owners[0].get("uid"):
        raise IdentityChanged("GatewayOwnerChanged")
    return owners[0]


class KubectlTarget:
    def __init__(self, config):
        self.config = config
        executable = shutil.which("kubectl")
        if executable is None:
            raise TunnelError("KubectlUnavailable")
        self.base = [
            executable,
            "--kubeconfig",
            str(config.kubeconfig),
            "--context",
            config.context,
            "--request-timeout=10s",
            "--insecure-skip-tls-verify=false",
        ]

    def get(self, kind, name=None, selector=None):
        args = self.base + (["-n", self.config.namespace] if kind != "namespace" else [])
        args += ["get", kind] + ([name] if name else [])
        if selector:
            args += ["-l", selector]
        try:
            result = subprocess.run(
                args + ["-o", "json"], capture_output=True, timeout=15, check=False
            )
            if result.returncode:
                raise TargetUnavailable("KubernetesReadUnavailable")
            if len(result.stdout) > 8 * 1024 * 1024:
                raise TargetUnavailable("KubernetesReadTooLarge")
            value = json.loads(result.stdout)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            raise TargetUnavailable("KubernetesReadUnavailable") from error

    def _deployment(self):
        config = self.config
        namespace = self.get("namespace", config.namespace).get("metadata", {})
        if (
            namespace.get("name") != config.namespace
            or namespace.get("uid") != config.namespace_uid
            or namespace.get("deletionTimestamp")
        ):
            raise IdentityChanged("NamespaceIdentityChanged")
        deployment = self.get("deployment", config.gateway_deployment)
        metadata = deployment.get("metadata", {})
        if (
            metadata.get("name") != config.gateway_deployment
            or metadata.get("uid") != config.gateway_deployment_uid
            or metadata.get("namespace") != config.namespace
            or metadata.get("deletionTimestamp")
        ):
            raise IdentityChanged("GatewayDeploymentChanged")
        specification = deployment.get("spec", {}).get("selector", {})
        labels = specification.get("matchLabels")
        if not isinstance(labels, dict) or not labels or specification.get("matchExpressions"):
            raise IdentityChanged("UnsupportedGatewaySelector")
        selector = ",".join(f"{key}={labels[key]}" for key in sorted(labels))
        if config.gateway_selector is not None and config.gateway_selector != selector:
            raise IdentityChanged("GatewaySelectorChanged")
        return labels, selector

    def _pod(self, pod, labels):
        if not isinstance(pod, dict) or not isinstance(pod.get("metadata"), dict):
            raise IdentityChanged("GatewayPodChanged")
        config, metadata = self.config, pod["metadata"]
        uid, name = metadata.get("uid"), metadata.get("name")
        if (
            metadata.get("namespace") != config.namespace
            or not isinstance(uid, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", uid)
            or not isinstance(name, str)
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", name)
            or not isinstance(metadata.get("labels"), dict)
            or metadata.get("deletionTimestamp")
            or any(metadata.get("labels", {}).get(key) != value for key, value in labels.items())
        ):
            raise IdentityChanged("GatewayPodChanged")
        if config.gateway_pod and (
            metadata["name"] != config.gateway_pod or metadata["uid"] != config.gateway_pod_uid
        ):
            raise IdentityChanged("PinnedGatewayPodChanged")
        owner = _owner(metadata, "ReplicaSet")
        replica = self.get("replicaset", owner["name"])
        replica_metadata = replica.get("metadata", {})
        parent = _owner(replica_metadata, "Deployment")
        if (
            replica_metadata.get("uid") != owner["uid"]
            or replica_metadata.get("namespace") != config.namespace
            or parent["name"] != config.gateway_deployment
            or parent["uid"] != config.gateway_deployment_uid
        ):
            raise IdentityChanged("GatewayOwnerChanged")
        status = pod.get("status", {})
        containers = status.get("containerStatuses", [])
        if (
            status.get("phase") != "Running"
            or not containers
            or not all(item.get("ready") is True for item in containers)
            or not any(
                item.get("type") == "Ready" and item.get("status") == "True"
                for item in status.get("conditions", [])
            )
        ):
            raise TargetUnavailable("GatewayNotReady")
        return {"name": metadata["name"], "uid": metadata["uid"]}

    def select(self):
        labels, selector = self._deployment()
        if self.config.gateway_pod:
            return self._pod(self.get("pod", self.config.gateway_pod), labels)
        listing = self.get("pods", selector=selector)
        kind = listing.get("kind")
        if (
            kind not in ("PodList", "List")
            or (kind == "List" and listing.get("apiVersion") != "v1")
            or not isinstance(listing.get("metadata"), dict)
            or listing["metadata"].get("continue")
        ):
            raise TargetUnavailable("GatewayListIncomplete")
        if not isinstance(listing.get("items"), list):
            raise TargetUnavailable("GatewayListIncomplete")
        ready = []
        for pod in listing.get("items", []):
            # kubectl's generic List is not a typed PodList: require every
            # item's explicit core/v1 Pod type before considering any candidate.
            if (
                not isinstance(pod, dict)
                or (kind == "List" and (pod.get("kind") != "Pod" or pod.get("apiVersion") != "v1"))
                or (kind == "PodList" and pod.get("kind") not in (None, "Pod"))
                or (kind == "PodList" and pod.get("apiVersion") not in (None, "v1"))
            ):
                raise TargetUnavailable("GatewayListIncomplete")
            if not isinstance(pod.get("metadata"), dict):
                raise IdentityChanged("GatewayPodChanged")
            if kind == "PodList" and pod["metadata"].get("deletionTimestamp"):
                continue
            try:
                ready.append(self._pod(pod, labels))
            except TargetUnavailable as error:
                if kind == "List" and error.args != ("GatewayNotReady",):
                    raise
                continue
        if len(ready) != 1:
            raise TargetUnavailable("GatewaySelectionAmbiguous")
        return ready[0]

    def validate_active(self, identity):
        labels, _ = self._deployment()
        pod = self._pod(self.get("pod", identity["name"]), labels)
        if pod != identity:
            raise IdentityChanged("ActiveGatewayPodUIDChanged")

    def command(self, identity):
        return self.base + [
            "-n",
            self.config.namespace,
            "port-forward",
            "--address=127.0.0.1",
            "pod/" + identity["name"],
            f"{self.config.listen_port}:{self.config.gateway_port}",
        ]


def _probe(config, tls_context):
    """Connect literal loopback, validate localhost SAN/CA, require unauthenticated denial."""
    connection = http.client.HTTPConnection("127.0.0.1", config.listen_port, timeout=3)
    raw = socket.create_connection(("127.0.0.1", config.listen_port), timeout=3)
    try:
        connection.sock = tls_context.wrap_socket(raw, server_hostname="localhost")
        connection.request("GET", "/projects", headers={"Host": f"localhost:{config.listen_port}"})
        response = connection.getresponse()
        return response.status == 401
    finally:
        raw.close()
        connection.close()


class ConnectionTunnel:
    def __init__(
        self,
        config,
        *,
        target=None,
        launch=None,
        probe=None,
        fingerprint=None,
        available=None,
        monotonic=None,
        sleep=None,
    ):
        self.config = config
        self.target = target
        self.launch = launch or (
            lambda args: subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        )
        self.probe = probe or _probe
        self.fingerprint = fingerprint or _fingerprint
        self.available = available or _port_available
        self.clock, self.sleep = monotonic or time.monotonic, sleep or time.sleep
        self.stopping, self.child, self.child_fingerprint = False, None, None
        self.state = {}

    def stop(self, *_):
        self.stopping = True

    def _write(self, **fields):
        self.state.update(fields, observed_at=_now())
        _receipt(self.receipt_path, self.state)

    def _cleanup(self):
        child = self.child
        if child is not None and child.poll() is None:
            if (
                self.child_fingerprint is None
                or self.fingerprint(child.pid) != self.child_fingerprint
            ):
                self._write(error_class="ChildIdentityUnverified", cleanup_refused=True)
                raise TunnelError("ChildIdentityUnverified")
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if child.poll() is None and self.fingerprint(child.pid) == self.child_fingerprint:
                    child.kill()
                    child.wait(timeout=5)
                else:
                    raise TunnelError("ChildIdentityUnverified") from None
        self.child, self.child_fingerprint = None, None

    def run(self):
        if os.name != "posix":
            raise TunnelError("UnsupportedOperatorPlatform")
        import fcntl

        config = self.config
        config.validate()
        directory = _private_directory(config.state_dir)
        self.receipt_path = directory / "connection-tunnel.json"
        descriptor = os.open(
            directory / "connection-tunnel.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise TunnelError("UnsafeStateLock")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise TunnelError("TunnelAlreadyRunning") from error
            return self._run_locked()
        finally:
            os.close(descriptor)

    def _run_locked(self):
        config = self.config
        started = self.clock()
        deadline = None if config.max_seconds is None else started + config.max_seconds
        expires = (
            None
            if config.max_seconds is None
            else (datetime.now(UTC) + timedelta(seconds=config.max_seconds)).isoformat()
        )
        self.state = {
            "schema_version": 1,
            "owner": "tessera-connection-tunnel",
            "supervisor_pid": os.getpid(),
            "started_at": _now(),
            "expires_at": expires,
            "origin": f"https://localhost:{config.listen_port}",
            "bind_address": "127.0.0.1",
            "public_ca_file": str(config.public_ca_file),
            "context": config.context,
            "namespace": config.namespace,
            "namespace_uid": config.namespace_uid,
            "gateway_deployment": config.gateway_deployment,
            "gateway_deployment_uid": config.gateway_deployment_uid,
            "ready": False,
            "state": "starting",
            "attempt": 0,
            "stop_reason": None,
            "error_class": None,
            "port_owner_pids": _port_owners(config.listen_port),
        }
        self._write()
        exit_code, failure_count, backoff = 0, 0, 1

        def pause(seconds):
            remaining = seconds if deadline is None else min(seconds, deadline - self.clock())
            if remaining > 0 and not self.stopping:
                self.sleep(remaining)

        try:
            self.available(config.listen_port)
            tls_context = ssl.create_default_context(cafile=str(config.public_ca_file))
            target = self.target or KubectlTarget(config)
            while not self.stopping and (deadline is None or self.clock() < deadline):
                try:
                    if self.child is not None and self.child.poll() is not None:
                        self._cleanup()
                        self._write(ready=False, state="reconnecting", error_class="ForwardExited")
                        pause(backoff)
                        backoff = min(16, backoff * 2)
                        if self.stopping or (deadline is not None and self.clock() >= deadline):
                            break
                    if self.child is None:
                        identity = target.select()
                        self.available(config.listen_port)
                        self.child = self.launch(target.command(identity))
                        self.child_fingerprint = self.fingerprint(self.child.pid)
                        self._write(
                            child_pid=self.child.pid,
                            child_fingerprint=self.child_fingerprint,
                            gateway_pod=identity["name"],
                            gateway_pod_uid=identity["uid"],
                            attempt=self.state["attempt"] + 1,
                            ready=False,
                            state="starting",
                        )
                        failure_count = 0
                        pause(1)
                        if self.stopping or (deadline is not None and self.clock() >= deadline):
                            break
                    target.validate_active(identity)
                    ready = self.probe(config, tls_context) and self.child.poll() is None
                    if ready:
                        owners = _port_owners(config.listen_port)
                        if owners is not None and self.child.pid not in owners:
                            raise IdentityChanged("LoopbackPortOwnerChanged")
                    if ready:
                        failure_count, backoff = 0, 1
                        self._write(ready=True, state="ready", error_class=None)
                    else:
                        failure_count += 1
                        self._write(
                            ready=False, state="probe_degraded", error_class="ReadinessNot401"
                        )
                except (ssl.SSLCertVerificationError, IdentityChanged):
                    raise
                except (TargetUnavailable, OSError, http.client.HTTPException) as error:
                    failure_count += 1
                    self._write(ready=False, state="reconnecting", error_class=type(error).__name__)
                if failure_count >= 3:
                    self._cleanup()
                    pause(backoff)
                    backoff = min(16, backoff * 2)
                    failure_count = 0
                else:
                    pause(5 if self.child is not None else backoff)
            self.state["stop_reason"] = "operator_stop" if self.stopping else "deadline"
        except (TunnelError, OSError, ssl.SSLError) as error:
            exit_code = 1
            self.state.update(stop_reason="error", error_class=type(error).__name__)
        finally:
            try:
                self._cleanup()
            except TunnelError:
                exit_code = 1
                self.state.update(
                    stop_reason="cleanup_refused", error_class="ChildIdentityUnverified"
                )
            self._write(ready=False, state="stopped", stopped_at=_now())
        return exit_code


def add_connection_tunnel_command(subcommands):
    command = subcommands.add_parser(
        "connection-tunnel",
        help="foreground UID-owned Kubernetes loopback TLS route (no login)",
        description="Operate an explicit existing gateway until Ctrl-C/SIGTERM "
        "or an optional deadline. "
        "Requires kubectl, an existing operator kubeconfig and a public CA trusting localhost. "
        "macOS/Linux; no deployment writes, insecure TLS, bearer proxy "
        "or implicit current context.",
    )
    for name in ("kubeconfig", "public-ca-file", "state-dir"):
        command.add_argument("--" + name, type=Path, required=True)
    for name in (
        "context",
        "namespace",
        "namespace-uid",
        "gateway-deployment",
        "gateway-deployment-uid",
    ):
        command.add_argument("--" + name, required=True)
    command.add_argument(
        "--gateway-selector", help="optional exact sorted deployment matchLabels selector"
    )
    command.add_argument(
        "--gateway-pod", help="optional fixed Pod name; requires --gateway-pod-uid"
    )
    command.add_argument("--gateway-pod-uid")
    command.add_argument("--listen-port", type=int, default=18443)
    command.add_argument("--gateway-port", type=int, default=443)
    command.add_argument(
        "--max-seconds",
        type=int,
        help="optional explicit deadline; default runs until operator stop",
    )


def run_connection_tunnel(arguments):
    names = TunnelConfig.__dataclass_fields__
    tunnel = ConnectionTunnel(TunnelConfig(**{name: getattr(arguments, name) for name in names}))
    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        for number in previous:
            signal.signal(number, tunnel.stop)
        return tunnel.run()
    except (TunnelError, OSError, ValueError) as error:
        # No exception messages: kubeconfig plugins/HTTP bodies can contain secrets.
        print("connection-tunnel failed: " + type(error).__name__, file=sys.stderr)
        return 1
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
