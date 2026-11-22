"""Operator-owned, one-shot activation and authenticated local port-forward."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import subprocess
import time

import httpx
from mcp.types import ToolAnnotations

from . import kubernetes as target
from . import node_transfer as transfer
from . import private_import as admission
from .upgrade_support import (
    _ready_pod_for as _ready_pod_for,
)
from .upgrade_support import (
    _write_once as _write_once,
)
from .upgrade_support import (
    ready_pod as ready_pod,
)

INTENT = target.ACTIVATED_MARKER
ROUTE = target.SOURCE / "private/local-persistent-route.json"
ROUTE_HISTORY = target.SOURCE / "private/local-persistent-route-history.jsonl"
PORT = 18765


def _activation_identity() -> tuple[dict, dict, str]:
    record, pvc, deploy = admission._owned()
    mapping, root = transfer._fixed_path()
    if (
        mapping["namespace_uid"] != record["namespace_uid"]
        or mapping["pvc_uid"] != pvc["metadata"]["uid"]
    ):
        raise target.AdmissionError("import namespace or PVC identity changed")
    intent = admission._receipt(transfer.RECEIPT)
    complete = admission._receipt(transfer.COMPLETE)
    if any(complete.get(key) != value for key, value in intent.items()) or any(
        complete.get(key) != mapping[key] for key in ("namespace_uid", "pvc_uid", "pv_uid")
    ):
        raise target.AdmissionError("verified import custody changed")
    if (
        not target.FENCE_MARKER.is_file()
        or target.FENCE_MARKER.is_symlink()
        or target.FENCE_MARKER.stat().st_mode & 0o077
    ):
        raise target.AdmissionError("source quarantine absent")
    marker = json.loads(target.FENCE_MARKER.read_bytes())
    if marker.get("namespace_uid") != mapping["namespace_uid"]:
        raise target.AdmissionError("source fence identity changed")
    target._source_listener_absent()
    for name in ("tessera.sqlite3", "owner.header"):
        if (target.SOURCE / name).exists() or (target.SOURCE / name).is_symlink():
            raise target.AdmissionError("source authority revived")
    verified = target.validate_backup(target.FENCED_BACKUP)
    if verified["snapshot_sha256"] != complete["snapshot_sha256"]:
        raise target.AdmissionError("fenced snapshot drifted")
    owner = target.QUARANTINE / "owner.header"
    with transfer._regular(owner, 8192) as stream:
        digest = hashlib.sha256(stream.read()).hexdigest()
    if digest != complete.get("owner_sha256"):
        raise target.AdmissionError("private owner custody changed")
    for name, digest in (
        ("tessera.sqlite3", verified["snapshot_sha256"]),
        ("owner.header", complete["owner_sha256"]),
    ):
        if transfer._node("sha256sum", root + "/control/" + name).decode().split()[0] != digest:
            raise target.AdmissionError("imported state drifted")
        if (
            transfer._node("stat", "-c", "%F:%a:%u:%g", root + "/control/" + name)
            != b"regular file:600:65532:65532"
        ):
            raise target.AdmissionError("imported state permissions drifted")
    image = deploy["spec"]["template"]["spec"]["containers"][0]["image"]
    if not deploy.get("metadata", {}).get("resourceVersion") or not deploy.get("metadata", {}).get(
        "uid"
    ):
        raise target.AdmissionError("Deployment revision unavailable")
    return mapping, deploy, image


def activate() -> dict:
    """Create permanent activation intent before atomic UID/RV/replica-tested patch."""
    if INTENT.exists() or INTENT.is_symlink():
        raise target.AdmissionError("prior activation retained; no retry or source rollback")
    mapping, deploy, image = _activation_identity()
    meta = deploy["metadata"]
    intent = {
        **mapping,
        "deployment_uid": meta["uid"],
        "resource_version": meta["resourceVersion"],
        "image": image,
    }
    _write_once(INTENT, intent)
    patch = [
        {"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
        {"op": "test", "path": "/spec/replicas", "value": 0},
        {"op": "test", "path": "/spec/template/spec/containers/0/image", "value": image},
        {"op": "replace", "path": "/spec/replicas", "value": 1},
    ]
    status = admission._api_request(
        "PATCH",
        f"/apis/apps/v1/namespaces/{target.NAMESPACE}/deployments/tessera-control",
        patch,
        content_type="application/json-patch+json",
    )
    if status != 200:
        raise target.AdmissionError("conditional activation failed; permanent intent retained")
    return {
        "state": "activation_patch_submitted; readiness_not_proven",
        "deployment_uid": meta["uid"],
        "pv_uid": mapping["pv_uid"],
    }


def _process_identity(pid: int, pod_name: str) -> dict | None:
    """Inspect only an exact recorded PID; never infer ownership from a port."""
    if type(pid) is not int or not 1 <= pid <= 2**31 - 1:
        raise target.AdmissionError("route process identity invalid")
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat=", "-o", "lstart=", "-o", "command=", "-ww"],
        capture_output=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        return None
    line = result.stdout.decode("utf-8", "replace").strip()
    fields = line.split(maxsplit=6)
    expected = [
        *target.KUBECTL,
        "-n",
        target.NAMESPACE,
        "port-forward",
        "--address=127.0.0.1",
        "pod/" + pod_name,
        f"{PORT}:8765",
    ]
    if fields and fields[0].startswith("Z"):
        return None
    if len(fields) != 7 or fields[6].split() != expected:
        raise target.AdmissionError("route PID belongs to different process")
    return {
        "started": " ".join(fields[1:6]),
        "command_sha256": hashlib.sha256(fields[6].encode()).hexdigest(),
    }


def _route_health() -> None:
    with httpx.Client(trust_env=False, timeout=2) as client:
        if client.get(f"http://127.0.0.1:{PORT}/health/ready").status_code != 200:
            raise target.AdmissionError("target application not ready")


def _route_port_owned(pid: int) -> None:
    result = subprocess.run(
        ["lsof", "-nP", "-iTCP:" + str(PORT), "-sTCP:LISTEN"],
        capture_output=True,
        timeout=5,
        check=False,
    )
    if result.returncode or len(result.stdout) > 8192:
        raise target.AdmissionError("loopback listener ownership unavailable")
    rows = result.stdout.decode("utf-8", "replace").splitlines()[1:]
    if not rows or any(
        len(row.split()) < 9
        or row.split()[1] != str(pid)
        or row.split()[-2] != f"127.0.0.1:{PORT}"
        or row.split()[-1] != "(LISTEN)"
        for row in rows
    ):
        raise target.AdmissionError("loopback port belongs to another service")


def _prior_route(pod: dict) -> dict | None:
    if not (ROUTE.exists() or ROUTE.is_symlink()):
        return None
    prior = admission._receipt(ROUTE)
    if prior.get("port") != PORT or not isinstance(prior.get("pod_name"), str):
        raise target.AdmissionError("prior route uncertain; no process adoption")
    if not isinstance(prior.get("process_id"), int) or not prior.get("started"):
        raise target.AdmissionError("prior route launch uncertain; no process adoption")
    process = _process_identity(prior["process_id"], prior["pod_name"])
    if process is not None:
        if process != {key: prior[key] for key in ("started", "command_sha256")}:
            raise target.AdmissionError("route process start changed; no process termination")
        _route_port_owned(prior["process_id"])
        if prior.get("pod_uid") == pod["pod_uid"]:
            _route_health()
            return {
                **pod,
                "state": "existing_owned_loopback_route; owner_auth_unverified",
                "loopback_port": PORT,
                "process_id": prior["process_id"],
            }
        os.kill(prior["process_id"], signal.SIGTERM)
        for _ in range(30):
            if _process_identity(prior["process_id"], prior["pod_name"]) is None:
                break
            time.sleep(0.1)
        else:
            raise target.AdmissionError("old route did not exit; no replacement")
    # Keep a private custody trail; a partial append refuses a later blind retry.
    if ROUTE_HISTORY.is_symlink() or (
        ROUTE_HISTORY.exists() and ROUTE_HISTORY.stat().st_size > 16384
    ):
        raise target.AdmissionError("route history unavailable")
    fd = os.open(ROUTE_HISTORY, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(json.dumps(prior, sort_keys=True).encode() + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    return None


def route() -> dict:
    """Start supervised Mac-loopback-only authenticated Kubernetes port-forward."""
    pod = ready_pod()
    target._kubeconfig_identity()
    target._docker_identity()
    previous = _prior_route(pod)
    if previous is not None:
        return previous
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", PORT))
        except OSError as error:
            raise target.AdmissionError("Mac loopback port occupied") from error
    # A durable intent precedes spawning. A lost launch response is not adopted.
    pending = {"pod_uid": pod["pod_uid"], "pod_name": pod["pod_name"], "port": PORT}
    if ROUTE.exists():
        replacement = ROUTE.with_name(ROUTE.name + ".pending")
        _write_once(replacement, pending)
        os.replace(replacement, ROUTE)
    else:
        _write_once(ROUTE, pending)
    try:
        proc = subprocess.Popen(
            [
                *target.KUBECTL,
                "-n",
                target.NAMESPACE,
                "port-forward",
                "--address=127.0.0.1",
                "pod/" + pod["pod_name"],
                f"{PORT}:8765",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(0.5)
        if proc.poll() is not None:
            raise target.AdmissionError("loopback route exited; intent retained")
        with socket.create_connection(("127.0.0.1", PORT), timeout=1):
            pass
    except (OSError, subprocess.SubprocessError) as error:
        if "proc" in locals() and proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        raise target.AdmissionError("loopback route unavailable; intent retained") from error
    try:
        if ready_pod()["pod_uid"] != pod["pod_uid"]:
            raise target.AdmissionError("route Pod changed during startup")
        _route_port_owned(proc.pid)
        _route_health()
    except Exception as error:
        proc.terminate()
        proc.wait(timeout=5)
        raise target.AdmissionError("route readiness not proven; intent retained") from error
    # Intent remains durable even if receipt update is interrupted. Unknown process
    # must be inspected, never replaced automatically.
    completed = ROUTE.with_name(ROUTE.name + ".new")
    try:
        process = _process_identity(proc.pid, pod["pod_name"])
        if process is None:
            raise target.AdmissionError("route process identity unavailable")
    except Exception as error:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        raise target.AdmissionError("route process identity uncertain; intent retained") from error
    _write_once(
        completed,
        {
            **pending,
            "process_id": proc.pid,
            **process,
        },
    )
    os.replace(completed, ROUTE)
    return {
        **pod,
        "state": "host_loopback_port_forward; app_health_ready; owner_auth_unverified",
        "loopback_port": PORT,
        "process_id": proc.pid,
    }


def register(server) -> None:
    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
    )
    def local_activate() -> dict:
        """One-shot guarded Deployment activation; never auto-unfence source."""
        return activate()

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    )
    def local_ready() -> dict:
        """Inspect exact target Pod readiness; not application authentication."""
        return ready_pod()

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    )
    def local_loopback_route() -> dict:
        """Start fixed authenticated Kubernetes port-forward on 127.0.0.1 only."""
        return route()
