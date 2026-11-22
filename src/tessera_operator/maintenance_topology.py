"""Fixed topology observations; unregistered, no default command executor.

Setup supplies the native per-effect executor, not a caller's topology claims.
Future effects: Kubernetes metadata GETs, projected Docker inspect, read-only
node exec, local directory metadata and private receipt creation. No fallback,
helper Pod or installation. Receipts do NOT prove ownership/drain/retention or
capacity, and are not native admission tokens. Pod/PVC loss only, not host loss.
"""

import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path, PurePosixPath

from . import upgrade_support as target
from .schema_supervised import SupervisedDenied, digest

NODE = "desktop-control-plane"
LIMIT = 262144
TTL = 60
INSPECT = "{{json .Id}}\n{{json .State.Pid}}\n{{json .Mounts}}"


def deny(message):
    raise SupervisedDenied(message)


def clean(value):
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or str(PurePosixPath(value)) != value
        or ".." in PurePosixPath(value).parts
        or not re.fullmatch(r"/[a-zA-Z0-9_./-]*", value)
    ):
        deny("noncanonical topology path")
    return value


def beneath(path, parent):
    return PurePosixPath(path).is_relative_to(parent)


def overlap(a, b):
    return beneath(a, b) or beneath(b, a)


def unescape(value):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)


def mounts(raw):
    result = []
    for line in raw.splitlines():
        fields = line.split()
        try:
            split = fields.index("-")
            result.append(
                dict(
                    id=int(fields[0]),
                    device=fields[2],
                    root=clean(unescape(fields[3])),
                    path=clean(unescape(fields[4])),
                    fs=fields[split + 1],
                    source=unescape(fields[split + 2]),
                )
            )
        except (ValueError, IndexError):
            deny("incomplete node mount observation")
    if not result or len({m["id"] for m in result}) != len(result):
        deny("missing or duplicate node mounts")
    return result


def local_identity(path):
    """No-follow ancestry walk: do not let a symlink swap select another tree."""
    path = Path(path)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    observed = []
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            observed.append([part, info.st_dev, info.st_ino])
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            deny("custody directory not private owner directory")
        if not stat.S_ISDIR(info.st_mode):
            deny("custody path not directory")
        return dict(ancestry=observed, fsid=os.fstatvfs(fd).f_fsid)
    finally:
        os.close(fd)


class TopologyCollector:
    """Owner-only entry; execute(argv, timeout, max_bytes) has no default.

    Executor MUST preserve native admission separately for each effect and return
    stdout only on successful exit. No retry on denial/partial output. Scripted
    executors produce scripted evidence, never live acceptance. Hashes bind the
    observations, not an external assertion of authority or authenticity.
    """

    def __init__(self, execute, *, clock=time.time):
        self.execute = execute
        self.clock = clock

    def collect(self, plan, *, observed_at=None, publish=True):
        operation = plan["operation"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", operation):
            deny("invalid operation")
        started = self.clock() if observed_at is None else observed_at
        expires = min(plan["deadline"], started + TTL)
        if started >= expires:
            deny("expired topology operation")
        root = target.SOURCE / "private/local-control-upgrade/supervised" / operation
        destinations = {p: root / "custody" / p for p in ("backup", "validate")}
        before = {p: local_identity(v) for p, v in destinations.items()}
        observations = []

        def read(argv):
            if self.clock() >= expires:
                deny("topology observation expired")
            raw = self.execute(tuple(argv), timeout=10, max_bytes=LIMIT)
            if not isinstance(raw, str) or len(raw.encode()) > LIMIT or not raw:
                deny("missing or oversized topology observation")
            observations.append(dict(argv=argv, sha256=hashlib.sha256(raw.encode()).hexdigest()))
            return raw

        def kube(kind, name, namespaced=True):
            args = ["kubectl", "--kubeconfig", str(target.KUBECONFIG), "--context", target.CONTEXT]
            if namespaced:
                args += ["-n", target.NAMESPACE]
            return json.loads(
                read(args + ["get", kind] + ([name] if name else []) + ["-o", "json"])
            )

        def metadata():
            ns = kube("namespace", target.NAMESPACE, False)
            deployment = kube("deployment", "tessera-control")
            pvc = kube("pvc", "tessera-control-state")
            name = pvc["spec"]["volumeName"]
            if not re.fullmatch(r"pvc-[a-f0-9-]{36}", name):
                deny("unsupported PV identity")
            pv = kube("pv", name, False)
            pods = kube("pods", "")
            scheduled = kube("node", NODE, False)
            pinned = plan["target"]
            for key, obj in (
                ("namespace", ns),
                ("deployment", deployment),
                ("pvc", pvc),
                ("pv", pv),
            ):
                if obj["metadata"]["uid"] != pinned[key + "_uid"]:
                    deny("operation target identity changed")
            volumes = deployment["spec"]["template"]["spec"]["volumes"]
            if [v for v in volumes if v["name"] == "state"] != [
                dict(name="state", persistentVolumeClaim=dict(claimName="tessera-control-state"))
            ]:
                deny("Deployment state claim changed")
            claim = pv["spec"]["claimRef"]
            if (claim["uid"], claim["name"], claim["namespace"]) != (
                pinned["pvc_uid"],
                "tessera-control-state",
                target.NAMESPACE,
            ) or pvc["status"]["phase"] != "Bound":
                deny("PVC binding changed")
            affinity = pv["spec"].get("nodeAffinity", {})
            terms = affinity.get("required", {}).get("nodeSelectorTerms")
            if terms != [
                dict(
                    matchExpressions=[
                        dict(key="kubernetes.io/hostname", operator="In", values=[NODE])
                    ]
                )
            ]:
                deny("unsupported PV node affinity")
            selected = [
                p
                for p in pods["items"]
                if any(
                    o.get("uid") == pinned["deployment_uid"] and o.get("controller")
                    for o in p["metadata"].get("ownerReferences", [])
                )
            ]
            # Real Deployment Pods normally belong to a ReplicaSet. Resolve that
            # exact owner rather than accepting a matching label as ownership.
            if not selected:
                replicas = kube("replicasets", "")
                owners = {
                    r["metadata"]["uid"]
                    for r in replicas["items"]
                    if any(
                        o.get("uid") == pinned["deployment_uid"] and o.get("controller")
                        for o in r["metadata"].get("ownerReferences", [])
                    )
                }
                selected = [
                    p
                    for p in pods["items"]
                    if any(
                        o.get("uid") in owners and o.get("controller")
                        for o in p["metadata"].get("ownerReferences", [])
                    )
                ]
            if len(selected) != 1 or selected[0]["spec"].get("nodeName") != NODE:
                deny("scheduled control Pod ambiguous or missing")
            if [v for v in selected[0]["spec"].get("volumes", []) if v["name"] == "state"] != [
                v for v in volumes if v["name"] == "state"
            ]:
                deny("Pod volume chain differs from Deployment")
            mounted = []
            for spec in (deployment["spec"]["template"]["spec"], selected[0]["spec"]):
                controls = [c for c in spec.get("containers", []) if c["name"] == "control"]
                if len(controls) != 1:
                    deny("control container ambiguous")
                state = [m for m in controls[0].get("volumeMounts", []) if m["name"] == "state"]
                if state != [dict(name="state", mountPath="/state")]:
                    deny("control state mount changed or uses subpath")
                mounted.append(state)

            def meta(obj):
                return {k: obj["metadata"][k] for k in ("uid", "resourceVersion")}

            return dict(
                namespace=meta(ns),
                deployment=meta(deployment),
                pvc=meta(pvc),
                pv=dict(
                    metadata=meta(pv),
                    spec={k: pv["spec"][k] for k in ("hostPath", "nodeAffinity", "claimRef")},
                ),
                pod=dict(
                    metadata=meta(selected[0]), node=NODE, volumes=volumes, state_mounts=mounted
                ),
                node=dict(
                    metadata=meta(scheduled),
                    machine_id=scheduled["status"]["nodeInfo"]["machineID"],
                ),
            )

        first = metadata()
        host_path = clean(first["pv"]["spec"]["hostPath"]["path"])
        docker = ["docker", "--context", "desktop-linux"]
        inspect_cmd = docker + ["inspect", "--format", INSPECT, NODE]

        def container():
            values = read(inspect_cmd).splitlines()
            if len(values) != 3:
                deny("partial container projection")
            ident, pid, mapping = map(json.loads, values)
            if not re.fullmatch(r"[a-f0-9]{64}", ident) or not isinstance(pid, int) or pid <= 0:
                deny("container identity unavailable")
            return dict(id=ident, pid=pid, mounts=mapping)

        node = container()
        # Exec by observed immutable ID, never silently follow a recreated name.
        prefix = docker + ["exec", node["id"]]
        machine = read(prefix + ["cat", "/etc/machine-id"]).strip()
        if not re.fullmatch(r"[a-f0-9]{32}", machine) or machine != first["node"]["machine_id"]:
            deny("scheduled Kubernetes node differs from inspected container")
        canonical = read(prefix + ["readlink", "-e", "--", host_path]).strip()
        if clean(canonical) != host_path:
            deny("PV backing ancestry contains alias")
        ancestry = [str(p) for p in (PurePosixPath(host_path), *PurePosixPath(host_path).parents)]
        if len(ancestry) > 32:
            deny("node ancestry exceeds bound")
        stat_cmd = prefix + ["stat", "-c", "%d:%i:%f:%Z", "--", *ancestry]
        identities = read(stat_cmd)
        if len(identities.splitlines()) != len(ancestry) or not all(
            re.fullmatch(r"[0-9]+:[0-9]+:[a-fA-F0-9]+:[0-9]+", line)
            for line in identities.splitlines()
        ):
            deny("partial node backing ancestry")
        raw_mounts = read(prefix + ["cat", "/proc/self/mountinfo"])
        parsed = mounts(raw_mounts)
        relevant = [m for m in parsed if beneath(host_path, m["path"])]
        selected = max(relevant, key=lambda m: len(m["path"]), default=None)
        mappings = [m for m in node["mounts"] if beneath(host_path, clean(m["Destination"]))]
        if not selected or not mappings:
            deny("unresolved node backing mount (including root overlay)")
        mapping = max(mappings, key=lambda m: len(m["Destination"]))
        if mapping["Destination"] != selected["path"]:
            deny("nested node backing mount unresolved")
        source = clean(mapping["Source"])
        backing = str(
            PurePosixPath(source) / PurePosixPath(host_path).relative_to(mapping["Destination"])
        )
        # Only documented Mac shared prefixes or an explicit VM-local Docker
        # volume on a native Linux filesystem can be resolved in this namespace.
        if source.startswith("/host_mnt/") or source.startswith("/Users/"):
            mac = backing.removeprefix("/host_mnt")
            mac_identity = local_identity(Path(mac))
            source_inodes = {tuple(i[1:]) for i in mac_identity["ancestry"]}
            if any(overlap(mac, str(p)) for p in destinations.values()) or any(
                tuple(v["ancestry"][-1][1:]) in source_inodes
                or tuple(mac_identity["ancestry"][-1][1:]) in {tuple(i[1:]) for i in v["ancestry"]}
                for v in before.values()
            ):
                deny("custody aliases source PVC")
            domain = dict(kind="mac-shared", source=mac)
        elif (
            mapping["Type"] == "volume"
            and source.startswith("/var/lib/docker/volumes/")
            and selected["fs"] in {"ext4", "xfs", "btrfs"}
        ):
            domain = dict(kind="vm-local-volume", source=backing)
        else:
            deny("unresolved VM-to-Mac backing mapping")
        aliases = [
            m
            for m in parsed
            if m["device"] == selected["device"] and overlap(m["root"], selected["root"])
        ]
        if read(prefix + ["readlink", "-e", "--", host_path]).strip() != canonical:
            deny("node backing ancestry raced")
        if read(stat_cmd) != identities:
            deny("node backing inode ancestry raced")
        if read(prefix + ["cat", "/proc/self/mountinfo"]) != raw_mounts or container() != node:
            deny("node mount/container observation raced")
        if metadata() != first:
            deny("Kubernetes topology observation raced")
        if read(prefix + ["cat", "/etc/machine-id"]).strip() != machine:
            deny("node machine identity raced")
        if domain["kind"] == "mac-shared" and local_identity(Path(mac)) != mac_identity:
            deny("Mac source backing identity raced")
        after = {p: local_identity(v) for p, v in destinations.items()}
        if before != after or self.clock() >= expires:
            deny("local custody identity raced or observation expired")
        receipt = dict(
            operation=operation,
            plan_sha256=digest(plan),
            target=plan["target"],
            observed_at=started,
            valid_until=expires,
            failure_domain="maintenance-pod-and-pvc",
            local_platform=os.uname().sysname,
            destinations={p: dict(path=str(destinations[p]), **v) for p, v in after.items()},
            topology=first,
            container=node,
            backing=domain,
            mount=selected,
            aliases=aliases,
            node_ancestry=identities,
            observations=observations,
            admission="external native per-effect receipts required; this is not admission",
        )
        if not publish:
            return receipt
        # A failure above never publishes partial evidence. Exclusive create.
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if after != {p: local_identity(v) for p, v in destinations.items()}:
                deny("custody publication raced")
            raw = json.dumps(receipt, sort_keys=True).encode()
            if len(raw) > LIMIT:
                deny("topology receipt exceeds bound")
            fd = os.open(
                "topology.json",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(directory)
        finally:
            os.close(directory)
        return receipt
