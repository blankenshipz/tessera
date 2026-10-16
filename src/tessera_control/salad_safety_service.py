"""Independent Salad stop/custody HTTPS service. Default CLI has zero effects.

Administrator RPC and workload JWT are distinct identities. Run on a separately
supervised trusted server, never in control/proxy/workload. Runtime configuration,
TLS keys and credential references are explicitly provisioned, not discovered.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import json
import math
import os
import signal
import socket
import ssl
import stat
import struct
import time
from contextlib import contextmanager
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .models import ExecutionSpec
from .remote_execution_models import RemoteExecutionPackage, RemoteProposal, derived_salad_argv
from .salad_custody import CustodyBinding, SaladCustody, private_database
from .salad_guardian import GuardianAssignment, ProviderObservation, SaladGuardian
from .salad_http import SaladHTTPTransport
from .salad_secret_files import PrivateFileSecretResolver
from .salad_workload_jwt import SaladWorkloadJWT, normalized_uuid, strict_json
from .store import _parse_utc, canonical_digest


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


@contextmanager
def alarm_budget(seconds):
    """Absolute main-thread budget, preserving any enclosing request deadline."""

    def expired(*unused):
        raise TimeoutError("service_deadline")

    previous_handler = signal.signal(signal.SIGALRM, expired)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    budget = min(seconds, previous_timer[0]) if previous_timer[0] else seconds
    signal.setitimer(signal.ITIMER_REAL, budget)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            signal.setitimer(
                signal.ITIMER_REAL,
                max(0.000001, previous_timer[0] - (time.monotonic() - started)),
                previous_timer[1],
            )


def private_read(path, cap=16384):
    path = Path(path).absolute()
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError("unsafe_file")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size > cap
        ):
            raise ValueError("unsafe_file")
        return os.read(fd, cap + 1)
    finally:
        os.close(fd)


class SaladSafetyService:
    """Single-thread service owns all SQLite connections and guardian execution."""

    def __init__(
        self,
        directory,
        *,
        organization_ids,
        resolver,
        reference,
        version,
        destination,
        verifier=None,
        transport_factory=SaladHTTPTransport,
        clock=time.time,
    ):
        root = Path(directory)
        self.clock, self.resolver = clock, resolver
        self.organization_ids, self.reference, self.version = organization_ids, reference, version
        self.destination, self.transport_factory = destination, transport_factory
        self.jwt = verifier or SaladWorkloadJWT(clock=clock)
        self.custody = SaladCustody(root / "custody.db", clock=clock)
        self.db = private_database(root / "service.db")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS reservations (
                reservation TEXT PRIMARY KEY, operation TEXT UNIQUE, tenant TEXT,
                project TEXT, proposal TEXT, binding TEXT, deadline REAL,
                heartbeat REAL, provider_id TEXT, allocation TEXT, machine TEXT,
                begun INTEGER DEFAULT 0, output INTEGER DEFAULT 0,
                recovery INTEGER DEFAULT 0, seen_running INTEGER DEFAULT 0);
        """)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(reservations)")}
        if "routing_package" not in columns:
            self.db.execute("ALTER TABLE reservations ADD COLUMN routing_package TEXT")
        if "route_id" not in columns:
            self.db.execute("ALTER TABLE reservations ADD COLUMN route_id TEXT")
        if "runtime_package" not in columns:
            self.db.execute("ALTER TABLE reservations ADD COLUMN runtime_package TEXT")
        if "allocation_uncertain" not in columns:
            self.db.execute(
                "ALTER TABLE reservations ADD COLUMN allocation_uncertain INTEGER DEFAULT 0"
            )
        for name, declaration in {
            "rate": "TEXT",
            "rate_at": "REAL",
            "accrued_cents": "TEXT DEFAULT '0'",
            "accounted_at": "REAL",
            "cost_cap": "INTEGER",
            "budget_fenced": "INTEGER DEFAULT 0",
            "rate_unknown": "INTEGER DEFAULT 1",
            "escalation": "INTEGER DEFAULT 0",
        }.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE reservations ADD COLUMN {name} {declaration}")
        with self.db:
            self.db.execute("UPDATE reservations SET recovery=1")
        self.guardian = SaladGuardian(
            root / "guardian.db",
            custody=self.custody,
            observe=self._observe,
            stop=self._stop,
            clock=clock,
        )
        self.loop_at = None
        self.next_poll = {}
        self.next_rate = {}

    def close(self):
        self.guardian.close()
        self.custody.db.close()
        self.db.close()

    def _row(self, reservation=None, operation=None):
        self.db.row_factory = __import__("sqlite3").Row
        row = self.db.execute(
            "SELECT * FROM reservations WHERE "
            + ("reservation=?" if reservation else "operation=?"),
            (reservation or operation,),
        ).fetchone()
        if not row:
            raise ValueError("reservation_missing")
        return dict(row)

    def _send(self, row, action, provider_id=None):
        with alarm_budget(6):
            return self._bounded_send(row, action, provider_id)

    # Explicit admission reserve for stop/reconciliation after the run deadline.
    # This is not automatic renewal or proof the provider can always terminate.
    STOP_RECOVERY_MARGIN_SECONDS = 60

    def _credential(self):
        metadata = self.resolver.metadata(self.reference)
        now = self.clock()
        if (
            metadata.version != self.version
            or metadata.revoked
            or metadata.purpose != "salad_management"
            or not 0 <= now - metadata.checked_at <= 5
            or metadata.expires_at <= now + 2
        ):
            raise ValueError("credential_unavailable")
        return metadata

    def _require_stop_coverage(self, deadline):
        metadata = self._credential()
        if metadata.expires_at <= deadline + self.STOP_RECOVERY_MARGIN_SECONDS:
            raise ValueError("stop_credential_window_insufficient")

    def _bounded_send(self, row, action, provider_id):
        proposal = RemoteProposal.model_validate_json(row["proposal"])
        self._credential()
        with self.resolver.resolve(self.reference, self.version) as secret:
            transport = self.transport_factory(proposal.selection)
            if action == "gpu_class":
                result = transport.gpu_class(proposal.selection, secret)
            else:
                result = transport.send(action, proposal.selection, provider_id, secret)
        if result.status not in {"observed", "pending"}:
            raise ValueError("provider_unknown")
        return result

    def _rate(self, row):
        result = self._send(row, "gpu_class")
        o = result.observation
        p = RemoteProposal.model_validate_json(row["proposal"])
        if (
            result.status != "observed"
            or type(o) is not dict
            or o.get("selection_digest") != canonical_digest(p.selection.model_dump(mode="json"))
            or normalized_uuid(o.get("gpu_class_id"))
            != normalized_uuid(p.selection.resources.gpu_class)
            or o.get("price_basis") != "published_gpu_hourly_rate_not_actual_cost"
            or type(o.get("hourly_price_usd")) is not str
        ):
            raise ValueError("rate_unknown")
        at = _parse_utc(o["observed_at"]).timestamp()
        price = Decimal(o["hourly_price_usd"])
        if not price.is_finite() or price <= 0 or not 0 <= self.clock() - at <= 5:
            raise ValueError("rate_unknown")
        return price, at

    def _account(self, row, *, force=False):
        now, last = self.clock(), row["accounted_at"]
        if last is None:
            return
        try:
            stopped = (
                row["provider_id"] and self.guardian.status(row["operation"])["status"] == "stopped"
            )
        except ValueError:
            stopped = False
        estimate = Decimal(row["accrued_cents"])
        if (not stopped or force) and row["rate"]:
            estimate += Decimal(row["rate"]) * Decimal(str(max(0, now - last))) * 100 / 3600
        escalation = row["cost_cap"] is not None and estimate > row["cost_cap"]
        with self.db:
            self.db.execute(
                "UPDATE reservations SET accrued_cents=?,accounted_at=?,"
                "escalation=MAX(escalation,?) WHERE reservation=?",
                (format(estimate, "f"), max(now, last), int(escalation), row["reservation"]),
            )

    def _budget(self, row, *, refresh):
        self._account(row)
        row = self._row(row["reservation"])
        try:
            price, at = (
                self._rate(row)
                if refresh
                else (Decimal(row["rate"]) if row["rate"] else None, row["rate_at"])
            )
        except Exception:
            self._exposure_fence(row)
            raise ValueError("rate_unknown") from None
        changed = row["rate"] is not None and price is not None and price != Decimal(row["rate"])
        if changed:
            price = max(price, Decimal(row["rate"]))
        fresh = price is not None and at is not None and 0 <= self.clock() - at <= 5
        required = (
            Decimal(row["accrued_cents"])
            + (price or Decimal(0))
            * Decimal(str(max(0, row["deadline"] - self.clock())))
            * 100
            / 3600
        )
        fenced = changed or not fresh or row["budget_fenced"] or required > row["cost_cap"]
        with self.db:
            self.db.execute(
                "UPDATE reservations SET rate=?,rate_at=?,rate_unknown=?,"
                "budget_fenced=MAX(budget_fenced,?) WHERE reservation=?",
                (
                    format(price, "f") if price else None,
                    at,
                    int(not fresh or changed),
                    int(fenced),
                    row["reservation"],
                ),
            )
        if fenced:
            self._exposure_fence(row)
            raise ValueError("exposure_fenced")

    def _exposure_fence(self, row):
        with self.db:
            self.db.execute(
                "UPDATE reservations SET recovery=1,budget_fenced=1,rate_unknown=1 "
                "WHERE reservation=?",
                (row["reservation"],),
            )
        try:
            self.custody.close_writers(CustodyBinding(**json.loads(row["binding"])))
        finally:
            if row["provider_id"]:
                try:
                    self.guardian.cancel(row["operation"])
                except ValueError:
                    pass

    def _status(self, row):
        self._account(row)
        row = self._row(row["reservation"])
        status = (
            self.guardian.status(row["operation"])
            if row["provider_id"]
            else dict(status="reserved", quarantine=True)
        )
        status.update(
            estimated_exposure_cents=row["accrued_cents"],
            actual_billed_cost_cents=None,
            published_hourly_rate_usd=row["rate"],
            rate_unknown=bool(row["rate_unknown"]),
            exposure_unknown=True,
            exposure_pending=True,
            budget_fenced=bool(row["budget_fenced"]),
            escalation_pending=bool(row["escalation"]),
            financial_hard_cap=False,
        )
        status["retained_output"] = self.custody.retained_metadata(
            CustodyBinding(**json.loads(row["binding"])), "output.json"
        )
        status["allocation_uncertain"] = bool(row["allocation_uncertain"]) or status.get(
            "allocation_uncertain", False
        )
        return status

    def _provider(self, row):
        result = self._send(
            row, "read_instance" if row["provider_id"] else "read_group", row["provider_id"]
        )
        observation = result.observation
        if type(observation) is not dict or not observation.get("provider_id"):
            raise ValueError("provider_unknown")
        group = normalized_uuid(observation["provider_id"])
        if row["provider_id"] and group != row["provider_id"]:
            raise ValueError("provider_identity_changed")
        with self.db:
            self.db.execute(
                "UPDATE reservations SET provider_id=? WHERE reservation=?",
                (group, row["reservation"]),
            )
        if "instances" not in observation:
            observation = self._send(
                self._row(row["reservation"]), "read_instance", group
            ).observation
        instances = observation.get("instances")
        if type(instances) is not list or len(instances) > 1:
            raise ValueError("allocation_unknown")
        allocation = row["allocation"] or "unallocated"
        machine = row["machine"]
        if instances:
            instance = instances[0]
            machine = normalized_uuid(instance["machine_id"])
            allocation = normalized_uuid(instance["instance_id"]) + ":" + machine
            if row["allocation"] and (allocation != row["allocation"] or machine != row["machine"]):
                # Return changed identity to guardian, never adopt replacement.
                with self.db:
                    self.db.execute(
                        "UPDATE reservations SET allocation_uncertain=1,recovery=1 "
                        "WHERE reservation=?",
                        (row["reservation"],),
                    )
                self.custody.close_writers(CustodyBinding(**json.loads(row["binding"])))
                return observation, group, allocation, machine
            with self.db:
                self.db.execute(
                    "UPDATE reservations SET allocation=?,machine=? WHERE reservation=?",
                    (allocation, machine, row["reservation"]),
                )
        return observation, group, allocation, machine

    def _assignment(self, row):
        proposal = RemoteProposal.model_validate_json(row["proposal"])
        p = proposal.selection.salad
        return GuardianAssignment(
            CustodyBinding(**json.loads(row["binding"])),
            p.organization,
            p.project,
            row["provider_id"],
            p.group,
            "unallocated",
            proposal.selection.manifests.image,
            row["deadline"],
            poll_seconds=15,
        )

    def _observe(self, assignment):
        row = self._row(operation=assignment.binding.operation)
        observation, group, allocation, _ = self._provider(row)
        counts = observation.get("instance_status_counts")
        terminal = (
            observation.get("state") == "stopped"
            and type(counts) is dict
            and set(counts)
            == {"allocating_count", "creating_count", "running_count", "stopping_count"}
            and all(type(v) is int and v == 0 for v in counts.values())
            and observation.get("pending_change") is False
            and not observation.get("instances")
        )
        if observation.get("state") == "running":
            with self.db:
                self.db.execute(
                    "UPDATE reservations SET seen_running=1 WHERE reservation=?",
                    (row["reservation"],),
                )
        status = self.guardian.status(assignment.binding.operation)
        stopping = status["reason"] != "armed" or row["recovery"]
        state = (
            "stopped"
            if terminal and (row["begun"] or row["seen_running"] or stopping)
            else "pending"
        )
        if observation.get("state") == "running":
            state = "running"
        if row["allocation_uncertain"]:
            state = "unknown"
        return ProviderObservation(
            assignment.organization,
            assignment.provider_project,
            group,
            assignment.group_name,
            allocation,
            assignment.image_digest,
            state,
            self.clock(),
            prior_allocation_id=row["allocation"],
        )

    def _stop(self, assignment):
        row = self._row(operation=assignment.binding.operation)
        if row["provider_id"] != assignment.group_id:
            raise ValueError("stop_identity_mismatch")
        return self._send(row, "stop", assignment.group_id).status

    @staticmethod
    def _request(argv):
        if len(argv) != 10 or argv[:2] != ["v1", "public-integers-v1"]:
            raise ValueError("unsupported_workload")
        fields = (
            "version",
            "public_input",
            "operation",
            "epoch",
            "destination",
            "watchdog",
            "max_seconds",
            "max_output_bytes",
            "max_output_files",
            "dimension",
        )
        values = [*argv[:6], *map(int, argv[6:])]
        request = dict(zip(fields, values, strict=True))
        if not (
            1 <= request["max_seconds"] <= 300
            and 256 <= request["max_output_bytes"] <= 65536
            and request["max_output_files"] == 1
            and 2 <= request["dimension"] <= 32
        ):
            raise ValueError("unsupported_workload")
        return request

    def admin(self, body):
        action, reservation = body["operation"], body["reservation"]
        fields = {"operation", "reservation"}
        if action == "reserve":
            fields |= {
                "route_id",
                "org_id",
                "project_id",
                "proposal",
                "routing_package",
                "runtime_package",
                "route_digest",
                "deadline",
            }
        elif action == "bind":
            fields |= {"provider_id", "allocation_id"}
        if set(body) != fields:
            raise ValueError("invalid_admin_request")
        now = self.clock()
        if action == "reserve":
            p = RemoteProposal.model_validate(body["proposal"])
            package = RemoteExecutionPackage.model_validate(body["routing_package"])
            if (
                package.proposal != p
                or package.org_id != body["org_id"]
                or package.project_id != body["project_id"]
                or type(body["deadline"]) not in {int, float}
                or not math.isfinite(body["deadline"])
            ):
                raise ValueError("reservation_mismatch")
            profile = p.selection.salad
            if (
                not profile
                or not profile.execution
                or profile.organization not in self.organization_ids
            ):
                raise ValueError("provider_scope_unenrolled")
            runtime = body["runtime_package"]
            if (
                type(runtime) is not dict
                or canonical_digest(runtime) != p.package_digest
                or runtime["operation_id"] != p.operation_id
                or runtime["epoch"] != p.epoch
                or runtime["lease_id"] != p.lease_id
                or runtime["project_id"] != package.project_id
                or runtime["task_id"] != package.task_id
                or runtime.get("org_id", package.org_id) != package.org_id
                or runtime["plan_digest"] != p.plan_digest
                or canonical_digest(runtime["plan"]) != p.plan_digest
                or runtime["pinned_image"] != p.selection.manifests.image
                or runtime["snapshot_digest"] != p.selection.manifests.data
                or body["deadline"] > _parse_utc(runtime["hard_expires_at"]).timestamp()
            ):
                raise ValueError("runtime_package_mismatch")
            plan = runtime["plan"]
            spec = ExecutionSpec.model_validate(plan["execution_spec"])
            argv = derived_salad_argv(
                profile.execution.model_dump(mode="json"),
                spec.argv,
                p.operation_id,
                p.epoch,
                p.selection.resources.max_seconds,
            )
            if (
                argv != profile.execution.argv
                or profile.execution.max_output_bytes > spec.max_output_bytes
                or spec.output_paths != ["output.json"]
                or profile.execution.max_output_files != len(spec.output_paths)
                or spec.input_snapshot_digest != runtime["snapshot_digest"]
                or profile.execution.max_cost_cents > plan["budget_cents"]
                or p.selection.resources.max_seconds > plan["max_duration_seconds"]
            ):
                raise ValueError("approved_spec_mismatch")
            request = self._request(profile.execution.argv)
            if (
                request["operation"] != p.operation_id
                or request["epoch"] != str(p.epoch)
                or request["destination"] != self.destination
                or request["watchdog"] != profile.execution.watchdog_profile
                or request["max_seconds"] > p.selection.resources.max_seconds
                or request["max_output_bytes"] > profile.execution.max_output_bytes
                or request["max_output_files"] > profile.execution.max_output_files
                or reservation != canonical_digest([body["route_id"], body["route_digest"]])
                or body["route_digest"] != canonical_digest(package.model_dump(mode="json"))
                or not now < body["deadline"] <= now + request["max_seconds"]
            ):
                raise ValueError("reservation_mismatch")
            binding = CustodyBinding(
                body["org_id"],
                body["project_id"],
                p.operation_id,
                p.epoch,
                profile.execution.artifact_destination,
            )
            serialized = p.model_dump_json()
            self._require_stop_coverage(body["deadline"])
            rate, rate_at = self._rate({"proposal": serialized})
            if (
                rate * Decimal(str(body["deadline"] - now)) * 100 / 3600
                > profile.execution.max_cost_cents
            ):
                raise ValueError("budget_insufficient")
            others = self.db.execute(
                "SELECT reservation FROM reservations WHERE operation!=?", (p.operation_id,)
            ).fetchall()
            for (other,) in others:
                if self._status(self._row(other))["status"] != "stopped":
                    raise ValueError("single_operation_orphan_exposure")
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO reservations(reservation,operation,tenant,project,"
                    "proposal,binding,deadline,heartbeat,routing_package,route_id,runtime_package) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        reservation,
                        p.operation_id,
                        body["org_id"],
                        body["project_id"],
                        serialized,
                        binding.key(),
                        body["deadline"],
                        now,
                        package.model_dump_json(),
                        body["route_id"],
                        encoded(runtime).decode(),
                    ),
                )
                row = self._row(reservation)
                if (
                    row["proposal"] != serialized
                    or row["binding"] != binding.key()
                    or row["routing_package"] != package.model_dump_json()
                    or row["route_id"] != body["route_id"]
                    or row["runtime_package"] != encoded(runtime).decode()
                    or body["deadline"] < row["deadline"]
                ):
                    raise ValueError("reservation_mismatch")
                if row["accounted_at"] is None:
                    self.db.execute(
                        "UPDATE reservations SET rate=?,rate_at=?,accounted_at=?,cost_cap=?,"
                        "rate_unknown=0 WHERE reservation=?",
                        (
                            format(rate, "f"),
                            rate_at,
                            now,
                            profile.execution.max_cost_cents,
                            reservation,
                        ),
                    )
            # Aggregate operation cap is allocated once to one bounded output;
            # the fixed smoke supports no extra checkpoint/upload destinations.
            self.custody.register(
                binding,
                byte_cap=request["max_output_bytes"],
                file_cap=request["max_output_files"],
                expected_files=1,
            )
        row = self._row(reservation)
        if action in {"bind", "ready", "heartbeat"}:
            self._require_stop_coverage(row["deadline"])
        if action == "bind":
            self._budget(row, refresh=True)
            # Caller-supplied allocation/group is not evidence. Independently
            # reconcile the exact approved selection through authenticated API.
            _, group, _, _ = self._provider(row)
            if normalized_uuid(body["provider_id"]) != group:
                raise ValueError("provider_identity_mismatch")
            row = self._row(reservation)
            self.guardian.arm(self._assignment(row))
        elif action in {"heartbeat", "ready"}:
            if row["recovery"] or now >= row["deadline"]:
                raise ValueError("stop_only")
            self._budget(row, refresh=True)
            if action == "heartbeat":
                with self.db:
                    self.db.execute(
                        "UPDATE reservations SET heartbeat=? WHERE reservation=?",
                        (now, reservation),
                    )
                if row["provider_id"]:
                    self.guardian.heartbeat(row["operation"])
        elif action == "cancel":
            with self.db:
                self.db.execute(
                    "UPDATE reservations SET recovery=1 WHERE reservation=?", (reservation,)
                )
            self.custody.close_writers(CustodyBinding(**json.loads(row["binding"])))
            if row["provider_id"]:
                self.guardian.cancel(row["operation"])
        elif action == "status":
            return self._status(row)
        elif action == "import":
            # A cancelled/unknown guardian is not a terminal provider readback.
            # Sealing here would otherwise close writers and expose retained bytes
            # while the GPU could still be running (or its allocation uncertain).
            if (
                not row["provider_id"]
                or self.guardian.status(row["operation"])["status"] != "stopped"
            ):
                raise ValueError("writers_not_stopped")
            binding = CustodyBinding(**json.loads(row["binding"]))
            manifest = self.custody.seal_operation(binding)
            return dict(
                manifest=manifest,
                output=strict_json(self.custody.read_retained(binding, "output.json")),
            )
        elif action not in {"reserve", "bind"}:
            raise ValueError("unsupported_operation")
        if self.loop_at is None or not 0 <= self.clock() - self.loop_at <= 2:
            raise ValueError("guardian_loop_unready")
        if self.jwt.refreshed is None or not 0 <= self.clock() - self.jwt.refreshed <= 3600:
            raise ValueError("workload_verifier_unready")
        # Verify durable custody closure and readable manifest state, not config flags.
        if action in {"reserve", "ready", "bind", "heartbeat"}:
            inventory = self.custody.db.execute(
                "SELECT closed FROM inventories WHERE binding=?", (row["binding"],)
            ).fetchone()
            if not inventory or inventory[0] or row["recovery"]:
                raise ValueError("custody_unready")
            if row["provider_id"]:
                state = self.guardian.status(row["operation"])
                if state["status"] != "armed" or state["allocation_uncertain"]:
                    raise ValueError("guardian_unready")
        return dict(reservation=reservation, state="ready", observed_at=self.loop_at)

    def loop(self):
        now = self.clock()
        if self.jwt.refreshed is None or now - self.jwt.refreshed >= 1800:
            try:
                with alarm_budget(2):
                    self.jwt.refresh()
            except Exception:
                pass
        for (reservation,) in self.db.execute("SELECT reservation FROM reservations").fetchall():
            row = self._row(reservation)
            self._account(row)
            if row["provider_id"]:
                try:
                    if self.guardian.status(row["operation"])["status"] == "stopped":
                        continue
                except ValueError:
                    pass
            expired = (
                now >= row["deadline"] or now - row["heartbeat"] >= 30 or now < row["heartbeat"]
            )
            if now >= self.next_rate.get(reservation, 0):
                try:
                    self._budget(row, refresh=True)
                except Exception:
                    expired = True
                    with self.db:
                        self.db.execute(
                            "UPDATE reservations SET budget_fenced=1,rate_unknown=1 "
                            "WHERE reservation=?",
                            (reservation,),
                        )
                self.next_rate[reservation] = self.clock() + 5
            if expired and not row["recovery"]:
                with self.db:
                    self.db.execute(
                        "UPDATE reservations SET recovery=1 WHERE reservation=?", (reservation,)
                    )
                row["recovery"] = 1
            if not row["provider_id"]:
                if row["recovery"]:
                    # Lost-create recovery is read by immutable reserved name only.
                    try:
                        self._provider(row)
                        row = self._row(reservation)
                        self.guardian.arm(self._assignment(row), stop_only=True)
                    except Exception:
                        self.custody.close_writers(CustodyBinding(**json.loads(row["binding"])))
                        continue
                else:
                    continue
            if row["recovery"]:
                self.guardian.arm(self._assignment(row), stop_only=True)
                self.guardian.cancel(row["operation"])
            else:
                self.guardian.arm(self._assignment(row))
            state = self.guardian.status(row["operation"])
            urgent = row["recovery"] and state["status"] == "armed"
            if now < self.next_poll.get(reservation, 0) and not urgent:
                continue
            try:
                self.guardian.tick(row["operation"])
                if self.guardian.status(row["operation"])["status"] == "stopped":
                    self._account(self._row(reservation), force=True)
            except Exception:
                # No failed observation/custody write is a confirmed stop.
                pass
            self.next_poll[reservation] = self.clock() + 5
        self.loop_at = self.clock()

    def workload(self, path, body, token):
        request = body["binding"]
        if type(request) is not dict:
            raise ValueError("workload_rejected")
        row = self._row(operation=request.get("operation"))
        proposal = RemoteProposal.model_validate_json(row["proposal"])
        expected = self._request(proposal.selection.salad.execution.argv)
        if (
            encoded(request) != encoded(expected)
            or row["recovery"]
            or self.clock() >= row["deadline"]
            or not row["provider_id"]
            or not row["machine"]
            or self.loop_at is None
            or self.clock() - self.loop_at > 2
        ):
            raise ValueError("workload_rejected")
        status = self.guardian.status(row["operation"])
        if status["status"] != "armed" or status["allocation_uncertain"]:
            raise ValueError("workload_rejected")
        observation, group, allocation, machine = self._provider(row)
        if (
            allocation != row["allocation"]
            or machine != row["machine"]
            or observation.get("state") != "running"
        ):
            raise ValueError("workload_rejected")
        self.jwt.verify(
            token,
            organization_id=self.organization_ids[proposal.selection.salad.organization],
            organization_name=proposal.selection.salad.organization,
            group_id=group,
            group_name=proposal.selection.salad.group,
            machine_id=machine,
        )
        if self.clock() >= row["deadline"] or self.clock() - row["heartbeat"] >= 30:
            raise ValueError("workload_rejected")
        if path == "/v1/workload/begin-once":
            self._budget(row, refresh=True)
            if set(body) != {"binding"}:
                raise ValueError("workload_rejected")
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                changed = self.db.execute(
                    "UPDATE reservations SET begun=1 WHERE operation=? AND begun=0 AND recovery=0",
                    (row["operation"],),
                ).rowcount
                if changed != 1:
                    raise ValueError("operation_consumed")
            return dict(
                schema="tessera.begin-once.v1",
                granted=True,
                operation=request["operation"],
                epoch=request["epoch"],
            )
        if (
            path != "/v1/workload/output"
            or set(body) != {"binding", "sha256", "output"}
            or not row["begun"]
        ):
            raise ValueError("workload_rejected")
        data = encoded(body["output"])
        digest = hashlib.sha256(data).hexdigest()
        if digest != body["sha256"] or len(data) > expected["max_output_bytes"]:
            raise ValueError("output_rejected")
        binding = CustodyBinding(**json.loads(row["binding"]))
        # Durable ticket reservation before byte insertion. A lost response or
        # crash never overwrites/reissues this filename or clears operation begun.
        ticket = self.custody.issue(
            binding,
            name="output.json",
            checksum=digest,
            size=len(data),
            expires=min(row["deadline"], self.clock() + 60),
        )
        self.custody.upload(ticket, binding, data)
        with self.db:
            self.db.execute(
                "UPDATE reservations SET output=1 WHERE reservation=?", (row["reservation"],)
            )
        return dict(
            schema="tessera.retained.v1",
            retained=True,
            sha256=digest,
            operation=request["operation"],
            epoch=request["epoch"],
        )


class SafetyHTTPServer(HTTPServer):
    """Bounded single-thread listener; handlers cannot race the guardian owner."""

    def __init__(self, address, service, admin_token, *, context):
        if len(admin_token) < 32:
            raise ValueError("admin_credential_unavailable")
        self.service, self.admin_token = service, admin_token
        super().__init__(address, SafetyHandler)
        self.timeout = 0.2
        self.socket = context.wrap_socket(
            self.socket, server_side=True, do_handshake_on_connect=False
        )

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(2)
        try:
            request.do_handshake()
        except Exception:
            request.close()
            raise
        return request, address

    def finish_request(self, request, address):
        # Socket idle timeouts alone permit endless trickling. This process owns
        # its main thread and signals; cap the entire parse/auth/dispatch budget.
        with alarm_budget(10):
            super().finish_request(request, address)

    def handle_error(self, request, address):
        pass  # Never emit HTTP/provider exception tracebacks.


class SafetyHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # No URL, token, body, claims or raw exception logging.

    def do_POST(self):
        status = 400
        try:
            if (
                self.headers.get("Transfer-Encoding") is not None
                or self.headers.get("Content-Type") != "application/json"
                or len(self.headers.get_all("Content-Length", [])) != 1
                or len(self.headers.get_all("Authorization", [])) != 1
            ):
                raise ValueError("invalid_request")
            length = int(self.headers["Content-Length"])
            if not 1 <= length <= 131072:
                raise ValueError("invalid_request")
            auth = self.headers["Authorization"]
            if not auth.startswith("Bearer ") or len(auth) > 2048:
                raise ValueError("invalid_auth")
            body = strict_json(self.rfile.read(length))
            if type(body) is not dict:
                raise ValueError("invalid_request")
            if self.path == "/v1/admin":
                status = 401
                if not hmac.compare_digest(auth[7:].encode(), self.server.admin_token):
                    raise ValueError("invalid_auth")
                result = self.server.service.admin(body)
            elif self.path in {"/v1/workload/begin-once", "/v1/workload/output"}:
                status = 401
                result = self.server.service.workload(self.path, body, auth[7:])
            else:
                raise ValueError("invalid_request")
            self._reply(200, result)
        except Exception:
            self._reply(status, {"error": "request_rejected"})

    def _reply(self, status, value):
        raw = encoded(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)


class SafetyHTTPSClient:
    def __init__(self, host, port, *, token_file, ca_file, certificate_sha256):
        self.host, self.port, self.token_file = host, port, token_file
        self.context = ssl.create_default_context(cafile=ca_file)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_3
        self.pin = certificate_sha256

    def exchange(self, body):
        token = private_read(self.token_file, 256).strip()
        connection = http.client.HTTPSConnection(
            self.host, self.port, timeout=10, context=self.context
        )
        try:
            connection.connect()
            if (
                hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
                != self.pin
            ):
                raise ValueError("service_identity_mismatch")
            connection.request(
                "POST",
                "/v1/admin",
                encoded(body),
                {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + token.decode("ascii"),
                },
            )
            response = connection.getresponse()
            raw = response.read(131073)
            if response.status != 200 or len(raw) > 131072:
                raise ValueError("service_unavailable")
            return strict_json(raw)
        except Exception:
            raise ValueError("service_unavailable") from None
        finally:
            connection.close()


def serve(service, server, *, shutdown=lambda: False, unix_listener=None, proxy_uid=None):
    while not shutdown():
        service.loop()
        if unix_listener is not None:
            try:
                connection, _ = unix_listener.accept()
            except TimeoutError:
                pass
            else:
                with connection:
                    try:
                        _, uid, _ = struct.unpack(
                            "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                        )
                        if uid != proxy_uid:
                            raise ValueError("proxy_identity_rejected")
                        connection.settimeout(2)
                        deadline = time.monotonic() + 2
                        raw = bytearray()
                        while not raw.endswith(b"\n"):
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise ValueError("frame_deadline")
                            connection.settimeout(remaining)
                            part = connection.recv(4096)
                            if not part or len(raw) + len(part) > 16384:
                                raise ValueError("invalid_frame")
                            raw.extend(part)
                        reply = service.admin(strict_json(raw))
                    except Exception:
                        reply = {"error": "request_rejected"}
                    connection.sendall(encoded(reply) + b"\n")
        server.handle_request()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    if not args.run:
        print('{"state":"disabled","effects":0}')
        return 0
    if not args.config:
        return 2
    service = None
    try:
        cfg = strict_json(private_read(args.config, 16384))
        resolver = PrivateFileSecretResolver(cfg["credential_directory"])
        jwt = SaladWorkloadJWT()
        with alarm_budget(2):
            jwt.refresh()  # Explicit run only; fixed public JWKS, never provider effect.
        service = SaladSafetyService(
            cfg["state_directory"],
            organization_ids=cfg["organization_ids"],
            resolver=resolver,
            reference=cfg["credential_reference"],
            version=cfg["credential_version"],
            destination=cfg["destination"],
            verifier=jwt,
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        private_read(cfg["tls_key"], 16384)
        context.load_cert_chain(cfg["tls_certificate"], cfg["tls_key"])
        token = private_read(cfg["admin_token_file"], 256).strip()
        server = SafetyHTTPServer(
            (cfg["listen_host"], cfg["listen_port"]), service, token, context=context
        )
        listener = None
        if cfg.get("unix_socket"):
            if not hasattr(socket, "SO_PEERCRED"):
                raise ValueError("linux_peer_credentials_required")
            socket_path = Path(cfg["unix_socket"])
            info = socket_path.parent.stat()
            if (
                info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700
                or socket_path.exists()
            ):
                raise ValueError("unsafe_socket")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            os.chmod(socket_path, 0o600)
            listener.listen(8)
            listener.settimeout(0.01)
        serve(service, server, unix_listener=listener, proxy_uid=cfg.get("proxy_uid"))
    except KeyboardInterrupt:
        return 0
    except Exception:
        print('{"state":"unavailable","reason":"service_prerequisite_or_runtime_failure"}')
        return 2
    finally:
        if service:
            service.close()


if __name__ == "__main__":
    raise SystemExit(main())
