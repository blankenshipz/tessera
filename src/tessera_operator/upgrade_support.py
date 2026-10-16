"""Canonical occupied-target safety primitives, shared with legacy local operators.

These implementations are moved, not forked, from kubernetes, private_import,
activation and target_recovery. No bootstrap/migration/authority-restore tools.
The module aliases retain the original bodies' dependency names without importing
any unpublished legacy module or changing the fixed receipt/authority identities.
"""

import base64
import hashlib
import json
import os
import sqlite3
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml

from tessera_control.recovery import (
    BACKUP_DATABASE,
    BACKUP_OBJECTS_DIRECTORY,
    _object_path,
    _read_manifest,
    _sha256,
    _validate_bundle_declaration,
    _validate_snapshot,
)

KUBECONFIG = Path("/Users/zach/.kube/config")
CONTEXT = "docker-desktop"
NAMESPACE = "tessera-local-persistent"
SOURCE = Path("/Users/zach/Code/tessera/.tessera")
REPOSITORY = Path("/Users/zach/Code/tessera")
RESOURCE_RECEIPT = SOURCE / "private/local-persistent-operator.json"
BACKUP_OUTPUT = SOURCE / "private/local-persistent-operator-backup"
ACTIVATED_MARKER = SOURCE / "private/local-persistent-activated"
INTENT = ACTIVATED_MARKER
KUBECTL = ("kubectl", "--kubeconfig", str(KUBECONFIG), "--context", CONTEXT, "--request-timeout=5s")
MAX_STREAM_BYTES = 2 * 1024 * 1024 * 1024
target = admission = activation = sys.modules[__name__]


class AdmissionError(RuntimeError):
    """Sanitized operator error; no kubectl stderr or credential data."""


def _docker(*args: str, timeout: int = 20) -> str:
    try:
        result = subprocess.run(
            ["docker", "--context", "desktop-linux", *args],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AdmissionError("Docker Desktop image operation unavailable") from error
    if result.returncode:
        raise AdmissionError("Docker Desktop image operation failed")
    return result.stdout.decode("utf-8", "replace").strip()


def _kubeconfig_identity() -> dict:
    if KUBECONFIG.is_symlink():
        raise AdmissionError("kubeconfig must not be a symlink")
    info = KUBECONFIG.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise AdmissionError("kubeconfig must be private regular file")
    config = yaml.safe_load(KUBECONFIG.read_text())
    context = next((c for c in config["contexts"] if c["name"] == CONTEXT), None)
    if context is None:
        raise AdmissionError("Docker Desktop context unavailable")
    cluster_name = context["context"]["cluster"]
    cluster = next((c["cluster"] for c in config["clusters"] if c["name"] == cluster_name), None)
    if cluster is None:
        raise AdmissionError("Docker Desktop cluster unavailable")
    parsed = urlsplit(cluster["server"])
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or not parsed.port
    ):
        raise AdmissionError("Docker Desktop API must use local HTTPS")
    if cluster.get("insecure-skip-tls-verify") or not (
        cluster.get("certificate-authority-data") or cluster.get("certificate-authority")
    ):
        raise AdmissionError("verified Kubernetes CA required")
    # Only public target identity leaves this boundary; never return user auth.
    return {
        "context": CONTEXT,
        "server": cluster["server"],
        "kubeconfig_sha256": hashlib.sha256(KUBECONFIG.read_bytes()).hexdigest(),
    }


def _kubectl(*args: str, data: dict | None = None) -> dict:
    result = subprocess.run(
        [*KUBECTL, *args],
        input=json.dumps(data).encode() if data is not None else None,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if result.returncode:
        raise AdmissionError("bounded Kubernetes inspection failed")
    if len(result.stdout) > 1024 * 1024:
        raise AdmissionError("Kubernetes response too large")
    try:
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except ValueError as error:
        raise AdmissionError("invalid Kubernetes inspection response") from error


def validate_backup(path: Path | None = None) -> dict:
    """Re-read fixed backup and verify DB plus every declared project object."""
    output = path or BACKUP_OUTPUT
    try:
        manifest = _read_manifest(output)
        start = time.monotonic()
        snapshot = output / BACKUP_DATABASE
        if _sha256(snapshot, start) != manifest["sha256"]:
            raise AdmissionError("backup snapshot checksum changed")
        schema, watermark = _validate_snapshot(snapshot, start)
        if (schema, watermark) != (manifest["schema_version"], manifest["event_watermark"]):
            raise AdmissionError("backup database metadata changed")
        if manifest.get("objects") is not None:
            _validate_bundle_declaration(snapshot, manifest, start)
            for entry in manifest["objects"]:
                if entry["availability"] == "present":
                    object_path = _object_path(output / BACKUP_OBJECTS_DIRECTORY, entry)
                    if _sha256(object_path, start) != entry["sha256"]:
                        raise AdmissionError("backup object checksum changed")
    except AdmissionError:
        raise
    except Exception as error:
        raise AdmissionError("private backup verification failed") from error
    return {
        "backup_id": manifest["backup_id"],
        "snapshot_sha256": manifest["sha256"],
        "schema_version": schema,
        "event_watermark": watermark,
        "state": "verified_snapshot_only; owner authority not copied",
    }


def _source_listener_absent() -> None:
    result = subprocess.run(
        ["lsof", "-nP", "-iTCP:8765", "-sTCP:LISTEN", "-Fp"],
        capture_output=True,
        timeout=5,
        check=False,
    )
    if result.returncode not in (0, 1) or result.stdout.strip():
        raise AdmissionError("source port in use; stop cutover")


def _receipt(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise target.AdmissionError("operator receipt unavailable")
    mode = path.stat()
    if not stat.S_ISREG(mode.st_mode) or mode.st_mode & 0o077 or mode.st_size > 8192:
        raise target.AdmissionError("operator receipt unsafe")
    try:
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except ValueError as error:
        raise target.AdmissionError("operator receipt invalid") from error


def _api_request(method: str, uri: str, body: dict, *, content_type: str | None = None) -> int:
    """Private kubeconfig client-certificate request; discard all response bodies."""
    identity = target._kubeconfig_identity()
    config = yaml.safe_load(target.KUBECONFIG.read_text())
    context = next(item["context"] for item in config["contexts"] if item["name"] == target.CONTEXT)
    user = next(item["user"] for item in config["users"] if item["name"] == context["user"])
    cluster = next(
        item["cluster"] for item in config["clusters"] if item["name"] == context["cluster"]
    )
    if not all(field in user for field in ("client-certificate-data", "client-key-data")):
        raise target.AdmissionError("client certificate reference unavailable")
    if "certificate-authority-data" not in cluster or cluster["server"] != identity["server"]:
        raise target.AdmissionError("cluster CA or endpoint changed")
    try:
        ca = base64.b64decode(cluster["certificate-authority-data"], validate=True).decode("ascii")
        cert = base64.b64decode(user["client-certificate-data"], validate=True)
        key = base64.b64decode(user["client-key-data"], validate=True)
        tls = ssl.create_default_context(cadata=ca)
        with tempfile.TemporaryDirectory(dir=target.SOURCE / "private") as directory:
            for name, content in (("client.crt", cert), ("client.key", key)):
                fd = os.open(
                    Path(directory) / name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                )
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
            tls.load_cert_chain(
                str(Path(directory) / "client.crt"), str(Path(directory) / "client.key")
            )
        with httpx.Client(
            verify=tls, trust_env=False, follow_redirects=False, timeout=10
        ) as client:
            result = client.request(
                method,
                identity["server"] + uri,
                json=body if content_type is None else None,
                content=json.dumps(body).encode() if content_type is not None else None,
                headers={"Content-Type": content_type} if content_type else None,
            )
        return result.status_code
    except target.AdmissionError:
        raise
    except Exception as error:
        raise target.AdmissionError("private Kubernetes UID-delete unavailable") from error


def _write_once(path, data: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(json.dumps(data, sort_keys=True).encode())
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def ready_pod() -> dict:
    from .upgrade import admitted_activation

    return _ready_pod_for(admitted_activation(admission._receipt(INTENT)))


def _ready_pod_for(activation: dict) -> dict:
    record = admission._receipt(target.RESOURCE_RECEIPT)
    namespace = target._kubectl("get", "namespace", target.NAMESPACE, "-o", "json")
    if namespace.get("metadata", {}).get("uid") != activation.get("namespace_uid") or record.get(
        "namespace_uid"
    ) != activation.get("namespace_uid"):
        raise target.AdmissionError("activated namespace identity changed")
    pvc = target._kubectl(
        "-n", target.NAMESPACE, "get", "pvc", "tessera-control-state", "-o", "json"
    )
    pv = target._kubectl(
        "get", "pv", pvc.get("spec", {}).get("volumeName", "INVALID"), "-o", "json"
    )
    deploy = target._kubectl(
        "-n", target.NAMESPACE, "get", "deployment", "tessera-control", "-o", "json"
    )
    image = deploy["spec"]["template"]["spec"]["containers"][0]["image"]
    if (
        pvc.get("metadata", {}).get("uid") != activation.get("pvc_uid")
        or pv.get("metadata", {}).get("uid") != activation.get("pv_uid")
        or pv.get("spec", {}).get("claimRef", {}).get("uid") != activation.get("pvc_uid")
        or deploy.get("metadata", {}).get("uid") != activation.get("deployment_uid")
        or image != activation.get("image")
        or deploy.get("spec", {}).get("replicas") != 1
    ):
        raise target.AdmissionError("activated resource identity changed")
    target._source_listener_absent()
    if any(
        (target.SOURCE / name).exists() or (target.SOURCE / name).is_symlink()
        for name in ("owner.header", "tessera.sqlite3")
    ):
        raise target.AdmissionError("source authority revived")
    pods = target._kubectl("-n", target.NAMESPACE, "get", "pods", "-o", "json").get("items", [])
    if len(pods) != 1:
        raise target.AdmissionError("expected exactly one target Pod")
    pod = pods[0]
    meta = pod.get("metadata", {})
    owners = [
        owner
        for owner in meta.get("ownerReferences", [])
        if owner.get("kind") == "ReplicaSet" and owner.get("controller") is True
    ]
    if (
        pod.get("status", {}).get("phase") != "Running"
        or not any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in pod.get("status", {}).get("conditions", [])
        )
        or len(owners) != 1
        or pod["spec"]["containers"][0]["image"] != image
    ):
        raise target.AdmissionError("target Pod not ready or image changed")
    replica = target._kubectl(
        "-n", target.NAMESPACE, "get", "replicaset", owners[0]["name"], "-o", "json"
    )
    if replica.get("metadata", {}).get("uid") != owners[0].get("uid") or not any(
        owner.get("uid") == activation["deployment_uid"] and owner.get("controller") is True
        for owner in replica.get("metadata", {}).get("ownerReferences", [])
    ):
        raise target.AdmissionError("Pod controller ownership changed")
    return {
        "pod_name": meta["name"],
        "pod_uid": meta["uid"],
        "deployment_uid": activation["deployment_uid"],
        "pv_uid": activation["pv_uid"],
        "state": "Kubernetes_Pod_Ready; application_auth_not_proven",
    }


def _identity(snapshot: Path) -> dict:
    connection = sqlite3.connect(f"file:{snapshot}?mode=ro&immutable=1", uri=True)
    try:
        row = connection.execute(
            "SELECT incarnation,state FROM control_recovery WHERE singleton=1"
        ).fetchone()
        cursor = connection.execute("SELECT coalesce(max(cursor),0) FROM events").fetchone()[0]
        digest = hashlib.sha256()
        for record in connection.execute("SELECT * FROM events ORDER BY cursor"):
            digest.update(json.dumps(record, separators=(",", ":")).encode())
        identities = hashlib.sha256()
        for table in ("organizations", "principals", "credentials", "projects"):
            identities.update(table.encode())
            for record in connection.execute(f"SELECT * FROM {table} ORDER BY 1"):
                identities.update(repr(record).encode())
        return {
            "incarnation": row[0],
            "recovery_state": row[1],
            "event_cursor": cursor,
            "events_sha256": digest.hexdigest(),
            "backend_records_sha256": identities.hexdigest(),
        }
    finally:
        connection.close()


def _pod_backup(pod: dict, stage: str, output: Path) -> dict:
    """SQLite online backup inside the existing owned Pod; copy bundle, never owner.header."""
    if output.exists() or output.is_symlink():
        raise target.AdmissionError("prior target backup retained; no overwrite")
    activation._write_once(
        output.with_name(output.name + ".intent"),
        {"pod_uid": pod["pod_uid"], "pv_uid": pod["pv_uid"]},
    )
    name = "operator-backup-" + stage
    args = [*target.KUBECTL, "-n", target.NAMESPACE, "exec", pod["pod_name"], "-c", "control", "--"]
    try:
        result = subprocess.run(
            [
                *args,
                "tessera",
                "backup",
                "--state-dir",
                "/state/control",
                "--output",
                "/state/" + name,
            ],
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise target.AdmissionError("target online backup unavailable; intent retained") from error
    if result.returncode:
        raise target.AdmissionError("target online backup failed; intent retained")
    if activation.ready_pod()["pod_uid"] != pod["pod_uid"]:
        raise target.AdmissionError("target Pod changed during backup")
    output.mkdir(mode=0o700)
    proc = subprocess.Popen(
        [*args, "tar", "-cf", "-", "-C", "/state/" + name, "."],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    seen: set[str] = set()
    total = 0
    watchdog = threading.Timer(120, proc.kill)
    watchdog.start()
    try:
        if proc.stdout is None:
            raise target.AdmissionError("target backup stream unavailable")
        with tarfile.open(fileobj=proc.stdout, mode="r|") as archive:
            for entry in archive:
                name = entry.name.removeprefix("./")
                if name in {"", "."} and entry.isdir():
                    continue
                parts = Path(name).parts
                if (
                    not parts
                    or any(part in {".", "..", "", "owner.header"} for part in parts)
                    or name.startswith("/")
                    or not (entry.isfile() or entry.isdir())
                    or not (
                        name in {"snapshot.sqlite3", "manifest.json", "objects"}
                        or (
                            name.startswith("objects/")
                            and all(
                                len(part) == 64 and all(c in "0123456789abcdef" for c in part)
                                for part in parts[1:]
                            )
                        )
                    )
                    or len(name) > 320
                    or name in seen
                ):
                    raise target.AdmissionError("unsafe target backup member")
                seen.add(name)
                total += entry.size
                if total > MAX_STREAM_BYTES or len(seen) > 20000:
                    raise target.AdmissionError("target backup exceeds limits")
                path = output.joinpath(*parts)
                if entry.isdir():
                    path.mkdir(mode=0o700, exist_ok=True)
                else:
                    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    source = archive.extractfile(entry)
                    if source is None:
                        raise target.AdmissionError("backup member unreadable")
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, "wb") as stream:
                        remaining = entry.size
                        while remaining:
                            chunk = source.read(min(65536, remaining))
                            if not chunk:
                                raise target.AdmissionError("partial target backup stream")
                            stream.write(chunk)
                            remaining -= len(chunk)
                        stream.flush()
                        os.fsync(stream.fileno())
        if proc.wait(timeout=30):
            raise target.AdmissionError("target backup stream failed")
    except BaseException as error:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if isinstance(error, target.AdmissionError):
            raise
        raise target.AdmissionError("target backup stream partial; retained") from error
    finally:
        watchdog.cancel()
    verified = target.validate_backup(output)
    if activation.ready_pod()["pod_uid"] != pod["pod_uid"]:
        raise target.AdmissionError("target Pod changed after backup")
    identity = _identity(output / "snapshot.sqlite3")
    return {
        "state": "independent_private_target_backup_verified",
        "pod_uid": pod["pod_uid"],
        "pv_uid": pod["pv_uid"],
        "backup_id": verified["backup_id"],
        "snapshot_sha256": verified["snapshot_sha256"],
        **identity,
    }
