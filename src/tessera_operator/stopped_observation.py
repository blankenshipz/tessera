"""Unregistered, read-only Kubernetes-scoped maintenance evidence collector.

Does not create an observer, stop writers, grant authority, or establish host/
guardian fencing. Its result is deliberately NOT accepted as stopped_db_proof.
"""

import hashlib
import inspect
import json
import re
import subprocess
from datetime import UTC, datetime

from . import upgrade_support as target


class ObservationDenied(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def read_db(root, relative):
    """Fixed future Pod probe. Immutable SQLite never opens a writable journal."""
    import hashlib
    import sqlite3
    import stat
    from pathlib import Path

    def stable_identity(info):
        # Reads may update atime. Preserve nanosecond write/change timestamps
        # and platform metadata; this helper travels with the generated probe.
        fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_uid",
            "st_gid",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
            "st_rdev",
            "st_flags",
            "st_gen",
            "st_birthtime",
            "st_birthtime_ns",
            "st_blocks",
            "st_blksize",
        )
        return tuple(getattr(info, field, None) for field in fields)

    root = Path(root)
    if not root.is_absolute() or root.resolve() != root:
        raise ValueError("noncanonical mount")
    current = root
    for part in relative.split("/"):
        if not part or part in {".", ".."} or "\\" in part:
            raise ValueError("unsafe relative DB")
        current /= part
        if stat.S_ISLNK(current.lstat().st_mode):
            raise ValueError("symlink DB path")
    before = current.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError("nonregular DB")
    if any(Path(str(current) + s).exists() for s in ("-wal", "-shm", "-journal")):
        raise ValueError("journal evidence present")
    with current.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    connection = sqlite3.connect(current.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        schema = connection.execute("SELECT schema_version FROM schema_meta").fetchone()[0]
        incarnation = connection.execute("SELECT incarnation FROM control_recovery").fetchone()[0]
    finally:
        connection.close()
    with current.open("rb") as stream:
        after_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    after = current.stat()
    if (
        stable_identity(before) != stable_identity(after)
        or sha != after_sha
        or any(Path(str(current) + s).exists() for s in ("-wal", "-shm", "-journal"))
    ):
        raise ValueError("DB changed during read")
    if type(schema) is not int or not isinstance(incarnation, str) or not incarnation:
        raise ValueError("unknown schema/incarnation")
    return dict(
        schema=schema,
        incarnation=incarnation,
        sha256=sha,
        device=after.st_dev,
        inode=after.st_ino,
        size=after.st_size,
        mtime_ns=after.st_mtime_ns,
        ctime_ns=after.st_ctime_ns,
    )


def bounded(argv):
    result = subprocess.run(argv, capture_output=True, timeout=8, check=False)
    if result.returncode or len(result.stdout) > 262144:
        raise ObservationDenied("bounded read failed")
    return result.stdout


class StoppedObservation:
    """Future transport: fixed GETs then one exec in a pre-existing RO Pod.

    The configuration must be operator-owned, never caller approval text. Pod
    creation and host/guardian fencing require separate explicit mechanisms.
    """

    def __init__(self, pins, *, run=None):
        self.pins, self.run = dict(pins), run or bounded
        if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", pins["observer_image"]):
            raise ObservationDenied("digest-pinned observer image required")

    def get(self, kind, name=None):
        args = [*target.KUBECTL]
        if kind != "pv":
            args += ["-n", target.NAMESPACE]
        args += ["get", kind]
        if name is not None:
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", name):
                raise ObservationDenied("invalid object name")
            args.append(name)
        raw = self.run([*args, "-o", "json"])
        if len(raw) > 262144:
            raise ObservationDenied("oversize observation")
        return json.loads(raw)

    def snapshot(self):
        p = self.pins
        deploy = self.get("deployment", "tessera-control")
        pvc = self.get("pvc", "tessera-control-state")
        pv = self.get("pv", pvc["spec"]["volumeName"])
        pods = self.get("pods")
        identities = {}
        for key, obj in (("deployment", deploy), ("pvc", pvc), ("pv", pv)):
            meta = obj["metadata"]
            if (
                meta["uid"] != p[key + "_uid"]
                or meta["resourceVersion"] != p[key + "_resource_version"]
            ):
                raise ObservationDenied("target identity changed")
            identities[key] = {k: meta[k] for k in ("name", "uid", "resourceVersion")}
        claim = pv["spec"]["claimRef"]
        if (claim["uid"], claim["name"], claim["namespace"]) != (
            p["pvc_uid"],
            "tessera-control-state",
            target.NAMESPACE,
        ) or pvc["status"]["phase"] != "Bound":
            raise ObservationDenied("PVC/PV binding changed")
        status = deploy["status"]
        if (
            deploy["spec"]["replicas"] != 0
            or status["observedGeneration"] != deploy["metadata"]["generation"]
        ):
            raise ObservationDenied("Deployment not observed at zero replicas")
        if any(
            status.get(k, 0) != 0
            for k in ("replicas", "readyReplicas", "updatedReplicas", "availableReplicas")
        ):
            raise ObservationDenied("Deployment still has replicas")
        observer = None
        for pod in pods["items"]:
            volumes = [
                v
                for v in pod["spec"].get("volumes", [])
                if v.get("persistentVolumeClaim", {}).get("claimName") == "tessera-control-state"
            ]
            if not volumes:
                continue
            # Terminal Pods too: no undocumented assumption that they cannot restart.
            if pod["metadata"]["uid"] != p["observer_uid"]:
                raise ObservationDenied("other PVC consumer present")
            observer = pod
            if pod["metadata"]["resourceVersion"] != p["observer_resource_version"]:
                raise ObservationDenied("observer version changed")
            spec = pod["spec"]
            if (
                digest(spec) != p["observer_spec_sha256"]
                or spec.get("initContainers")
                or spec.get("ephemeralContainers")
            ):
                raise ObservationDenied("observer profile changed")
            if (
                len(spec["containers"]) != 1
                or spec.get("automountServiceAccountToken") is not False
            ):
                raise ObservationDenied("observer not isolated")
            container = spec["containers"][0]
            if container["name"] != "observer" or container["image"] != p["observer_image"]:
                raise ObservationDenied("observer image/container changed")
            if len(volumes) != 1 or volumes[0]["persistentVolumeClaim"].get("readOnly") is not True:
                raise ObservationDenied("observer PVC not read-only")
            mounts = container["volumeMounts"]
            if mounts != [{"name": volumes[0]["name"], "mountPath": "/state", "readOnly": True}]:
                raise ObservationDenied("observer mount ambiguous")
            if pod["status"]["phase"] != "Running":
                raise ObservationDenied("observer unavailable")
            (runtime,) = pod["status"]["containerStatuses"]
            if (
                runtime["name"] != "observer"
                or not runtime["ready"]
                or not runtime.get("state", {}).get("running")
                or runtime["imageID"] != p["observer_image_id"]
                or not runtime["imageID"].endswith(p["observer_image"].split("@", 1)[1])
                or runtime["containerID"] != p["observer_container_id"]
            ):
                raise ObservationDenied("observer runtime identity changed")
        if observer is None:
            raise ObservationDenied("separately provisioned read-only observer absent")
        # Retain full raw identities only as hashes; no env/credential projection.
        return dict(
            target=identities,
            observer={k: observer["metadata"][k] for k in ("name", "uid", "resourceVersion")},
            snapshot_sha256=digest([deploy, pvc, pv, pods]),
        )

    def observe(self):
        try:
            before = self.snapshot()
            relative = self.pins["relative_db"]
            if relative != "control/tessera.sqlite3":
                raise ObservationDenied("fixed DB path required")
            code = (
                inspect.getsource(read_db)
                + "\nimport json;print(json.dumps(read_db('/state','control/tessera.sqlite3')))"
            )
            raw = self.run(
                [
                    *target.KUBECTL,
                    "-n",
                    target.NAMESPACE,
                    "exec",
                    before["observer"]["name"],
                    "-c",
                    "observer",
                    "--",
                    "/app/.venv/bin/python",
                    "-I",
                    "-B",
                    "-c",
                    code,
                ]
            )
            if len(raw) > 4096:
                raise ObservationDenied("oversize DB observation")
            db = json.loads(raw)
            if set(db) != {
                "schema",
                "incarnation",
                "sha256",
                "device",
                "inode",
                "size",
                "mtime_ns",
                "ctime_ns",
            }:
                raise ObservationDenied("invalid DB projection")
            if (
                any(
                    type(db[k]) is not int or db[k] < 0
                    for k in ("schema", "device", "inode", "size", "mtime_ns", "ctime_ns")
                )
                or not isinstance(db["incarnation"], str)
                or not db["incarnation"]
                or not isinstance(db["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", db["sha256"])
            ):
                raise ObservationDenied("unknown DB identity")
            if self.snapshot() != before:
                raise ObservationDenied("target changed during observation")
            return dict(
                before,
                db=db,
                relative_db=relative,
                observed_at=datetime.now(UTC).isoformat(),
                kubernetes_consumers="only pinned read-only observer",
                stopped_db_proof=False,
                backup_custody=None,
                authority=None,
                missing="independent host/admission/guardian fence and backup/restore custody",
            )
        except Exception as exc:
            raise ObservationDenied("stopped observation failed; retain attempt, no retry") from exc
