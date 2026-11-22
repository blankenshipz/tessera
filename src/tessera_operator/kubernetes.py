"""Admission and provisioning helpers for a dedicated Docker Desktop control namespace.

The admission checks are read-only; the separately named commands that write do so
only as reviewed, separately approved stages (image, state-fencing, TLS).
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import time
import tomllib
import uuid
from pathlib import Path

import yaml
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from tessera_control.recovery import (
    _backup_state,
    backup_state,
)

from . import local_paths
from .upgrade_support import (
    AdmissionError as AdmissionError,
)
from .upgrade_support import (
    _docker as _docker,
)
from .upgrade_support import (
    _kubeconfig_identity as _kubeconfig_identity,
)
from .upgrade_support import (
    _kubectl as _kubectl,
)
from .upgrade_support import (
    _source_listener_absent as _source_listener_absent,
)
from .upgrade_support import (
    validate_backup as validate_backup,
)

KUBECONFIG = local_paths.KUBECONFIG
CONTEXT = "docker-desktop"
NAMESPACE = "tessera-local-persistent"
SOURCE = local_paths.STATE_DIR
REPOSITORY = local_paths.REPOSITORY
RESOURCE_PROFILE = REPOSITORY / "deploy/kubernetes/local-persistent-control.yaml"
RESOURCE_RECEIPT = SOURCE / "private/local-persistent-operator.json"
BACKUP_OUTPUT = SOURCE / "private/local-persistent-operator-backup"
QUARANTINE = SOURCE / "private/local-persistent-source-quarantine"
FENCED_BACKUP = SOURCE / "private/local-persistent-fenced-backup"
FENCE_MARKER = SOURCE / ".authority-moved"
ACTIVATED_MARKER = SOURCE / "private/local-persistent-activated"
DOCKERFILE = REPOSITORY / "deploy/kubernetes/local-persistent.Dockerfile"
IMAGE_TAG = "docker.io/library/tessera-local-persistent:reviewed"
# Namespaces from earlier deployments that are observed read-only; the operator lists more
# in TESSERA_LEGACY_NAMESPACES (comma separated).
LEGACY_NAMESPACES = (
    "tessera-lab",
    *filter(None, os.environ.get("TESSERA_LEGACY_NAMESPACES", "").split(",")),
)
KUBECTL = ("kubectl", "--kubeconfig", str(KUBECONFIG), "--context", CONTEXT, "--request-timeout=5s")


def _docker_identity() -> str:
    endpoint = _docker(
        "context", "inspect", "desktop-linux", "--format", "{{json .Endpoints.docker.Host}}"
    )
    if endpoint != f'"unix://{local_paths.DOCKER_DESKTOP_SOCKET}"':
        raise AdmissionError("Docker Desktop endpoint changed")
    return endpoint


def _source_digest() -> str:
    root = REPOSITORY
    digest = hashlib.sha256()
    mandatory = [
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "LICENSE",
        "MANIFEST.in",
        "CONTRIBUTING.md",
        "DESIGN.md",
        "CHANGELOG.md",
        "SECURITY.md",
        "AGENTS.md",
        "deploy/kubernetes/local-persistent.Dockerfile",
        "deploy/kubernetes/local-persistent.Dockerfile.dockerignore",
    ]
    expected = {
        "**",
        "!src/",
        "!src/**",
        "**/__pycache__/",
        "**/*.egg-info/",
        "**/*.pyc",
        "**/.DS_Store",
    }
    expected.update("!" + name for name in mandatory)
    items = set(mandatory)
    config = tomllib.loads((root / "pyproject.toml").read_text())
    groups = config["tool"]["setuptools"]["data-files"]
    for patterns in groups.values():
        for pattern in patterns:
            if pattern.startswith("/") or ".." in Path(pattern).parts or pattern.startswith("."):
                raise AdmissionError("unsafe declared image data file")
            for parent in Path(pattern).parents:
                if str(parent) != ".":
                    expected.add("!" + str(parent) + "/")
            expected.add("!" + pattern)
            found = glob.glob(pattern, root_dir=root)
            if not found:
                raise AdmissionError("declared package file unavailable from image context")
            items.update(found)
    for path in (root / "src").rglob("*"):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or any(
            part.endswith(".egg-info") for part in relative.parts
        ):
            continue
        if path.is_symlink():
            raise AdmissionError("reviewed source contains symlink")
        if path.is_file():
            if path.suffix not in {".py", ".yaml"}:
                raise AdmissionError("unexpected source file in image context")
            items.add(str(relative))
    ignore = (root / "deploy/kubernetes/local-persistent.Dockerfile.dockerignore").read_text()
    if set(ignore.splitlines()) != expected or len(ignore.splitlines()) != len(expected):
        raise AdmissionError("image context allowlist differs from declared package inputs")
    for name in sorted(items):
        path = root / name
        parents = Path(name).parents
        if (
            path.is_symlink()
            or not path.is_file()
            or any((root / parent).is_symlink() for parent in parents if str(parent) != ".")
            or any(part.startswith(".") for part in Path(name).parts)
            or any(part in {"owner.header", ".tessera", "__pycache__"} for part in Path(name).parts)
        ):
            raise AdmissionError("reviewed source contains unavailable file")
        digest.update(name.encode() + b"\x00" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _node_image_digest() -> str:
    listing = _docker(
        "exec", "desktop-control-plane", "ctr", "--namespace", "k8s.io", "images", "ls"
    )
    matches = [
        line.split()
        for line in listing.splitlines()
        if line.split() and line.split()[0] == IMAGE_TAG
    ]
    if len(matches) != 1:
        raise AdmissionError("exact node image tag unavailable")
    digests = [word for word in matches[0] if re.fullmatch(r"sha256:[a-f0-9]{64}", word)]
    if len(digests) != 1:
        raise AdmissionError("node manifest digest unavailable")
    return digests[0]


def image_plan() -> dict:
    """Hash reviewed build inputs and pin the explicit two local execution targets."""
    target = _kubeconfig_identity()
    endpoint = _docker_identity()
    observed = inspect()
    if not observed["namespace_absent_not_reserved"]:
        raise AdmissionError("dedicated namespace occupied")
    source = _source_digest()
    digest = hashlib.sha256(
        json.dumps(
            {"target": target, "docker": endpoint, "source": source, "image": IMAGE_TAG},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return {
        "plan_sha256": digest,
        "source_sha256": source,
        "image_tag": IMAGE_TAG,
        "context": CONTEXT,
        "docker_context": "desktop-linux",
        "namespace": NAMESPACE,
        "effects": "image build/load only; no namespace writes",
    }


def build_image(approved_plan_sha256: str) -> dict:
    """Build reviewed image on Desktop and import it into the local kind node.

    This never creates a namespace or runs a control service. If import fails,
    a retained image is inspected manually; no automatic retry or cleanup.
    """
    if not re.fullmatch(r"[a-f0-9]{64}", approved_plan_sha256):
        raise AdmissionError("exact human-approved plan digest required")
    if image_plan()["plan_sha256"] != approved_plan_sha256:
        raise AdmissionError("approved image plan changed")
    _docker(
        "buildx",
        "build",
        "--load",
        "--platform",
        "linux/amd64",
        "--file",
        str(DOCKERFILE),
        "--tag",
        IMAGE_TAG,
        str(REPOSITORY),
        timeout=900,
    )
    if image_plan()["plan_sha256"] != approved_plan_sha256:
        raise AdmissionError("build inputs or target changed; image retained, no import")
    exporter = importer = None
    try:
        exporter = subprocess.Popen(
            ["docker", "--context", "desktop-linux", "image", "save", IMAGE_TAG],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if exporter.stdout is None:
            raise AdmissionError("image export stream unavailable")
        importer = subprocess.Popen(
            [
                "docker",
                "--context",
                "desktop-linux",
                "exec",
                "-i",
                "desktop-control-plane",
                "ctr",
                "--namespace",
                "k8s.io",
                "images",
                "import",
                "-",
            ],
            stdin=exporter.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        exporter.stdout.close()
        importer.communicate(timeout=180)
        exporter.communicate(timeout=180)
    except (OSError, subprocess.TimeoutExpired) as error:
        for process in (importer, exporter):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()
        raise AdmissionError("node image import unavailable; reconcile retained image") from error
    if importer.returncode or exporter.returncode:
        raise AdmissionError("node image import failed; reconcile retained image")
    digest = _node_image_digest()
    pinned = "docker.io/library/tessera-local-persistent@" + digest
    _docker(
        "exec",
        "desktop-control-plane",
        "ctr",
        "--namespace",
        "k8s.io",
        "images",
        "tag",
        IMAGE_TAG,
        pinned,
    )
    listing = _docker(
        "exec", "desktop-control-plane", "ctr", "--namespace", "k8s.io", "images", "ls", "--quiet"
    )
    if pinned not in listing.splitlines() or image_plan()["plan_sha256"] != approved_plan_sha256:
        raise AdmissionError("pinned node image not verified; do not deploy")
    return {"image": pinned, "stage": "node_image_pinned_no_namespace_writes"}


def inspect() -> dict:
    """Read exact target/legacy namespace identities without reading their Secrets."""
    identity = _kubeconfig_identity()
    namespaces = _kubectl("get", "namespaces", "-o", "json")
    items = namespaces.get("items", [])
    if not isinstance(items, list):
        raise AdmissionError("invalid namespace inventory")
    names = {item.get("metadata", {}).get("name"): item for item in items}
    source = SOURCE
    if source.is_symlink() or not source.is_dir():
        raise AdmissionError("private source state directory unavailable")
    database = source / "tessera.sqlite3"
    owner = source / "owner.header"
    for path in (database, owner):
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise AdmissionError("private source state references unavailable")
    return {
        **identity,
        "namespace": NAMESPACE,
        "namespace_absent_not_reserved": NAMESPACE not in names,
        "legacy_namespace_uids": {
            name: names[name]["metadata"]["uid"] if name in names else None
            for name in LEGACY_NAMESPACES
        },
        "source_references_private": True,
        "effects": "none",
        "blocking_prerequisites": [
            "reviewed immutable node-resident image and exact digest",
            "fence source writer and clients; consistent backup and active-state transfer",
            "dedicated PVC ownership and named single-writer Deployment",
            "trusted localhost TLS gateway certificate and private CA reference",
            "explicit human-approved provisioning plan and resource-UID reconciliation",
        ],
    }


def _resource_docs(image: str) -> list[dict]:
    if not re.fullmatch(r"docker\.io/library/tessera-local-persistent@sha256:[a-f0-9]{64}", image):
        raise AdmissionError("exact immutable local image required")
    if RESOURCE_PROFILE.is_symlink():
        raise AdmissionError("resource profile must be regular")
    docs = list(yaml.safe_load_all(RESOURCE_PROFILE.read_text()))
    if [doc.get("kind") for doc in docs] != [
        "PersistentVolumeClaim",
        "Deployment",
        "Service",
        "NetworkPolicy",
    ]:
        raise AdmissionError("fixed profile changed")
    if any(doc["metadata"].get("namespace") != NAMESPACE for doc in docs):
        raise AdmissionError("profile namespace changed")
    deployment = docs[1]["spec"]
    if deployment.get("replicas") != 0 or deployment.get("strategy", {}).get("type") != "Recreate":
        raise AdmissionError("profile could start a writer")
    if docs[2]["spec"].get("type") != "ClusterIP":
        raise AdmissionError("profile could expose HTTP")
    if (
        docs[3]["metadata"].get("name") != "default-deny"
        or docs[3]["spec"].get("podSelector") != {}
        or set(docs[3]["spec"].get("policyTypes", [])) != {"Ingress", "Egress"}
    ):
        raise AdmissionError("deny-all declaration changed")
    container = deployment["template"]["spec"]["containers"][0]
    if container.get("command", [])[-2:] != ["--bind-host", "127.0.0.1"]:
        raise AdmissionError("control listener must remain Pod loopback-only")
    if (
        container["image"]
        != "docker.io/library/tessera-local-persistent@sha256:REPLACE_EXACT_NODE_DIGEST"
    ):
        raise AdmissionError("unexpected image placeholder")
    container["image"] = image
    for doc in docs:
        doc.setdefault("metadata", {}).setdefault("labels", {})["tessera.io/operator-scope"] = (
            "local-persistent"
        )
    return docs


def resource_plan(image: str) -> dict:
    """Immutable inert resource plan; no admission of occupied namespace."""
    docs = _resource_docs(image)
    observed = inspect()
    if not observed["namespace_absent_not_reserved"]:
        raise AdmissionError("dedicated namespace occupied; no adoption")
    endpoint = _docker_identity()
    listing = _docker(
        "exec", "desktop-control-plane", "ctr", "--namespace", "k8s.io", "images", "ls", "--quiet"
    )
    if image not in listing.splitlines():
        raise AdmissionError("pinned image absent from exact node")
    kube_system = _kubectl("get", "namespace", "kube-system", "-o", "json")
    cluster_uid = kube_system.get("metadata", {}).get("uid")
    if not isinstance(cluster_uid, str) or not cluster_uid:
        raise AdmissionError("cluster identity unavailable")
    target = {
        "context": CONTEXT,
        "server": observed["server"],
        "kubeconfig_sha256": observed["kubeconfig_sha256"],
        "kube_system_uid": cluster_uid,
        "legacy_uids": observed["legacy_namespace_uids"],
        "namespace": NAMESPACE,
        "image": image,
        "docker_endpoint": endpoint,
        "documents": docs,
    }
    digest = hashlib.sha256(
        json.dumps(target, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "plan_sha256": digest,
        "target": NAMESPACE,
        "image": image,
        "documents": docs,
        "kube_system_uid": cluster_uid,
        "resources": [
            "namespace",
            "pvc/tessera-control-state",
            "deployment/tessera-control (replicas=0)",
            "service/tessera-control (ClusterIP)",
            "networkpolicy/default-deny (enforcement unverified)",
        ],
        "effects": "named create-only; no Pod or source writer mutation",
    }


def _receipt_write(value: dict, *, new: bool = False) -> None:
    path = RESOURCE_RECEIPT
    if path.is_symlink() or path.parent.is_symlink() or not path.parent.is_dir():
        raise AdmissionError("operator receipt custody unavailable")
    if new:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        candidate = path
    else:
        candidate = path.with_name(path.name + ".new")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(candidate, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps(value, sort_keys=True).encode())
            stream.flush()
            os.fsync(stream.fileno())
        if not new:
            os.replace(candidate, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        candidate.unlink(missing_ok=True)
        raise


def provision_resources(approved_plan_sha256: str, image: str) -> dict:
    """Create only named inert resources; ambiguous partial work requires inspection."""
    if not re.fullmatch(r"[a-f0-9]{64}", approved_plan_sha256):
        raise AdmissionError("exact human-approved resource digest required")
    current = resource_plan(image)
    if approved_plan_sha256 != current["plan_sha256"]:
        raise AdmissionError("approved resource plan changed")
    if RESOURCE_RECEIPT.exists() or RESOURCE_RECEIPT.is_symlink():
        raise AdmissionError("prior operator attempt retained; no automatic adoption")
    nonce = uuid.uuid4().hex
    receipt = {
        "plan_sha256": approved_plan_sha256,
        "nonce": nonce,
        "namespace_uid": None,
        "created": [],
    }
    _receipt_write(receipt, new=True)  # durable intent BEFORE any cluster effect
    namespace = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": NAMESPACE,
            "labels": {
                "tessera.io/local-operator-nonce": nonce,
                "tessera.io/operator-scope": "local-persistent",
            },
        },
    }
    created = _kubectl("create", "-f", "-", "-o", "json", data=namespace)
    uid = created.get("metadata", {}).get("uid")
    if not isinstance(uid, str) or not uid:
        raise AdmissionError("namespace create uncertain; retained intent, inspect exact target")
    receipt["namespace_uid"] = uid
    _receipt_write(receipt)
    for doc in _resource_docs(image):
        current = _kubectl("get", "namespace", NAMESPACE, "-o", "json")
        meta = current.get("metadata", {})
        if (
            meta.get("uid") != uid
            or meta.get("labels", {}).get("tessera.io/local-operator-nonce") != nonce
        ):
            raise AdmissionError("namespace identity changed; stop")
        resource = _kubectl("-n", NAMESPACE, "create", "-f", "-", "-o", "json", data=doc)
        resource_uid = resource.get("metadata", {}).get("uid")
        if not isinstance(resource_uid, str) or not resource_uid:
            raise AdmissionError("resource create uncertain; retained intent, inspect exact target")
        receipt["created"].append(
            {"kind": doc["kind"], "name": doc["metadata"]["name"], "uid": resource_uid}
        )
        _receipt_write(receipt)
    return {
        "namespace": NAMESPACE,
        "namespace_uid": uid,
        "created": receipt["created"],
        "stage": "inert_resources_only; no Pod or client endpoint",
    }


def backup_plan() -> dict:
    """Review a consistent online snapshot operation, not an active-state copy."""
    if SOURCE.is_symlink() or not SOURCE.is_dir():
        raise AdmissionError("source state unavailable")
    for path in (SOURCE, SOURCE / "tessera.sqlite3", SOURCE / "owner.header", BACKUP_OUTPUT.parent):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_mode & 0o077:
            raise AdmissionError("private source or backup custody unavailable")
    if (SOURCE / "owner.header").is_file() is False:
        raise AdmissionError("owner credential reference unavailable")
    if BACKUP_OUTPUT.exists() or BACKUP_OUTPUT.is_symlink():
        raise AdmissionError("backup output occupied; no overwrite")
    db = (SOURCE / "tessera.sqlite3").stat()
    if not stat.S_ISREG(db.st_mode):
        raise AdmissionError("database reference unavailable")
    target = {
        "source": str(SOURCE),
        "output": str(BACKUP_OUTPUT),
        "database_inode": db.st_ino,
        "owner_inode": (SOURCE / "owner.header").stat().st_ino,
        "scope": "consistent online snapshot; no owner.header transfer or writer stop",
    }
    return {
        **target,
        "plan_sha256": hashlib.sha256(
            json.dumps(target, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def backup_source(expected_plan_sha256: str) -> dict:
    # The user already authorized backup. This is a stale-input fence, not a
    # second human approval gate or control-plane authority grant.
    if not re.fullmatch(r"[a-f0-9]{64}", expected_plan_sha256):
        raise AdmissionError("exact backup plan integrity digest required")
    if backup_plan()["plan_sha256"] != expected_plan_sha256:
        raise AdmissionError("backup plan drifted")
    try:
        backup_state(SOURCE, BACKUP_OUTPUT)
    except Exception as error:
        raise AdmissionError("private backup failed; retain output for inspection") from error
    result = validate_backup()
    return {"backup_directory": str(BACKUP_OUTPUT), **result}


def cutover_preflight(*, require_writer: bool = True) -> dict:
    """Read-only source/PVC identity gate. Never signal or copy credentials."""
    backup = validate_backup()
    receipt = RESOURCE_RECEIPT
    if receipt.is_symlink() or not receipt.is_file() or receipt.stat().st_mode & 0o077:
        raise AdmissionError("private resource receipt unavailable")
    try:
        record = json.loads(receipt.read_bytes())
    except (ValueError, OSError) as error:
        raise AdmissionError("resource receipt invalid") from error
    uid = record.get("namespace_uid")
    expected = [
        ("PersistentVolumeClaim", "tessera-control-state"),
        ("Deployment", "tessera-control"),
        ("Service", "tessera-control"),
        ("NetworkPolicy", "default-deny"),
    ]
    created = record.get("created", [])
    if (
        not isinstance(uid, str)
        or not uid
        or not isinstance(created, list)
        or [(item.get("kind"), item.get("name")) for item in created] != expected
    ):
        raise AdmissionError("inert resource stage incomplete")
    target = _kubectl("get", "namespace", NAMESPACE, "-o", "json")
    meta = target.get("metadata", {})
    if meta.get("uid") != uid or meta.get("labels", {}).get(
        "tessera.io/local-operator-nonce"
    ) != record.get("nonce"):
        raise AdmissionError("namespace ownership drifted")
    deployment = _kubectl("-n", NAMESPACE, "get", "deployment", "tessera-control", "-o", "json")
    if (
        deployment.get("metadata", {}).get("uid") != created[1]["uid"]
        or deployment.get("spec", {}).get("replicas") != 0
    ):
        raise AdmissionError("target writer already active")
    pvc = _kubectl("-n", NAMESPACE, "get", "pvc", "tessera-control-state", "-o", "json")
    if pvc.get("metadata", {}).get("uid") != created[0]["uid"]:
        raise AdmissionError("PVC identity drifted")
    if pvc.get("status", {}).get("phase") != "Bound":
        raise AdmissionError("dedicated PVC not bound")
    service = _kubectl("-n", NAMESPACE, "get", "service", "tessera-control", "-o", "json")
    if (
        service.get("metadata", {}).get("uid") != created[2]["uid"]
        or service.get("spec", {}).get("type") != "ClusterIP"
    ):
        raise AdmissionError("Service identity or exposure drifted")
    pods = _kubectl("-n", NAMESPACE, "get", "pods", "-o", "json")
    if pods.get("items") != []:
        raise AdmissionError("target namespace has an active or unknown Pod")
    if not require_writer:
        return {"namespace_uid": uid, "backup_sha256": backup["snapshot_sha256"]}
    # A nonzero or unexpected listener is not permission to kill anything.
    listener = subprocess.run(
        ["lsof", "-nP", "-iTCP:8765", "-sTCP:LISTEN", "-Fp"],
        capture_output=True,
        timeout=5,
        check=False,
    )
    pids = re.findall(r"(?m)^p([0-9]+)$", listener.stdout.decode("ascii", "replace"))
    if listener.returncode or len(pids) != 1:
        raise AdmissionError("exact source listener unavailable")
    process = subprocess.run(
        ["ps", "-p", pids[0], "-o", "uid=,command="], capture_output=True, timeout=5, check=False
    )
    try:
        parts = process.stdout.decode("utf-8").strip().split(None, 1)
        arguments = shlex.split(parts[1])
        if (
            process.returncode
            or int(parts[0]) != os.getuid()
            or len(arguments) < 2
            or Path(arguments[1]).name != "tessera"
            or (REPOSITORY / arguments[1]).resolve() != (REPOSITORY / ".venv/bin/tessera").resolve()
            or "serve" not in arguments
            or arguments[arguments.index("--state-dir") + 1] != str(SOURCE)
            or arguments[arguments.index("--port") + 1] != "8765"
            or (
                "--bind-host" in arguments
                and arguments[arguments.index("--bind-host") + 1] != "127.0.0.1"
            )
        ):
            raise ValueError()
    except (ValueError, IndexError, UnicodeError) as error:
        raise AdmissionError("source writer identity mismatch") from error
    return {
        "source_pid_observed_not_fenced": int(pids[0]),
        "namespace_uid": uid,
        "backup_sha256": backup["snapshot_sha256"],
        "state": "preflight_only; source writer still active; transfer and TLS unavailable",
    }


def _exact_process_start(pid: int) -> bytes:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "uid=,lstart=,command="],
        capture_output=True,
        timeout=5,
        check=False,
    )
    if result.returncode or len(result.stdout) > 8192:
        raise AdmissionError("source process identity unavailable")
    text = result.stdout.decode("utf-8", "replace")
    if not re.match(rf"\s*{os.getuid()}\s+", text) or "--state-dir " + str(SOURCE) not in text:
        raise AdmissionError("source process identity drifted")
    return result.stdout


def _quarantine_nodes() -> list[str]:
    names = ["tessera.sqlite3", "owner.header", "project-artifacts"]
    names += [
        name
        for name in ("tessera.sqlite3-wal", "tessera.sqlite3-shm", "tessera.sqlite3-journal")
        if (SOURCE / name).exists() or (SOURCE / name).is_symlink()
    ]
    for name in names:
        path = SOURCE / name
        if path.is_symlink() or not path.exists() or path.stat().st_mode & 0o077:
            raise AdmissionError("source state node unsafe")
        if name == "project-artifacts":
            if not path.is_dir():
                raise AdmissionError("artifact source not a directory")
        elif not path.is_file():
            raise AdmissionError("source state node not regular")
    return names


def _serve_candidates() -> dict[int, tuple[int, str]]:
    """Classify Tessera CLI serves, without treating unrelated argv as authority."""
    try:
        processes = subprocess.run(
            ["ps", "-axo", "uid=,pid=,command="], capture_output=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AdmissionError("source process inventory unavailable") from error
    if processes.returncode or len(processes.stdout) > 4_000_000:
        raise AdmissionError("source process inventory unavailable")
    candidates = {}
    for line in processes.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            raise AdmissionError("source process inventory incomplete")
        uid, pid, command = parts
        head = command.split(None, 1)[0]
        exe = Path(head).name.lower()
        python_cli = re.fullmatch(r"python(?:3(?:\.\d+)?)?", exe) is not None
        direct_cli = exe == "tessera"
        if not direct_cli and not python_cli:
            continue
        # An unparseable CLI command that might be a serve cannot be skipped.
        try:
            argv = shlex.split(command)
        except ValueError as error:
            if "serve" in command or "tessera" in command:
                raise AdmissionError("Tessera serve command unparseable") from error
            continue
        cli = direct_cli
        if python_cli:
            cli = any(Path(arg).name == "tessera" for arg in argv[1:]) or any(
                argv[i] == "-m" and argv[i + 1] in ("tessera_control", "tessera_control.cli")
                for i in range(1, len(argv) - 1)
            )
        if not cli or "serve" not in argv:
            continue
        if not uid.isdecimal() or not pid.isdecimal() or int(pid) in candidates:
            raise AdmissionError("Tessera serve process identity unavailable")
        candidates[int(pid)] = (int(uid), command)
        values = []
        for i, arg in enumerate(argv):
            if arg == "--state-dir":
                if i + 1 >= len(argv):
                    raise AdmissionError("Tessera serve state directory ambiguous")
                values.append(argv[i + 1])
            elif arg.startswith("--state-dir="):
                values.append(arg.split("=", 1)[1])
        if len(values) != 1 or not values[0] or values[0].startswith("-"):
            raise AdmissionError("Tessera serve state directory ambiguous")
        state = Path(values[0])
        if not state.is_absolute():
            try:
                cwd = subprocess.run(
                    ["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise AdmissionError("Tessera serve relative state directory uncertain") from error
            paths = [
                row[1:]
                for row in cwd.stdout.decode("utf-8", "replace").splitlines()
                if row.startswith("n")
            ]
            if cwd.returncode or len(paths) != 1 or not Path(paths[0]).is_absolute():
                raise AdmissionError("Tessera serve relative state directory uncertain")
            state = Path(paths[0]) / state
        if state.resolve() == SOURCE.resolve():
            raise AdmissionError("source writer candidate present; no absent-writer fence")
    return candidates


def _absent_writer_guard() -> None:
    """Best-effort observation, not exclusion of arbitrary same-UID writers."""
    _source_listener_absent()
    observed = _serve_candidates()
    if _serve_candidates() != observed:
        raise AdmissionError("Tessera serve process identity changed")
    _source_listener_absent()


def _source_node_identity(names: list[str]) -> dict[str, tuple[int, int, int, int]]:
    if SOURCE.is_symlink() or not SOURCE.is_dir() or SOURCE.stat().st_mode & 0o077:
        raise AdmissionError("source directory unsafe")
    if (
        QUARANTINE.parent.is_symlink()
        or not QUARANTINE.parent.is_dir()
        or QUARANTINE.parent.stat().st_mode & 0o077
    ):
        raise AdmissionError("private quarantine parent unsafe")
    if _quarantine_nodes() != names:
        raise AdmissionError("source inventory changed")
    identity = {}
    for name in names:
        path = SOURCE / name
        info = path.lstat()
        identity[name] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        result = subprocess.run(
            ["lsof", "-nP", "--", str(path)], capture_output=True, timeout=5, check=False
        )
        if result.returncode not in (0, 1) or result.stdout.strip():
            raise AdmissionError("source file has open handles; no quarantine")
    return identity


def fence_absent_source() -> dict:
    """Quarantine an observed-inactive source; retain all partial intent on races."""
    admission = cutover_preflight(require_writer=False)
    if any(
        path.exists() or path.is_symlink() for path in (FENCE_MARKER, QUARANTINE, FENCED_BACKUP)
    ):
        raise AdmissionError("prior fence attempt retained; inspect before continuing")
    _absent_writer_guard()
    names = _quarantine_nodes()
    identity = _source_node_identity(names)
    _absent_writer_guard()
    if _source_node_identity(names) != identity:
        raise AdmissionError("source state changed before fence")
    QUARANTINE.mkdir(mode=0o700)
    marker_fd = os.open(FENCE_MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(marker_fd, "wb") as stream:
        stream.write(
            json.dumps(
                {
                    "namespace_uid": admission["namespace_uid"],
                    "mode": "observed_absent_writer",
                    "names": names,
                },
                sort_keys=True,
            ).encode()
        )
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(SOURCE, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    # A race after the durable marker is not silently rolled back.
    _absent_writer_guard()
    if _source_node_identity(names) != identity:
        raise AdmissionError("source state changed after marker; intent retained")
    for name in names:
        os.rename(SOURCE / name, QUARANTINE / name)
        moved = (QUARANTINE / name).lstat()
        if (moved.st_dev, moved.st_ino, moved.st_size, moved.st_mtime_ns) != identity[name]:
            raise AdmissionError("source changed during quarantine; intent retained")
    for path in (SOURCE, QUARANTINE):
        directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    _absent_writer_guard()
    if any((SOURCE / name).exists() or (SOURCE / name).is_symlink() for name in names):
        raise AdmissionError("old source node reappeared; intent retained")
    if any(
        (SOURCE / ("tessera.sqlite3" + suffix)).exists()
        or (SOURCE / ("tessera.sqlite3" + suffix)).is_symlink()
        for suffix in ("-wal", "-shm", "-journal")
    ):
        raise AdmissionError("new source sidecar appeared; intent retained")
    try:
        _backup_state(QUARANTINE, FENCED_BACKUP)
        verified = validate_backup(FENCED_BACKUP)
    except Exception as error:
        raise AdmissionError("post-fence snapshot unavailable; quarantine retained") from error
    _absent_writer_guard()
    return {
        "state": "source_quarantined_target_still_inactive",
        "namespace_uid": admission["namespace_uid"],
        "fenced_snapshot_sha256": verified["snapshot_sha256"],
        "event_watermark": verified["event_watermark"],
        "owner_reference": str(QUARANTINE / "owner.header"),
    }


def fence_source() -> dict:
    """Stop exact local writer, quarantine its authority, and snapshot without source writes.

    A partial failure retains the marker and quarantine; never unconditionally
    restore while a target writer may have become active.
    """
    admission = cutover_preflight()
    if (
        FENCE_MARKER.exists()
        or FENCE_MARKER.is_symlink()
        or QUARANTINE.exists()
        or QUARANTINE.is_symlink()
        or FENCED_BACKUP.exists()
    ):
        raise AdmissionError("prior fence attempt retained; inspect before continuing")
    names = _quarantine_nodes()
    pid = admission["source_pid_observed_not_fenced"]
    observed = _exact_process_start(pid)
    # Revalidate immediately before signalling, then require exit. No SIGKILL or
    # fallback against a process whose ownership may have changed.
    if _exact_process_start(pid) != observed:
        raise AdmissionError("source process changed before stop")
    os.kill(pid, signal.SIGTERM)
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        raise AdmissionError("source process did not exit; no quarantine")
    _source_listener_absent()
    for name in names:
        path = SOURCE / name
        result = subprocess.run(
            ["lsof", "-nP", "--", str(path)], capture_output=True, timeout=5, check=False
        )
        if result.returncode not in (0, 1) or result.stdout.strip():
            raise AdmissionError("source file has open handles; no quarantine")
    QUARANTINE.mkdir(mode=0o700)
    marker_fd = os.open(FENCE_MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(marker_fd, "wb") as stream:
        stream.write(
            json.dumps(
                {"namespace_uid": admission["namespace_uid"], "source_pid": pid, "names": names},
                sort_keys=True,
            ).encode()
        )
        stream.flush()
        os.fsync(stream.fileno())
    for name in names:
        os.rename(SOURCE / name, QUARANTINE / name)
    directory = os.open(SOURCE, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    _source_listener_absent()
    if (SOURCE / "tessera.sqlite3").exists() or (SOURCE / "tessera.sqlite3").is_symlink():
        raise AdmissionError("old source database reappeared; stop")
    try:
        _backup_state(QUARANTINE, FENCED_BACKUP)
        verified = validate_backup(FENCED_BACKUP)
    except Exception as error:
        raise AdmissionError("post-fence snapshot unavailable; quarantine retained") from error
    return {
        "state": "source_quarantined_target_still_inactive",
        "namespace_uid": admission["namespace_uid"],
        "fenced_snapshot_sha256": verified["snapshot_sha256"],
        "event_watermark": verified["event_watermark"],
        "owner_reference": str(QUARANTINE / "owner.header"),
    }


def rollback_source() -> dict:
    """Restore only a quarantined source while exact target remains inactive."""
    if ACTIVATED_MARKER.exists() or ACTIVATED_MARKER.is_symlink():
        raise AdmissionError("new authority may have written; rollback prohibited")
    if (
        FENCE_MARKER.is_symlink()
        or not FENCE_MARKER.is_file()
        or FENCE_MARKER.stat().st_mode & 0o077
    ):
        raise AdmissionError("private fence marker unavailable")
    try:
        intent = json.loads(FENCE_MARKER.read_bytes())
    except (OSError, ValueError) as error:
        raise AdmissionError("fence marker invalid") from error
    names = intent.get("names")
    if (
        not isinstance(names, list)
        or not {"tessera.sqlite3", "owner.header", "project-artifacts"}.issubset(names)
        or any(
            name
            not in {
                "tessera.sqlite3",
                "owner.header",
                "project-artifacts",
                "tessera.sqlite3-wal",
                "tessera.sqlite3-shm",
                "tessera.sqlite3-journal",
            }
            for name in names
        )
    ):
        raise AdmissionError("unexpected quarantine inventory")
    target = _kubectl("get", "namespace", NAMESPACE, "-o", "json")
    if target.get("metadata", {}).get("uid") != intent.get("namespace_uid"):
        raise AdmissionError("target namespace identity drifted")
    deploy = _kubectl("-n", NAMESPACE, "get", "deployment", "tessera-control", "-o", "json")
    pods = _kubectl("-n", NAMESPACE, "get", "pods", "-o", "json")
    if (
        deploy.get("spec", {}).get("replicas") != 0
        or pods.get("items") != []
        or deploy.get("metadata", {}).get("generation") != 1
    ):
        raise AdmissionError("target might have written; rollback prohibited")
    _source_listener_absent()
    if QUARANTINE.is_symlink() or not QUARANTINE.is_dir():
        raise AdmissionError("quarantine unavailable")
    for name in names:
        origin = QUARANTINE / name
        if origin.is_symlink() or not origin.exists() or origin.stat().st_mode & 0o077:
            raise AdmissionError("quarantine file unavailable or unsafe")
        if (SOURCE / name).exists() or (SOURCE / name).is_symlink():
            raise AdmissionError("old source path occupied; no overwrite")
    for name in names:
        os.rename(QUARANTINE / name, SOURCE / name)
    directory = os.open(SOURCE, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    _source_listener_absent()
    FENCE_MARKER.unlink()
    return {
        "state": "old_source_restored_not_started",
        "target_namespace_uid": intent["namespace_uid"],
    }


def create_server() -> FastMCP:
    server = FastMCP("Tessera local Kubernetes human operator admission", log_level="CRITICAL")
    from .private_import import register

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    )
    def local_control_inspect() -> dict:
        """Read fixed Docker Desktop target and source metadata; never deploy or read secrets."""
        return inspect()

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    )
    def local_image_plan() -> dict:
        """Dry-run reviewed source/cluster/Docker identity digest; no build or apply."""
        return image_plan()

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    )
    def local_image_build(approved_plan_sha256: str) -> dict:
        """Build/import fixed image only after an external human approves exact plan digest."""
        return build_image(approved_plan_sha256)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    )
    def local_resource_plan(image: str) -> dict:
        """Plan named inert Kubernetes resources for an exact pinned node image."""
        return resource_plan(image)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    )
    def local_resource_provision(approved_plan_sha256: str, image: str) -> dict:
        """Require separate human resource-plan digest; create-only, retain partial intents."""
        return provision_resources(approved_plan_sha256, image)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    )
    def local_backup_plan() -> dict:
        """Plan one consistent private source snapshot; no credential disclosure or writes."""
        return backup_plan()

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    )
    def local_backup_source(expected_plan_sha256: str) -> dict:
        """Authorized online backup with stale-plan fence; no extra human approval."""
        return backup_source(expected_plan_sha256)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    )
    def local_backup_verify() -> dict:
        """Recheck fixed backup DB/object hashes without reading owner credential."""
        return validate_backup()

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    )
    def local_cutover_preflight() -> dict:
        """Check backup, exact source writer and inert PVC/namespace UIDs; no effect."""
        return cutover_preflight()

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
    )
    def local_cutover_fence() -> dict:
        """Stop exact source, quarantine authority, snapshot; never start target."""
        return fence_source()

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
    )
    def local_cutover_fence_absent_writer() -> dict:
        """Fence observed-inactive source without signals; retain races for inspection."""
        return fence_absent_source()

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
    )
    def local_cutover_rollback() -> dict:
        """Restore quarantined old state only while target has zero replicas and no Pods."""
        return rollback_source()

    register(server)
    return server


def main(argv=None) -> None:
    argparse.ArgumentParser(
        description="Fixed-target local Kubernetes operator (admission, backup, activation)"
    ).parse_args(argv)
    local_paths.require_repository()
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
