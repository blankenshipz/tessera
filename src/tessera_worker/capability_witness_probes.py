"""Fixed daemon-side probe programs for deployment-owned independent sensors.

These programs run ONLY in a separate authenticated witness service. The node
adapter is a trusted deployment component, not a workload RPC or operator JSON
report. An unsupported adapter must raise CollectionError('unavailable'). There
is deliberately no kubectl/exec-plugin/shell discovery or generic command runner.

The adapter must enforce the deadline out of process (including while a syscall
blocks), never mount credentials in fixtures, and journal creates before issuing
them. Lost create/kill/cleanup replies are unknown, not permission to retry.
"""

from __future__ import annotations

import time
from typing import Protocol

from .capability_kube_client import CollectionError
from .capability_witness import require


class NodeSensors(Protocol):
    """Required node/runtime-specific implementation; NOT broker self-report.

    `prepare` verifies empty nonce-owned fixtures, node UID/boot ID, workload
    security profile and pinned image already present (imagePullPolicy Never).
    It returns a private handle, never an arbitrary path/command from the caller.
    Every resource returned by `resources` came from a successful exact create
    under that handle. Raw outputs are fixture-only; the adapter rejects secrets
    before retaining any API/CRI/CNI output. No workload writes witness custody.
    """

    def prepare(self, request, *, deadline): ...
    def resources(self, handle): ...
    def cleanup(self, handle, *, deadline): ...
    def ancillary_probes(self, handle, requests, *, deadline): ...
    def network_fixture(self, handle, category, *, deadline): ...
    def network_policy(self, handle, *, deadline): ...
    def network_exchange(self, handle, fixture, *, packets, deadline): ...
    def quota_fixture(self, handle, *, bounds, deadline): ...
    def cgroup_snapshot(self, handle, fixture, *, deadline): ...
    def pressure(self, handle, fixture, *, bounds, deadline): ...
    def filesystem_sentinels(self, handle, fixture, *, bounds, deadline): ...
    def namespace_access(self, handle, fixture, *, deadline): ...
    def termination(self, handle, fixture, *, deadline): ...
    def watchdog_fixture(self, handle, trigger, *, deadline): ...
    def watchdog_challenge(self, handle, fixture, *, deadline): ...
    def watchdog_fault(self, handle, fixture, trigger, *, deadline): ...
    def watchdog_observe(self, handle, fixture, *, deadline): ...
    def admission_policy(self, handle, *, deadline): ...
    def admission_dry_runs(self, handle, *, deadline): ...
    def admission_disposable(self, handle, *, deadline): ...


class ProbeProgram:
    kind = None

    def __init__(self, sensors, *, monotonic=time.monotonic):
        self.sensors, self.clock = sensors, monotonic

    def __call__(self, request, *, deadline):
        require(request.kind == self.kind and 0 < deadline - self.clock() <= 5)
        handle = None
        resources, cleanup, probes, observations = (), (), {}, {}
        state = "unknown"
        try:
            handle = self.sensors.prepare(request, deadline=deadline)
            observations = self.run(handle, request, deadline)
            probes = self.sensors.ancillary_probes(
                handle, request.inputs["probes"], deadline=deadline
            )
            resources = self.sensors.resources(handle)
            require(self.clock() < deadline)
            state = "observed"
        except Exception:
            # No raw exception text enters signed custody. Even if the workload
            # wrote a success message, a missing node observation remains unknown.
            state = "unknown"
        finally:
            if handle is not None:
                try:
                    resources = self.sensors.resources(handle)
                    cleanup = self.sensors.cleanup(handle, deadline=deadline)
                except Exception:
                    state = "unknown"
        if self.clock() >= deadline:
            state = "unknown"
        result = dict(
            state=state,
            resources=resources,
            cleanup=cleanup,
            observations=observations,
            probes=probes,
        )
        if handle is not None and hasattr(self.sensors, "native_evidence"):
            result["native"] = self.sensors.native_evidence(handle)
        return result


class CNIWitness(ProbeProgram):
    kind = "cni"

    def run(self, handle, request, deadline):
        # Distinct disposable identities and fresh endpoint UIDs for every case;
        # adapter checks listener health from its own namespace before the deny.
        fixtures = [
            self.sensors.network_fixture(handle, category, deadline=deadline)
            for category in (
                "allowed",
                "allowed-dns",
                "public",
                "private",
                "metadata",
                "control",
                "dns-bypass",
                "proxy",
            )
        ]
        policy = self.sensors.network_policy(handle, deadline=deadline)
        samples = [
            self.sensors.network_exchange(handle, fixture, packets=1, deadline=deadline)
            for fixture in fixtures
        ]
        # Exchanges must include actual direct socket/HTTP/DNS output and CNI
        # per-flow verdict correlation. API policy presence is never sufficient.
        return policy | {"samples": samples}


class QuotaWitness(ProbeProgram):
    kind = "quota"

    def run(self, handle, request, deadline):
        plan = request.inputs["plan"]
        q = plan["activation"]["quota"]
        bounds = {
            "allocation_bytes": q["memory_bytes"] + 1024 * 1024,
            "fork_attempts": plan["pids"] + 1,
            "cpu_milliseconds": 250,
            "output_bytes": q["output_bytes"] + 1,
            "output_inodes": q["output_inodes"] + 1,
            "ephemeral_bytes": q["ephemeral_bytes"] + 1,
        }
        fixture = self.sensors.quota_fixture(handle, bounds=bounds, deadline=deadline)
        before = self.sensors.cgroup_snapshot(handle, fixture, deadline=deadline)
        filesystems = self.sensors.filesystem_sentinels(
            handle, fixture, bounds=bounds, deadline=deadline
        )
        access = self.sensors.namespace_access(handle, fixture, deadline=deadline)
        attempts = self.sensors.pressure(handle, fixture, bounds=bounds, deadline=deadline)
        after = self.sensors.cgroup_snapshot(handle, fixture, deadline=deadline)
        terminal = self.sensors.termination(handle, fixture, deadline=deadline)
        # Read-only node cgroup/projquota observations, not values copied from a
        # Pod spec. Counter increases and kernel termination are checked together.
        return {
            "limits_before": {
                key: before[key]
                for key in (
                    "pod_uid",
                    "pvc_uid",
                    "cgroup_version",
                    "cgroup_id",
                    "cpu_max",
                    "memory_max",
                    "pids_max",
                )
            },
            **{
                key: after[key]
                for key in (
                    "pod_uid",
                    "pvc_uid",
                    "cgroup_version",
                    "cgroup_id",
                    "cpu_max",
                    "memory_max",
                    "pids_max",
                    "memory_peak",
                    "pids_peak",
                )
            },
            **{
                key + "_before": before[key] for key in ("cpu_stat", "memory_events", "pids_events")
            },
            **{key + "_after": after[key] for key in ("cpu_stat", "memory_events", "pids_events")},
            **attempts,
            **terminal,
            "filesystems": filesystems,
            "outside_workspace": access,
        }


class ExternalWatchdogWitness(ProbeProgram):
    kind = "watchdog"

    def run(self, handle, request, deadline):
        fixtures = [
            (trigger, self.sensors.watchdog_fixture(handle, trigger, deadline=deadline))
            for trigger in ("broker-suspended", "revoked")
        ]
        # Arm both harmless write-sentinel fixtures before injecting either fault.
        # Scheduler, monotonic clock and kill credential are outside broker custody.
        for _, fixture in fixtures:
            self.sensors.watchdog_challenge(handle, fixture, deadline=deadline)
        for trigger, fixture in fixtures:
            self.sensors.watchdog_fault(handle, fixture, trigger, deadline=deadline)
        cases = [
            self.sensors.watchdog_observe(handle, fixture, deadline=deadline)
            for _, fixture in fixtures
        ]
        return {
            "watchdog_identity": request.inputs["plan"]["activation"]["watchdog"]["identity"],
            "clock_boot_id": handle.boot_id,
            "cases": cases,
        }


class AdmissionWitness(ProbeProgram):
    kind = "admission"

    def run(self, handle, request, deadline):
        policy = self.sensors.admission_policy(handle, deadline=deadline)
        dry = self.sensors.admission_dry_runs(handle, deadline=deadline)
        # Negative controls are dry-run ONLY: even broken policy must not cause
        # privileged/hostPath/Secret-bearing fixtures to execute. Actual positives
        # require a separately approved harmless, already-present fixture image.
        actual = self.sensors.admission_disposable(handle, deadline=deadline)
        return policy | {"dry_runs": dry, "actual": actual}


class UnavailableNodeSensors:
    """Explicit unsupported platform: no APIs, files, subprocesses or fake success."""

    def prepare(self, request, *, deadline):
        raise CollectionError("unavailable")
