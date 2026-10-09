"""Bounded local Docker CLI, with no ambient context, registry auth or host mounts."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import shutil
import subprocess
import time

from .capability_effects import BackendBlocked
from .local_docker_contract import CAPTURE, FS_BYTES, ID, INODES, MEMORY, PIDS, PROGRAM
from .local_docker_namespace import (
    OBSERVER,
    NamespaceUnknown,
    NativeNamespaceIdentity,
    require,
    verify_observation,
)


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

    def __call__(self, args, *, limit=CAPTURE, timeout=2.0):
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


def tmpfs():
    return {
        root: (
            f"rw,noexec,nosuid,nodev,size={FS_BYTES},nr_inodes={INODES},"
            "uid=65532,gid=65532,mode=0700"
        )
        for root in ("/scratch", "/output")
    }


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
    for key, value in plan.labels.items():
        args += ["--label", f"{key}={value}"]
    # The locally inspected config ID prevents a repository alias race.
    return args + [image_id, "-I", "-B", "-c", PROGRAM, plan.mode, plan.nonce, plan.digest]


class LocalDockerEngine:
    def __init__(self, run, *, identity=None):
        self.run = run
        self.identity = identity or NativeNamespaceIdentity()

    def namespace_preflight(self):
        return self.identity.preflight()

    def verify_network(self, plan, image_id, cid):
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
                OBSERVER,
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

    def inspect(self, plan, image_id, container_id=None):
        value = json.loads(self.run(["container", "inspect", container_id or plan.name]))[0]
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
            or any(
                m.get("Type") != "tmpfs" or m.get("Destination") not in tmpfs()
                for m in value.get("Mounts", [])
            )
            or value.get("NetworkSettings", {}).get("Networks", {}).keys() != {"none"}
        ):
            raise BackendBlocked("security settings unknown")

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
            PROGRAM,
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
        value = self.inspect(plan, image_id, container_id)
        cid = value["Id"]
        if value["State"].get("Paused"):
            self.run(["unpause", cid])
        if value["State"]["Running"]:
            self.run(["kill", "--signal", "KILL", cid])
        observed = self.inspect(plan, image_id, cid)
        if observed["State"]["Running"] or observed["State"]["Status"] not in {"exited", "created"}:
            raise DockerReplyUnknown()
        return observed

    def cleanup(self, plan, image_id, cid):
        value = self.inspect(plan, image_id, cid)
        if value["State"]["Running"]:
            raise BackendBlocked("cleanup before confirmed stop")
        self.run(["rm", cid])
        # An inspect error is NOT absence. Successful list + exact immutable ID is.
        remaining = self.run(["container", "ls", "--all", "--no-trunc", "--format", "{{.ID}}"])
        if cid in remaining.decode().splitlines():
            raise DockerReplyUnknown()
