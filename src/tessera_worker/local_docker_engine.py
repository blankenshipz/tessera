"""Bounded local Docker CLI, with no ambient context, registry auth or host mounts."""

from __future__ import annotations

import hashlib
import inspect as introspect
import json
import os
import re
import selectors
import shutil
import subprocess
import time

from .capability_effects import BackendBlocked
from .local_docker_contract import (
    CAPTURE,
    FS_BYTES,
    ID,
    INODES,
    MEMORY,
    OUTPUT_VOLUME_PREFIX,
    PIDS,
    VOLUME_LABELS,
    observer_for,
    output_volume_options,
    program_for,
)
from .local_docker_namespace import (
    ATTESTATION,
    STRONG_CLASS,
    WEAK_CLASS,
    EngineDeclaredIdentity,
    NamespaceUnknown,
    NativeNamespaceIdentity,
    require,
    verify_engine_declared_observation,
    verify_observation,
)

# Default bound for every Docker call (create/start/exec/observer/peer inventory/...).
DEFAULT_CALL_SECONDS = 2.0
# Post-termination cleanup confirmation only (stop readbacks, rm, absence query, output
# volume removal). Runs after the workload is killed/exited, so it cannot extend runtime
# or lease; a timeout is still unknown, never absence.
CLEANUP_CALL_SECONDS = 10.0


class DockerReplyUnknown(Exception):
    pass


def safe_engine_error(value):
    """Retain an error digest and finite categories, never daemon paths/raw text."""
    text = value if isinstance(value, str) else ""
    categories = [
        s
        for s in (
            "invalid argument",
            "permission denied",
            "operation not permitted",
            "no such file or directory",
            "no space left on device",
            "exec format error",
            "tmpfs",
            "nr_inodes",
            "executable file not found",
            "mount",
            "not supported",
        )
        if s in text.lower()
    ]
    return dict(error_sha256=hashlib.sha256(text.encode()).hexdigest(), categories=categories)


class DockerCLI:
    def __init__(self, config, private_config):
        self.config = config
        self.private_config = str(private_config)
        self.executable = shutil.which("docker")
        if not self.executable:
            raise BackendBlocked("Docker executable unavailable")

    def __call__(self, args, *, limit=CAPTURE, timeout=DEFAULT_CALL_SECONDS):
        command = [
            self.executable,
            "--config",
            self.private_config,
            "--host",
            self.config.target,
            *args,
        ]
        # No inherited Docker context/config, proxy, credential helper or cloud env.
        env = {"PATH": os.defpath, "HOME": self.private_config}
        process = None
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
            output = bytearray()
            total = 0
            deadline = time.monotonic() + timeout
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                selector.register(process.stderr, selectors.EVENT_READ)
                while selector.get_map():
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise DockerReplyUnknown()
                    for item, _ in selector.select(min(left, 0.1)):
                        chunk = os.read(item.fileobj.fileno(), 8192)
                        if not chunk:
                            selector.unregister(item.fileobj)
                            continue
                        total += len(chunk)
                        if total > limit:
                            raise DockerReplyUnknown()
                        if item.fileobj is process.stdout:
                            output.extend(chunk)
                process.wait(timeout=max(0.001, deadline - time.monotonic()))
            if process.returncode != 0:
                raise DockerReplyUnknown()
            return bytes(output)
        except Exception:
            # Neither argv echo, stderr, env nor exception text reaches evidence.
            raise DockerReplyUnknown("Docker response unavailable") from None
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=1)
                process.stdout.close()
                process.stderr.close()


VOLUME_NAME = re.compile(re.escape(OUTPUT_VOLUME_PREFIX) + r"[a-f0-9]{64}\Z")


def tmpfs():
    """Container-namespace tmpfs mounts. /output is NOT here: see output_volume_arguments."""
    return {
        root: (
            f"rw,noexec,nosuid,nodev,size={FS_BYTES},nr_inodes={INODES},"
            "uid=65532,gid=65532,mode=0700"
        )
        for root in ("/scratch",)
    }


def output_volume_arguments(plan):
    """Exact-named, labelled tmpfs-typed local volume that `docker cp` can read."""
    options = output_volume_options()
    args = ["volume", "create", "--driver", "local"]
    for key in ("type", "device", "o"):
        args += ["--opt", f"{key}={options[key]}"]
    for key, value in plan.labels.items():
        args += ["--label", f"{key}={value}"]
    return args + [plan.output_volume_name]


_CONFIGURED = {"Type", "Source", "Target", "VolumeOptions", "ReadOnly"}


def create_arguments(plan, image_id):
    args = [
        "create",
        "--pull",
        "never",
        "--name",
        plan.name,
        "--network",
        "none",
        "--user",
        "65532:65532",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--cpus",
        "0.25",
        "--memory",
        str(MEMORY),
        "--memory-swap",
        str(MEMORY),
        "--pids-limit",
        str(PIDS),
        "--ipc",
        "none",
        "--shm-size",
        "65536",
        "--restart",
        "no",
        "--ulimit",
        "nofile=64:64",
        "--ulimit",
        "core=0:0",
        "--log-driver",
        "local",
        "--log-opt",
        "max-size=32k",
        "--log-opt",
        "max-file=1",
        # The local driver defaults to compression, incompatible with one file.
        # Disable rotation compression, not the bounded log or sandbox profile.
        "--log-opt",
        "compress=false",
        "--workdir",
        "/scratch",
        "--entrypoint",
        "/usr/local/bin/python",
        "--no-healthcheck",
    ]
    for root, options in tmpfs().items():
        args += ["--tmpfs", f"{root}:{options}"]
    args += ["--mount", f"type=volume,src={plan.output_volume_name},dst=/output"]
    for key, value in plan.labels.items():
        args += ["--label", f"{key}={value}"]
    # The locally inspected config ID prevents a repository alias race.
    return args + [
        image_id,
        "-I",
        "-B",
        "-c",
        program_for(plan),
        plan.mode,
        plan.nonce,
        plan.digest,
    ]


class LocalDockerEngine:
    def __init__(self, run, *, identity=None, attestation_class=STRONG_CLASS):
        if attestation_class not in ATTESTATION:
            raise BackendBlocked("unknown attestation class")
        self.run = run
        # Runners with a fixed (args) signature cannot take a bound; they keep their own.
        try:
            names = introspect.signature(run).parameters
            self._bounded = "timeout" in names or any(
                p.kind is p.VAR_KEYWORD for p in names.values()
            )
        except (TypeError, ValueError):
            self._bounded = False
        self.attestation_class = attestation_class
        # Explicit selection only: the strong reader never falls back to the weak one.
        self.identity = identity or (
            NativeNamespaceIdentity()
            if attestation_class == STRONG_CLASS
            else EngineDeclaredIdentity()
        )

    def settle(self, args, **kw):
        """Cleanup-class call: post-termination confirmation with its own longer bound."""
        if self._bounded:
            kw["timeout"] = CLEANUP_CALL_SECONDS
        return self.run(args, **kw)

    def namespace_preflight(self):
        return self.identity.preflight()

    def peer_declarations(self, ids):
        """Engine-declared network/PID/IPC modes of every listed container (weak class)."""
        template = (
            '{"id":{{json .Id}},"net":{{json .HostConfig.NetworkMode}},'
            '"pid":{{json .HostConfig.PidMode}},"ipc":{{json .HostConfig.IpcMode}}}'
        )
        raw = self.run(["container", "inspect", "--format", template, *ids], limit=16384)
        try:
            peers = [json.loads(line) for line in raw.splitlines()]
        except Exception:
            raise NamespaceUnknown("peer_inventory_unknown") from None
        require(
            len(peers) == len(ids) and [p.get("id") for p in peers] == ids,
            "peer_inventory_unknown",
        )
        return peers

    def list_peers(self, cid):
        ids = self.run(["container", "ls", "--no-trunc", "--format", "{{.ID}}"], limit=4096)
        ids = ids.decode().splitlines()
        require(
            cid in ids and len(ids) <= 32 and all(ID.fullmatch(i) for i in ids),
            "peer_inventory_unknown",
        )
        return ids

    def verify_network(self, plan, image_id, cid):
        # Class is bound in the plan; an engine of another class must not verify it.
        if plan.attestation_class != self.attestation_class:
            raise BackendBlocked("attestation class mismatch between plan and engine")
        if self.attestation_class == WEAK_CLASS:
            return self.verify_engine_declared(plan, image_id, cid)
        # Trusted code is supplied via immutable exec argv, not installed in the
        # workload. Both host snapshots and exec are mandatory and bounded.
        value = self.inspect(plan, image_id, cid)
        self.verify_security(value)
        require(value["State"]["Running"] is True, "namespace_process_not_running")
        ids = self.run(["container", "ls", "--no-trunc", "--format", "{{.ID}}"], limit=4096)
        ids = ids.decode().splitlines()
        require(
            cid in ids and len(ids) <= 32 and all(ID.fullmatch(i) for i in ids),
            "peer_inventory_unknown",
        )
        pids = self.run(
            ["container", "inspect", "--format", "{{json .State.Pid}}", *ids], limit=4096
        )
        try:
            peers = [json.loads(line) for line in pids.splitlines()]
        except Exception:
            raise NamespaceUnknown("peer_inventory_unknown") from None
        require(len(peers) == len(ids), "peer_inventory_unknown")
        before = self.identity.snapshot(value, peers)
        raw = self.run(
            [
                "exec",
                cid,
                "/usr/local/bin/python",
                "-I",
                "-B",
                "-c",
                observer_for(plan),
                plan.nonce,
                plan.digest,
            ],
            limit=16384,
        )
        observed = self.inspect(plan, image_id, cid)
        self.verify_security(observed)
        require(observed["State"]["Running"] is True, "namespace_process_not_running")
        after = self.identity.snapshot(observed, peers)
        return verify_observation(raw, before, after, plan)

    def verify_engine_declared(self, plan, image_id, cid):
        """Weak class: engine readback + the unchanged in-workload observer. Same exec."""
        value = self.inspect(plan, image_id, cid)
        self.verify_security(value)
        require(value["State"]["Running"] is True, "namespace_process_not_running")
        before = self.identity.snapshot(value, self.peer_declarations(self.list_peers(cid)))
        raw = self.run(
            [
                "exec",
                cid,
                "/usr/local/bin/python",
                "-I",
                "-B",
                "-c",
                observer_for(plan),
                plan.nonce,
                plan.digest,
            ],
            limit=16384,
        )
        observed = self.inspect(plan, image_id, cid)
        self.verify_security(observed)
        require(observed["State"]["Running"] is True, "namespace_process_not_running")
        after = self.identity.snapshot(observed, self.peer_declarations(self.list_peers(cid)))
        return verify_engine_declared_observation(raw, before, after, plan)

    def preflight(self, plan):
        # Do not read daemon proxy/auth/config details. Discovery precedes effects
        # and has a separate bounded five-second allowance.
        template = (
            '{"OperatingSystem":{{json .OperatingSystem}},'
            '"OSType":{{json .OSType}},"CgroupVersion":{{json .CgroupVersion}}}'
        )
        info = json.loads(self.run(["info", "--format", template], limit=4096, timeout=5))
        if (info.get("OperatingSystem"), info.get("OSType"), info.get("CgroupVersion")) != (
            "Docker Desktop",
            "linux",
            "2",
        ):
            raise BackendBlocked("unproven local Docker Desktop target")
        value = json.loads(self.run(["image", "inspect", plan.image]))[0]
        image_id = value.get("Id", "")
        config = value.get("Config", {})
        if (
            not image_id.startswith("sha256:")
            or not ID.fullmatch(image_id[7:])
            or plan.image not in value.get("RepoDigests", [])
            or value.get("Os") != "linux"
            or config.get("Volumes")
            or config.get("OnBuild")
        ):
            raise BackendBlocked("unproven local immutable image")
        # Image environment must also be credential-free. Override no host values.
        allowed = {
            "PATH",
            "LANG",
            "GPG_KEY",
            "PYTHON_VERSION",
            "PYTHON_SHA256",
            "UV_LINK_MODE",
            "UV_PYTHON_DOWNLOADS",
            "PYTHONDONTWRITEBYTECODE",
        }
        if any(item.split("=", 1)[0] not in allowed for item in config.get("Env", [])):
            raise BackendBlocked("image environment outside reviewed profile")
        return image_id

    def inspect(self, plan, image_id, container_id=None, cleanup=False):
        call = self.settle if cleanup else self.run
        value = json.loads(call(["container", "inspect", container_id or plan.name]))[0]
        cid = value.get("Id", "")
        if (
            not ID.fullmatch(cid)
            or (container_id is not None and cid != container_id)
            or value.get("Name") != "/" + plan.name
            or value.get("Image") != image_id
            or any(
                value.get("Config", {}).get("Labels", {}).get(k) != v
                for k, v in plan.labels.items()
            )
        ):
            raise BackendBlocked("container ownership mismatch")
        return value

    def verify_security(self, value):
        h, c = value["HostConfig"], value["Config"]
        required = {
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "Privileged": False,
            "NanoCpus": 250000000,
            "Memory": MEMORY,
            "MemorySwap": MEMORY,
            "PidsLimit": PIDS,
            "IpcMode": "none",
            "PidMode": "",
            "UTSMode": "",
            "CapDrop": ["ALL"],
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "Tmpfs": tmpfs(),
            "ShmSize": 65536,
            "LogConfig": {
                "Type": "local",
                "Config": {"max-size": "32k", "max-file": "1", "compress": "false"},
            },
        }
        if (
            any(h.get(k) != v for k, v in required.items())
            or c.get("User") != "65532:65532"
            or c.get("Entrypoint") != ["/usr/local/bin/python"]
            or h.get("SecurityOpt") != ["no-new-privileges=true"]
            or any(
                h.get(k)
                for k in [
                    "Binds",
                    "Devices",
                    "DeviceRequests",
                    "VolumesFrom",
                    "CapAdd",
                    "PortBindings",
                    "ExtraHosts",
                ]
            )
            or value.get("NetworkSettings", {}).get("Networks", {}).keys() != {"none"}
        ):
            raise BackendBlocked("security settings unknown")
        self.verify_output_volume(value)

    def verify_output_volume(self, value):
        """Exactly one volume mount (the exact-named /output volume) plus the /scratch tmpfs."""
        labels = value["Config"].get("Labels") or {}
        name = labels.get("tessera.output_volume", "")
        if not VOLUME_NAME.fullmatch(name):
            raise BackendBlocked("security settings unknown")
        mounts = value.get("Mounts") or []
        volumes = [m for m in mounts if m.get("Type") == "volume"]
        others = [m for m in mounts if m.get("Type") != "volume"]
        configured = value["HostConfig"].get("Mounts")
        if (
            len(volumes) != 1
            or any(m.get("Type") != "tmpfs" or m.get("Destination") != "/scratch" for m in others)
            or len(others) > 1
            or volumes[0].get("Name") != name
            or volumes[0].get("Destination") != "/output"
            or volumes[0].get("Driver") != "local"
            or volumes[0].get("RW") is not True
            or not isinstance(configured, list)
            or len(configured) != 1
            or configured[0].get("Type") != "volume"
            or configured[0].get("Source") != name
            or configured[0].get("Target") != "/output"
            or configured[0].get("ReadOnly")
            or any(configured[0].get(k) for k in set(configured[0]) - _CONFIGURED)
            or any(v for v in (configured[0].get("VolumeOptions") or {}).values())
        ):
            raise BackendBlocked("security settings unknown")
        self.verify_volume(name, {k: labels.get(k) for k in VOLUME_LABELS})

    def verify_volume(self, name, labels, cleanup=False):
        """Read the named volume back via `docker volume inspect`: driver, options, labels."""
        try:
            raw = json.loads((self.settle if cleanup else self.run)(["volume", "inspect", name]))
        except Exception:
            raise BackendBlocked("output volume readback unknown") from None
        volume = raw[0] if isinstance(raw, list) and len(raw) == 1 else None
        if (
            not isinstance(volume, dict)
            or volume.get("Name") != name
            or volume.get("Driver") != "local"
            or volume.get("Scope", "local") != "local"
            or volume.get("Options") != output_volume_options()
            or any((volume.get("Labels") or {}).get(k) != v for k, v in labels.items())
            or any(v is None for v in labels.values())
        ):
            raise BackendBlocked("output volume readback mismatch")
        return volume

    def create_volume(self, plan):
        """Create the exact-named volume, then prove it by readback. No retry, no reuse."""
        raw = self.run(output_volume_arguments(plan)).decode().strip()
        if raw != plan.output_volume_name:
            raise DockerReplyUnknown()
        return self.verify_volume(plan.output_volume_name, plan.labels)

    def remove_volume(self, plan):
        """Remove only the exact owned volume; absence needs a successful exact-name list."""
        name = plan.output_volume_name
        query = ["volume", "ls", "--quiet", "--filter", f"name={name}"]
        listed = self.settle(query).decode()
        if name in listed.splitlines():
            # ownership before every destructive action
            self.verify_volume(name, plan.labels, cleanup=True)
            self.settle(["volume", "rm", name])
            listed = self.settle(query).decode()
            if name in listed.splitlines():
                raise DockerReplyUnknown()

    def create(self, plan, image_id):
        raw = self.run(create_arguments(plan, image_id)).decode().strip()
        if not ID.fullmatch(raw):
            raise DockerReplyUnknown()
        value = self.inspect(plan, image_id, raw)
        self.verify_security(value)
        if value["Config"].get("Cmd") != [
            "-I",
            "-B",
            "-c",
            program_for(plan),
            plan.mode,
            plan.nonce,
            plan.digest,
        ]:
            raise BackendBlocked("fixture command readback mismatch")
        return raw

    def signal(self, container_id, marker):
        if marker not in {"go", "finish"}:
            raise BackendBlocked("invalid fixture signal")
        return self.run(
            [
                "exec",
                container_id,
                "/usr/local/bin/python",
                "-I",
                "-B",
                "-c",
                f"open('/scratch/{marker}', 'x').close()",
            ]
        )

    def stop(self, plan, image_id, container_id=None):
        # Inspect ownership before every destructive action; use immutable ID, never
        # rm -f, prune, name filters or label selectors for destruction.
        # Pre-termination calls keep the short default bound (the workload may still be
        # running); only the readback after kill/exit uses the longer cleanup bound.
        value = self.inspect(plan, image_id, container_id)
        cid = value["Id"]
        if value["State"].get("Paused"):
            self._signal_workload(plan, image_id, cid, ["unpause", cid])
        if value["State"]["Running"]:
            self._signal_workload(plan, image_id, cid, ["kill", "--signal", "KILL", cid])
        observed = self.inspect(plan, image_id, cid, cleanup=True)
        if observed["State"]["Running"] or observed["State"]["Status"] not in {"exited", "created"}:
            raise DockerReplyUnknown()
        return observed

    def _signal_workload(self, plan, image_id, cid, args):
        """A pre-termination call that may lose a race with the workload's own exit.

        `docker unpause`/`kill` fail when the container stopped between the inspection and
        the call. That is benign only if a fresh inspection (short bound, same ownership
        checks) shows the container is no longer running; a container that is still
        running, or one that can no longer be inspected (for example removed), stays
        unknown. The readback after this still has to confirm the exit.
        """
        try:
            self.run(args)
        except DockerReplyUnknown:
            if self.inspect(plan, image_id, cid)["State"]["Running"]:
                raise DockerReplyUnknown() from None

    def cleanup(self, plan, image_id, cid):
        value = self.inspect(plan, image_id, cid, cleanup=True)
        if value["State"]["Running"]:
            raise BackendBlocked("cleanup before confirmed stop")
        self.settle(["rm", cid])
        self.confirm_absent(cid)

    def confirm_absent(self, cid):
        """Exact-ID, O(1) absence query. Only a successful, well-formed, cid-free reply counts.

        An inspect/list error or timeout is NOT absence. A filtered reply may only be empty;
        any ID (the cid or another) is invalid evidence for an exact-ID filter.
        """
        if not isinstance(cid, str) or not ID.fullmatch(cid):
            raise DockerReplyUnknown()
        remaining = self.settle(
            [
                "container",
                "ls",
                "--all",
                "--no-trunc",
                "--filter",
                f"id={cid}",
                "--format",
                "{{.ID}}",
            ]
        )
        try:
            lines = remaining.decode().splitlines()
        except Exception:
            raise DockerReplyUnknown() from None
        if lines:  # exact-ID filter: any ID, matching or not, is invalid evidence
            raise DockerReplyUnknown()
