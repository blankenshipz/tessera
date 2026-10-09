"""Exact-ID, stop-only Docker fixture monitor. No create/start/exec-selected commands.

Docker socket authority is host-equivalent. It requires explicit acknowledgement,
an isolated execution host and independent daemon-host procfs visibility. It is NOT
a privileged universal host service. Namespace proof is reused, not weakened for
Docker Desktop clients outside the Linux daemon host.
"""

import json
import os
from pathlib import Path

from tessera_worker.local_docker_contract import PROGRAM, FixturePlan
from tessera_worker.local_docker_namespace import bounded_text, process_identity

from .protocol import Denied, Fact, Observation, digest


def observation(category, state="verified", *, origin="adapter", **facts):
    return Observation(
        category=category,
        state=state,
        origin=origin,
        facts=tuple(Fact(name=k, value=v) for k, v in facts.items()),
    )


class DockerAdapter:
    kind = "docker-local-v1"
    source = "execution-monitor"

    def __init__(self, engine, plan, *, allow_host_socket=False):
        self.engine = engine
        self.plan = FixturePlan.model_validate_json(plan.model_dump_json())
        self.allowed = allow_host_socket is True
        self.process = None

    def target(self):
        # Boot identity prevents reconnecting the same assignment to a replaced host.
        raw = self.engine.run(["info", "--format", "{{json .ID}}"], limit=1024)
        daemon = json.loads(raw)
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if not isinstance(daemon, str) or not 1 <= len(daemon) <= 128 or len(boot) != 36:
            raise Denied("daemon identity unknown")
        return digest([daemon, boot])

    def preflight(self, a):
        p = self.plan
        if (
            not self.allowed
            or a.privilege_profile != "docker-socket-procfs-read-v1"
            or (a.tenant, a.project, a.operation, a.nonce, a.plan_digest, a.package_digest)
            != (p.org_id, p.project_id, p.operation_id, p.nonce, p.digest, p.digest)
            or p.mode != "wait"
            or a.limits.output_bytes != 0
            or (
                a.limits.memory_bytes,
                a.limits.cpu_millicores,
                a.limits.pids,
                a.limits.filesystem_bytes,
                a.limits.filesystem_inodes,
            )
            != (
                p.memory_bytes,
                p.cpu_millicores,
                p.pid_limit,
                p.filesystem_bytes_per_mount,
                p.filesystem_inodes_per_mount,
            )
        ):
            raise Denied("Docker fixture profile mismatch")
        self.engine.namespace_preflight()
        if a.workload.target_digest != self.target():
            raise Denied("daemon target mismatch")

    def owned(self, a):
        self.preflight(a)
        value = self.engine.inspect(self.plan, "sha256:" + a.workload.image_digest, a.workload.uid)
        if (
            digest([value["Id"], value["Created"]]) != a.workload.instance_digest
            or value.get("RestartCount") != 0
            or value["Config"].get("Cmd")
            != [
                "-I",
                "-B",
                "-c",
                PROGRAM,
                self.plan.mode,
                self.plan.nonce,
                self.plan.digest,
            ]
        ):
            raise Denied("container generation mismatch")
        self.engine.verify_security(value)
        if value["State"]["Running"]:
            current = (
                value["State"]["Pid"],
                value["State"]["StartedAt"],
                process_identity(value["State"]["Pid"])["start_ticks"],
            )
            if self.process is not None and current != self.process:
                raise Denied("process generation changed")
            self.process = current
        return value

    def absent(self, a):
        self.preflight(a)
        raw = self.engine.run(
            ["container", "ls", "--all", "--no-trunc", "--format", "{{.ID}}"], limit=65536
        )
        ids = raw.decode().splitlines()
        if any(len(cid) != 64 or any(c not in "0123456789abcdef" for c in cid) for cid in ids):
            raise Denied("container inventory incomplete")
        return a.workload.uid not in ids

    def observe(self, a):
        if self.absent(a):
            return (observation("termination", absent=True), observation("cleanup", absent=True))
        value = self.owned(a)
        s, h = value["State"], value["HostConfig"]
        result = [
            observation(
                "identity",
                container=value["Id"],
                image=a.workload.image_digest,
                instance=a.workload.instance_digest,
                running=s["Running"],
                uid=os.geteuid(),
                privilege_profile=digest(a.privilege_profile),
            ),
            # Readback is retained as raw evidence but is NOT enforcement proof.
            observation(
                "resources",
                "unknown",
                origin="daemon-readback",
                memory_max=h["Memory"],
                pids_max=h["PidsLimit"],
                cpu_millicores=h["NanoCpus"] // 1000000,
            ),
            observation(
                "filesystem", "unknown", origin="daemon-readback", readonly=h["ReadonlyRootfs"]
            ),
            # No capture was performed. Even the closed wait program's lack of
            # declared output is not an independently observed empty filesystem.
            observation("custody", "unknown"),
        ]
        if not s["Running"] and s["Status"] in {"exited", "created"}:
            result.append(observation("termination", running=False, exit_code=s["ExitCode"]))
            return tuple(result)
        # Independently read the exact process's cgroup and mount, not its log or
        # self-reported limits. Unknown remains until native exhaustion conformance.
        try:
            cg = bounded_text(f"/proc/{s['Pid']}/cgroup").strip()
            if not cg.startswith("0::/") or a.workload.uid not in cg or "\n" in cg:
                raise Denied("cgroup identity unknown")
            root = Path("/sys/fs/cgroup")
            path = root / cg[4:]
            if path.resolve() != path or not path.is_relative_to(root):
                raise Denied("cgroup path unknown")
            quota, period = map(int, bounded_text(path / "cpu.max").split())
            resource = observation(
                "resources",
                "unknown",
                origin="host-procfs",
                memory_max=int(bounded_text(path / "memory.max")),
                memory_swap_max=int(bounded_text(path / "memory.swap.max")),
                pids_max=int(bounded_text(path / "pids.max")),
                cpu_millicores=quota * 1000 // period,
            )
            fs = os.statvfs(f"/proc/{s['Pid']}/root/output")
            filesystem = observation(
                "filesystem",
                "unknown",
                origin="host-procfs",
                filesystem_bytes=fs.f_blocks * fs.f_frsize,
                filesystem_inodes=fs.f_files,
            )
            self.owned(a)
            result[1:3] = [resource, filesystem]
        except Exception:
            pass  # retain explicitly unknown Docker configuration observations
        try:
            proof = self.engine.verify_network(
                self.plan, "sha256:" + a.workload.image_digest, a.workload.uid
            )
            if proof.get("state") != "verified":
                raise Denied("namespace proof unknown")
            native = process_identity(s["Pid"])
            self.owned(a)  # process-generation fence after observations
            raw = proof["observation"]
            network = dict(
                network_none=True,
                native_proof=digest(proof),
                loopback_index=raw["interfaces"][0]["index"],
                loopback_flags=raw["interfaces"][0]["flags"],
                route_count=len(raw["routes"]),
            )
            for address in raw["addresses"]:
                family = "ipv6" if ":" in address["address"] else "ipv4"
                network[family + "_prefix"] = address["prefix"]
            for family in ("ipv4", "ipv6"):
                network.update({family + "_" + k: v for k, v in raw["probes"][family].items()})
            result.extend(
                [
                    observation(
                        "namespace",
                        origin="host-procfs",
                        pid=s["Pid"],
                        start_ticks=native["start_ticks"],
                        **{k + "_inode": v["inode"] for k, v in native["namespaces"].items()},
                    ),
                    observation("network", origin="namespace-probe", **network),
                ]
            )
        except Exception:
            result.extend([observation("namespace", "unknown"), observation("network", "unknown")])
        return tuple(result)

    def kill(self, a):
        # Never signal a host PID: full immutable container ID plus generation+nonce.
        if self.absent(a):
            return
        value = self.owned(a)
        if value["State"].get("Paused"):
            # No automatic unpause (would be a second forward effect).
            raise Denied("paused container requires reconciliation")
        if value["State"]["Running"]:
            self.engine.run(["kill", "--signal", "KILL", a.workload.uid])

    def cleanup(self, a):
        if self.absent(a):
            return
        value = self.owned(a)
        if value["State"]["Running"] or value["State"]["Status"] not in {"exited", "created"}:
            raise Denied("cleanup requires termination")
        self.engine.run(["rm", a.workload.uid])
