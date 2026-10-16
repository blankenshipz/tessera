"""One bounded same-PVC image update; no bootstrap, DB restore or authority issuance.

Candidate build receipt production is a separate, still-required build checkpoint.
Only the trusted operator can write it; MCP never accepts an attestation boolean.
"""

import fcntl
import hashlib
import inspect
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import time
from contextlib import contextmanager

from mcp.types import ToolAnnotations

from . import upgrade_support as activation
from . import upgrade_support as admission
from . import upgrade_support as recovery
from . import upgrade_support as target

ROOT = target.SOURCE / "private/local-control-upgrade"
IMAGE = r"docker\.io/library/tessera-local-persistent@sha256:[a-f0-9]{64}"
IDENTITY = ("namespace_uid", "deployment_uid", "pvc_uid", "pv_uid")
MIN_NODE_FREE = 5 * 1024**3
MAX_BACKUP_AGE_SECONDS = 300


def _now():
    return time.time()


def _logical_head(db):
    # All SQL state, not just event cursor: includes credentials, delegations and
    # idempotency. Only a digest leaves this function; SQL values are never logged.
    db.execute("BEGIN")
    digest = hashlib.sha256()
    for statement in db.iterdump():
        encoded = statement.encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _snapshot_head(path):
    with sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True) as db:
        return _logical_head(db)


def _control_head(pod):
    code = "import hashlib,sqlite3\n" + inspect.getsource(_logical_head)
    code += (
        "\nwith sqlite3.connect('file:/state/control/tessera.sqlite3?mode=ro',uri=True) as db:\n"
    )
    code += " print(_logical_head(db))\n"
    try:
        result = subprocess.run(
            [
                *target.KUBECTL,
                "-n",
                target.NAMESPACE,
                "exec",
                pod["pod_name"],
                "-c",
                "control",
                "--",
                "python",
                "-c",
                code,
            ],
            capture_output=True,
            timeout=15,
        )
        head = result.stdout.decode().strip()
        if result.returncode or not re.fullmatch(r"[a-f0-9]{64}", head):
            raise ValueError()
        return head
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise target.AdmissionError("backup control state unverifiable") from None


def _backup_admission(directory, saved, backup):
    freshness = admission._receipt(directory / "backup-freshness.json")
    age = _now() - freshness["started_at"]
    if not 0 <= age <= MAX_BACKUP_AGE_SECONDS:
        raise target.AdmissionError(
            "backup stale; prepare a new operation without overwriting proof"
        )
    if (
        freshness.get("plan_sha256") != saved["plan_sha256"]
        or freshness.get("snapshot_sha256") != backup["snapshot_sha256"]
    ):
        raise target.AdmissionError("backup freshness binding changed")
    observed = _control_head(saved["pod"])
    if not isinstance(observed, str) or not re.fullmatch(r"[a-f0-9]{64}", observed):
        raise target.AdmissionError("backup control state unverifiable")
    if observed != freshness.get("control_head"):
        raise target.AdmissionError("backup control state changed; prepare a new operation")
    # Also reject if the bounded observation itself consumed the remaining window.
    if not 0 <= _now() - freshness["started_at"] <= MAX_BACKUP_AGE_SECONDS:
        raise target.AdmissionError("backup stale after control observation")


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _operation(operation):
    if not re.fullmatch(r"[a-f0-9]{32}", operation):
        raise target.AdmissionError("exact upgrade operation ID required")
    return ROOT / operation


def _private_root():
    from .salad import _directory

    parent = _directory(ROOT.parent)
    os.close(parent)
    if not ROOT.exists():
        ROOT.mkdir(mode=0o700)
    info = ROOT.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise target.AdmissionError("private upgrade directory required")


@contextmanager
def _locked():
    _private_root()
    fd = os.open(ROOT / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise target.AdmissionError("private upgrade lock required")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def candidate_inventory() -> dict:
    """Published tree plus explicit existing active store fence/build-recipe overlays.

    Untracked migration/operator files and dirty pyproject are NOT image inputs.
    A future builder must materialize this inventory, not build the dirty checkout.
    """

    def git(*args):
        r = subprocess.run(["git", *args], cwd=target.REPOSITORY, capture_output=True, timeout=10)
        if r.returncode:
            raise target.AdmissionError("candidate Git inventory unavailable")
        return r.stdout

    commit = git("rev-parse", "HEAD").decode().strip()
    tree = git("ls-tree", "-rz", commit)
    blobs = []
    for entry in tree.split(b"\0"):
        if entry:
            meta, name = entry.split(b"\t", 1)
            mode, kind, blob = meta.decode().split()
            if kind != "blob" or mode not in {"100644", "100755"}:
                raise target.AdmissionError("unsupported candidate Git entry")
            blobs.append([name.decode(), blob])
    overlays = {}
    for name in (
        "src/tessera_control/store.py",
        "deploy/kubernetes/local-persistent.Dockerfile",
        "deploy/kubernetes/local-persistent.Dockerfile.dockerignore",
    ):
        path = target.REPOSITORY / name
        if path.is_symlink() or not path.is_file():
            raise target.AdmissionError("candidate overlay unavailable")
        with path.open("rb") as stream:
            overlays[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    value = {"git_commit": commit, "tracked_blobs": blobs, "overlays": overlays}
    return value | {"inventory_sha256": _digest(value)}


def _candidate(image):
    from .build_receipt import verify_receipt

    if not re.fullmatch(IMAGE, image):
        raise target.AdmissionError("immutable local control image required")
    receipt = admission._receipt(ROOT / "candidates" / (image.rsplit(":", 1)[1] + ".json"))
    inventory = candidate_inventory()
    if (
        receipt.get("image") != image
        or receipt.get("inventory_sha256") != inventory["inventory_sha256"]
        or receipt.get("git_commit") != inventory["git_commit"]
    ):
        raise target.AdmissionError("candidate build receipt or source changed")
    verify_receipt(receipt, inventory)
    return receipt, inventory


def _saved(operation):
    value = admission._receipt(_operation(operation) / "plan.json")
    digest = value.get("plan_sha256")
    if (
        value.get("operation") != operation
        or _digest({k: v for k, v in value.items() if k != "plan_sha256"}) != digest
    ):
        raise target.AdmissionError("upgrade plan receipt changed")
    return value


def _backend(pod):
    # Only schema/incarnation/store-source hashes leave the owned Pod, never DB bytes or keys.
    code = """import json,hashlib,sqlite3,http.client
import tessera_control.store as s
with sqlite3.connect('file:/state/control/tessera.sqlite3?mode=ro',uri=True) as db:
 schema=db.execute('SELECT schema_version FROM schema_meta WHERE singleton=1').fetchone()[0]
 incarnation,state=db.execute(
  'SELECT incarnation,state FROM control_recovery WHERE singleton=1'
 ).fetchone()
with open(s.__file__,'rb') as f: store=hashlib.file_digest(f,'sha256').hexdigest()
c=http.client.HTTPConnection('127.0.0.1',8765,timeout=2)
try:
 c.request('GET','/health/ready')
 http=c.getresponse().status
finally: c.close()
print(json.dumps(dict(schema=schema,incarnation=incarnation,recovery_state=state,store_sha256=store,application_http=http)))
"""
    r = subprocess.run(
        [
            *target.KUBECTL,
            "-n",
            target.NAMESPACE,
            "exec",
            pod["pod_name"],
            "-c",
            "control",
            "--",
            "python",
            "-c",
            code,
        ],
        capture_output=True,
        timeout=10,
    )
    if r.returncode or len(r.stdout) > 4096:
        raise target.AdmissionError("bounded backend identity unavailable")
    return json.loads(r.stdout)


def _capacity():
    if shutil.disk_usage(ROOT.parent).free < 5 * 1024**3:
        raise target.AdmissionError("host receipt reserve insufficient")
    raw = target._docker("exec", "desktop-control-plane", "df", "-Pk", "/var/lib/containerd")
    if int(raw.splitlines()[-1].split()[3]) * 1024 < MIN_NODE_FREE:
        raise target.AdmissionError("node build/import reserve insufficient")


def _deployment():
    return target._kubectl(
        "-n", target.NAMESPACE, "get", "deployment", "tessera-control", "-o", "json"
    )


def _snapshot():
    base = admission._receipt(activation.INTENT)
    if (ROOT / "current.json").exists():
        raise target.AdmissionError(
            "one upgrade already admitted; separate next-generation admission required"
        )
    pod = activation.ready_pod()
    deploy = _deployment()
    spec = deploy["spec"]
    volumes = spec["template"]["spec"]["volumes"]
    if (
        deploy["metadata"]["uid"] != base["deployment_uid"]
        or spec.get("replicas") != 1
        or spec.get("strategy") != {"type": "Recreate"}
        or spec["template"]["spec"]["containers"][0]["image"] != base["image"]
        or [v for v in volumes if v.get("name") == "state"]
        != [{"name": "state", "persistentVolumeClaim": {"claimName": "tessera-control-state"}}]
    ):
        raise target.AdmissionError("owned single-writer Recreate target changed")
    return base, pod, deploy


def plan(operation: str, image: str) -> dict:
    _operation(operation)
    base, pod, deploy = _snapshot()
    candidate, inventory = _candidate(image)
    backend = _backend(pod)
    if (
        backend["recovery_state"] != "active"
        or backend.get("application_http") != 200
        or backend["schema"] != candidate.get("schema")
        or backend["store_sha256"] != inventory["overlays"]["src/tessera_control/store.py"]
    ):
        raise target.AdmissionError(
            "schema or existing store fence overlay mismatch; no migration permitted"
        )
    listing = target._docker(
        "exec", "desktop-control-plane", "ctr", "--namespace", "k8s.io", "images", "ls", "--quiet"
    )
    if (
        image not in listing.splitlines()
        or base["image"] not in listing.splitlines()
        or image == base["image"]
    ):
        raise target.AdmissionError("new and retained rollback image references required")
    value = {
        "operation": operation,
        **{k: base[k] for k in IDENTITY},
        "old_image": base["image"],
        "new_image": image,
        "resource_version": deploy["metadata"]["resourceVersion"],
        "spec_sha256": _digest(deploy["spec"]),
        "pod": pod,
        "backend": backend,
        "inventory_sha256": inventory["inventory_sha256"],
    }
    return value | {"plan_sha256": _digest(value)}


def prepare(operation: str, image: str, expected_plan_sha256: str) -> dict:
    with _locked():
        if (ROOT / "apply-claim.json").exists():
            raise target.AdmissionError("prior apply intent; reconcile only")
        current = plan(operation, image)
        if current["plan_sha256"] != expected_plan_sha256:
            raise target.AdmissionError("upgrade plan stale")
        _capacity()
        directory = _operation(operation)
        try:
            directory.mkdir(mode=0o700)  # Create-only; partial backup is retained.
        except FileExistsError:
            raise target.AdmissionError(
                "prior upgrade preparation retained; no overwrite"
            ) from None
        activation._write_once(directory / "plan.json", current)
        inventory = candidate_inventory()
        activation._write_once(directory / "inventory.json", inventory)
        started = _now()
        before = _control_head(current["pod"])
        backup = recovery._pod_backup(current["pod"], "upgrade-" + operation, directory / "backup")
        if (
            backup["incarnation"] != current["backend"]["incarnation"]
            or backup["recovery_state"] != "active"
        ):
            raise target.AdmissionError("fresh backup backend identity changed")
        activation._write_once(directory / "backup.json", backup)
        head = _snapshot_head(directory / "backup/snapshot.sqlite3")
        if (
            not isinstance(head, str)
            or not re.fullmatch(r"[a-f0-9]{64}", head)
            or head != before
            or head != _control_head(current["pod"])
        ):
            raise target.AdmissionError("backup control state changed during preparation")
        if not 0 <= _now() - started <= MAX_BACKUP_AGE_SECONDS:
            raise target.AdmissionError("backup stale during preparation; retained")
        activation._write_once(
            directory / "backup-freshness.json",
            {
                "started_at": started,
                "control_head": head,
                "snapshot_sha256": backup["snapshot_sha256"],
                "plan_sha256": current["plan_sha256"],
            },
        )
        return {
            "state": "fresh_backup_prepared_no_image_change",
            "operation": operation,
            "plan_sha256": current["plan_sha256"],
            "backup_id": backup["backup_id"],
        }


def apply(operation: str, expected_plan_sha256: str) -> dict:
    with _locked():
        directory = _operation(operation)
        saved = _saved(operation)
        if (ROOT / "apply-claim.json").exists():
            raise target.AdmissionError("prior apply intent; reconcile only")
        if (
            saved["plan_sha256"] != expected_plan_sha256
            or plan(operation, saved["new_image"]) != saved
        ):
            raise target.AdmissionError("upgrade target or candidate stale")
        _capacity()
        backup = admission._receipt(directory / "backup.json")
        verified = target.validate_backup(directory / "backup")
        if (
            backup["pod_uid"] != saved["pod"]["pod_uid"]
            or backup["pv_uid"] != saved["pv_uid"]
            or verified["snapshot_sha256"] != backup["snapshot_sha256"]
        ):
            raise target.AdmissionError("fresh upgrade backup binding changed")
        _backup_admission(directory, saved, backup)
        activation._write_once(
            ROOT / "apply-claim.json", {"operation": operation, "plan_sha256": expected_plan_sha256}
        )
        patch = [
            {"op": "test", "path": path, "value": value}
            for path, value in (
                ("/metadata/uid", saved["deployment_uid"]),
                ("/metadata/resourceVersion", saved["resource_version"]),
                ("/spec/replicas", 1),
                ("/spec/strategy", {"type": "Recreate"}),
                ("/spec/template/spec/containers/0/image", saved["old_image"]),
            )
        ]
        patch.append(
            {
                "op": "replace",
                "path": "/spec/template/spec/containers/0/image",
                "value": saved["new_image"],
            }
        )
        status = admission._api_request(
            "PATCH",
            f"/apis/apps/v1/namespaces/{target.NAMESPACE}/deployments/tessera-control",
            patch,
            content_type="application/json-patch+json",
        )
        if status != 200:
            raise target.AdmissionError(
                "upgrade apply uncertain; retain intent and reconcile, never retry"
            )
        return {"state": "image_patch_submitted_readiness_unproven", "operation": operation}


def admitted_activation(base):
    pointer = ROOT / "current.json"
    if not pointer.exists():
        return base
    publication = admission._receipt(pointer)
    operation = publication["operation"]
    saved = _saved(operation)
    ready_name = publication.get("ready_receipt", "")
    if not re.fullmatch(r"ready-[a-f0-9]{64}\.json", ready_name):
        raise target.AdmissionError("upgrade readiness reference invalid")
    ready = admission._receipt(_operation(operation) / ready_name)
    claim = admission._receipt(ROOT / "apply-claim.json")
    if (
        any(saved[k] != base[k] for k in IDENTITY)
        or saved["old_image"] != base["image"]
        or ready.get("plan_sha256") != saved["plan_sha256"]
        or ready_name != "ready-" + _digest(ready) + ".json"
        or claim != {"operation": operation, "plan_sha256": saved["plan_sha256"]}
    ):
        raise target.AdmissionError("upgrade admission chain changed")
    return base | {"image": saved["new_image"]}


def reconcile(operation: str) -> dict:
    with _locked():
        saved = _saved(operation)
        claim = admission._receipt(ROOT / "apply-claim.json")
        if claim != {"operation": operation, "plan_sha256": saved["plan_sha256"]}:
            raise target.AdmissionError("upgrade claim mismatch")
        base = admission._receipt(activation.INTENT)
        if any(saved[k] != base[k] for k in IDENTITY) or saved["old_image"] != base["image"]:
            raise target.AdmissionError("upgrade original authority identity changed")
        pod = activation._ready_pod_for(base | {"image": saved["new_image"]})
        deploy = _deployment()
        old_spec = deploy["spec"].copy()
        old_spec = json.loads(json.dumps(old_spec))
        old_spec["template"]["spec"]["containers"][0]["image"] = saved["old_image"]
        if _digest(old_spec) != saved["spec_sha256"]:
            raise target.AdmissionError("upgrade changed fields beyond admitted image")
        backend = _backend(pod)
        if backend != saved["backend"]:
            raise target.AdmissionError("upgrade backend identity/schema/fence changed")
        value = {"plan_sha256": saved["plan_sha256"], "pod_uid": pod["pod_uid"]}
        receipt = _operation(operation) / ("ready-" + _digest(value) + ".json")
        if receipt.exists():
            if admission._receipt(receipt) != value:
                raise target.AdmissionError("upgrade readiness receipt changed")
        else:
            activation._write_once(receipt, value)
        pointer = ROOT / "current.json"
        if pointer.exists():
            if admission._receipt(pointer).get("operation") != operation:
                raise target.AdmissionError("another upgrade generation admitted")
            admitted_activation(base)  # Validate retained publication; it is historical proof.
        else:
            activation._write_once(pointer, {"operation": operation, "ready_receipt": receipt.name})
        return {
            "state": "same_PVC_single_Ready_writer_admitted",
            "operation": operation,
            "pod_uid": pod["pod_uid"],
            "application_auth": "unverified",
        }


def rollback(operation: str) -> dict:
    saved = _saved(operation)
    # There is no atomic fence between checking DB writes and a Kubernetes image
    # patch. Never pretend a SQL schema number makes old JSON models compatible.
    return {
        "state": "blocked",
        "operation": operation,
        "old_image": saved["old_image"],
        "reason": "live_image_only_rollback_unsafe_after_possible_metadata_writes",
        "next_action": "Use an explicitly reviewed compatible forward image or separately "
        "authorized held recovery; no database restore/overwrite is automatic.",
    }


def register(server):
    from .build_receipt import register as register_build

    register_build(server)
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
    server.tool(name="local_control_candidate_inventory", annotations=read)(candidate_inventory)
    server.tool(name="local_control_upgrade_plan", annotations=read)(plan)
    server.tool(name="local_control_upgrade_prepare", annotations=write)(prepare)
    server.tool(name="local_control_upgrade_apply", annotations=write)(apply)
    server.tool(name="local_control_upgrade_reconcile", annotations=write)(reconcile)
    server.tool(name="local_control_upgrade_rollback", annotations=read)(rollback)
