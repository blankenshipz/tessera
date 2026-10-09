"""Long-lived ordinary experiment worker with injected authenticated backend protocol."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from .ordinary_docker_contract import DockerLease, DockerTarget, digest
from .ordinary_docker_messages import (
    DockerClaim,
    DockerControlReadback,
    DockerDeliveryAck,
    DockerWorkerBackend,
    NativeDockerManifest,
    NativeDockerReport,
)


class DockerServiceError(ValueError):
    """Finite diagnostics only; raw backend/provider errors never enter logs."""


@dataclass(frozen=True)
class DockerWorkerConfig:
    worker_principal_id: str
    control_url: str
    credential_file: Path
    control_ca_file: Path
    workspace: Path
    target: DockerTarget
    request_timeout_seconds: float = 2.0

    def __post_init__(self):
        import re

        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", self.worker_principal_id):
            raise DockerServiceError("fixed worker principal required")
        parsed = urlsplit(self.control_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or (parsed.path not in {"", "/"} or parsed.query or parsed.fragment)
        ):
            raise DockerServiceError("fixed HTTPS control endpoint required")
        if not all(
            isinstance(value, Path) and value.is_absolute()
            for value in (self.credential_file, self.control_ca_file, self.workspace)
        ):
            raise DockerServiceError("absolute runtime references required")
        if not 0 < self.request_timeout_seconds <= 10:
            raise DockerServiceError("bounded backend timeout required")
        DockerTarget.model_validate(self.target.model_dump())

    @property
    def binding_digest(self):
        return digest(
            [
                "ordinary-docker-worker-v1",
                self.worker_principal_id,
                self.control_url,
                self.target.binding.identity_digest,
            ]
        )


@dataclass(frozen=True)
class DockerWorkerStep:
    status: str
    operation_id: str | None = None
    container_id: str | None = None
    termination_confirmed: bool = False
    control_acknowledged: bool = False


class _RenewChannel:
    """Map the existing adapter's finite renewal seam, not guessed HTTP routes."""

    def __init__(self, service, claim):
        self.service, self.claim = service, claim

    def post(self, path, body, key):
        if path != "/worker/leases/renew" or body != {
            "lease_id": self.claim.package.lease_id,
            "expected_epoch": self.claim.package.epoch,
        }:
            raise DockerServiceError("unsupported adapter backend command")
        result = self.service.backend.renew(
            self.claim, key, timeout_seconds=self.service.config.request_timeout_seconds
        )
        return DockerLease.model_validate_json(result.model_dump_json()).model_dump(mode="json")


class OrdinaryDockerWorkerService:
    def __init__(
        self,
        config: DockerWorkerConfig,
        backend: DockerWorkerBackend,
        *,
        adapter_factory,
        now=lambda: datetime.now(UTC),
        sleep=time.sleep,
    ):
        self.config, self.backend = config, backend
        self.adapter_factory, self.now, self.sleep = adapter_factory, now, sleep
        self._signal_stop = False
        root = config.workspace
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = root.stat()
        if root.is_symlink() or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise DockerServiceError("private owned worker workspace required")
        self.lock = os.open(
            root / "ordinary-docker.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.lock)
            raise DockerServiceError("worker workspace is already supervised") from None
        self.path = root / "ordinary-docker-service.sqlite3"
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            if self.path.stat().st_mode & 0o077:
                raise DockerServiceError("private worker journal required")
            with self._db() as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS scans(sequence INTEGER PRIMARY KEY,"
                    "key TEXT NOT NULL,completed INTEGER NOT NULL DEFAULT 0)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS beats(sequence INTEGER PRIMARY KEY,"
                    "key TEXT NOT NULL,completed INTEGER NOT NULL DEFAULT 0)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS operations(operation TEXT PRIMARY KEY,"
                    "claim TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,"
                    "started INTEGER NOT NULL DEFAULT 0,container TEXT,next_renewal TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS outbox(operation TEXT,kind TEXT,sequence INTEGER,"
                    "key TEXT NOT NULL,payload TEXT NOT NULL,payload_digest TEXT NOT NULL,"
                    "semantic_digest TEXT NOT NULL,ack TEXT,PRIMARY KEY(operation,kind,sequence))"
                )
                saved = db.execute("SELECT value FROM meta WHERE key='binding'").fetchone()
                if saved is not None and saved[0] != config.binding_digest:
                    raise DockerServiceError("worker identity/target/endpoint changed")
                db.execute(
                    "INSERT OR IGNORE INTO meta VALUES ('binding',?)", (config.binding_digest,)
                )
        except Exception:
            self.close()
            raise
        self._adapters = {}
        self.next_heartbeat = self.now()

    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def close(self):
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None

    def request_stop(self):
        self._signal_stop = True
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('stop_requested','true')")

    def _stopped(self):
        if self._signal_stop:
            self.request_stop()
            return True
        with self._db() as db:
            return (
                db.execute("SELECT value FROM meta WHERE key='stop_requested'").fetchone()
                is not None
            )

    def _action(self, table):
        if table not in {"scans", "beats"}:
            raise DockerServiceError("closed worker action required")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                f"SELECT * FROM {table} WHERE completed=0 ORDER BY sequence LIMIT 1"
            ).fetchone()
            if row is not None:
                return row["sequence"], row["key"]
            sequence = db.execute(f"SELECT coalesce(max(sequence),0)+1 FROM {table}").fetchone()[0]
            key = "ordinary-docker-service-" + digest([self.config.binding_digest, table, sequence])
            db.execute(f"INSERT INTO {table}(sequence,key) VALUES (?,?)", (sequence, key))
        return sequence, key

    def _heartbeat(self):
        sequence, key = self._action("beats")
        at = self.backend.heartbeat(
            self.config.worker_principal_id,
            key,
            timeout_seconds=self.config.request_timeout_seconds,
        )
        if (
            not isinstance(at, datetime)
            or at.tzinfo is None
            or at < self.now() - timedelta(seconds=30)
            or at > self.now() + timedelta(seconds=1)
        ):
            raise DockerServiceError("worker heartbeat freshness unknown")
        with self._db() as db:
            db.execute("UPDATE beats SET completed=1 WHERE sequence=?", (sequence,))
        self.next_heartbeat = self.now() + timedelta(seconds=30)

    def _active(self):
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM operations WHERE active=1 ORDER BY rowid LIMIT 1"
            ).fetchone()
        return row

    def _claim(self):
        sequence, key = self._action("scans")
        response = self.backend.claim(
            self.config.worker_principal_id,
            self.config.target,
            key,
            timeout_seconds=self.config.request_timeout_seconds,
        )
        claim = (
            None
            if response is None
            else DockerClaim.model_validate_json(response.model_dump_json())
        )
        if claim is not None:
            claim.package.envelope(self.config.target)
            if claim.package.worker_principal_id != self.config.worker_principal_id:
                raise DockerServiceError("claim is assigned to another worker")
        with self._db() as db:
            if claim is not None:
                encoded = claim.model_dump_json()
                old = db.execute(
                    "SELECT claim FROM operations WHERE operation=?", (claim.package.operation_id,)
                ).fetchone()
                if old is not None and old[0] != encoded:
                    raise DockerServiceError("claim replay changed operation descriptor")
                deadline = min(
                    self.now() + timedelta(seconds=10),
                    claim.lease.expires_at - timedelta(seconds=5),
                )
                db.execute(
                    "INSERT OR IGNORE INTO operations(operation,claim,next_renewal) VALUES (?,?,?)",
                    (claim.package.operation_id, encoded, deadline.isoformat()),
                )
            db.execute("UPDATE scans SET completed=1 WHERE sequence=?", (sequence,))
        return claim

    def _authority(self, claim):
        if self._stopped():
            raise DockerServiceError("worker stop requested")
        proof = self.backend.current_authority(
            claim, timeout_seconds=self.config.request_timeout_seconds
        )
        return DockerLease.model_validate_json(proof.model_dump_json())

    def _adapter(self, claim):
        operation = claim.package.operation_id
        if operation not in self._adapters:
            path = self.config.workspace / ("docker-" + operation + ".sqlite3")
            self._adapters[operation] = self.adapter_factory(
                claim, path, lambda _: self._authority(claim)
            )
        return self._adapters[operation]

    def _queue(self, claim, kind, value):
        if kind not in {"report", "cancel", "output"}:
            raise DockerServiceError("closed delivery kind required")
        payload = value.model_dump(mode="json")
        semantic = {key: val for key, val in payload.items() if key != "observed_at"}
        fingerprint, payload_digest = digest(semantic), digest(payload)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            last = db.execute(
                "SELECT * FROM outbox WHERE operation=? AND kind=? ORDER BY sequence DESC LIMIT 1",
                (claim.package.operation_id, kind),
            ).fetchone()
            if last is not None and last["semantic_digest"] == fingerprint:
                return last["key"]
            if kind == "output" and last is not None:
                raise DockerServiceError("retained output descriptor changed")
            sequence = 1 if last is None else last["sequence"] + 1
            key = "ordinary-docker-delivery-" + digest(
                [claim.package.operation_id, kind, sequence, payload_digest]
            )
            db.execute(
                "INSERT INTO outbox(operation,kind,sequence,key,payload,payload_digest,"
                "semantic_digest) VALUES (?,?,?,?,?,?,?)",
                (
                    claim.package.operation_id,
                    kind,
                    sequence,
                    key,
                    json.dumps(payload),
                    payload_digest,
                    fingerprint,
                ),
            )
        return key

    def _ack(self, claim, acknowledgement):
        ack = DockerDeliveryAck.model_validate_json(acknowledgement.model_dump_json())
        if ack.operation_id != claim.package.operation_id:
            raise DockerServiceError("delivery acknowledgement operation mismatch")
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM outbox WHERE operation=? AND key=?", (ack.operation_id, ack.key)
            ).fetchone()
            if row is None or row["payload_digest"] != ack.payload_digest:
                raise DockerServiceError("delivery acknowledgement key/digest mismatch")
            if row["kind"] == "output" and (
                ack.manifest_id is None or ack.custody != "quarantined"
            ):
                raise DockerServiceError("output custody acknowledgement missing")
            if row["ack"] is not None and row["ack"] != ack.model_dump_json():
                raise DockerServiceError("delivery acknowledgement replay changed")
            db.execute(
                "UPDATE outbox SET ack=? WHERE operation=? AND key=?",
                (ack.model_dump_json(), ack.operation_id, ack.key),
            )

    def _read(self, claim, container):
        value = self.backend.read(claim, timeout_seconds=self.config.request_timeout_seconds)
        value = DockerControlReadback.model_validate_json(value.model_dump_json())
        if value.operation_id != claim.package.operation_id or (
            container is not None and value.container_id not in {None, container}
        ):
            raise DockerServiceError("control readback operation/container changed")
        for ack in value.acknowledgements:
            self._ack(claim, ack)
        return value

    def _drain(self, claim, adapter):
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM outbox WHERE operation=? AND ack IS NULL ORDER BY rowid LIMIT 128",
                (claim.package.operation_id,),
            ).fetchall()
        for row in rows:
            if digest(json.loads(row["payload"])) != row["payload_digest"]:
                raise DockerServiceError("durable delivery payload changed")
            if row["kind"] == "output":
                manifest = NativeDockerManifest.model_validate_json(row["payload"])
                outputs = adapter.collect_outputs(claim.package.envelope(self.config.target))
                reconstructed = NativeDockerManifest.from_capture(
                    claim,
                    adapter.output_manifest(claim.package.envelope(self.config.target)),
                    outputs,
                )
                if reconstructed != manifest:
                    raise DockerServiceError("durable native custody descriptor changed")
                ack = self.backend.output(
                    claim,
                    manifest,
                    outputs,
                    row["key"],
                    timeout_seconds=self.config.request_timeout_seconds,
                )
            else:
                report = NativeDockerReport.model_validate_json(row["payload"]).require_claim(claim)
                method = self.backend.cancel if row["kind"] == "cancel" else self.backend.report
                ack = method(
                    claim, report, row["key"], timeout_seconds=self.config.request_timeout_seconds
                )
            self._ack(claim, ack)

    def _pending(self, operation):
        with self._db() as db:
            return db.execute(
                "SELECT count(*) FROM outbox WHERE operation=? AND ack IS NULL", (operation,)
            ).fetchone()[0]

    def step(self):
        row = self._active()
        if row is None:
            if self._stopped():
                return DockerWorkerStep("stopped")
            try:
                if self.now() >= self.next_heartbeat:
                    self._heartbeat()
                claim = self._claim()
            except Exception:
                return DockerWorkerStep("recovery_required")
            if claim is None:
                return DockerWorkerStep("idle")
            row = self._active()
            if row is None:
                return DockerWorkerStep("idle")  # acknowledged replay of already-closed operation
        claim = DockerClaim.model_validate_json(row["claim"])
        envelope = claim.package.envelope(self.config.target)
        adapter = self._adapter(claim)
        cancelled = self._stopped()
        try:
            observation = adapter.observe(envelope)
            if not cancelled:
                readback = self._read(claim, row["container"] or observation.handle.provider_id)
                cancelled = readback.desired == "cancel_requested"
            if cancelled and not observation.termination_confirmed:
                raise DockerServiceError("control/worker stop requested")
            if self.now() >= self.next_heartbeat and not observation.termination_confirmed:
                self._heartbeat()
            # Validate live authority before any new effect, including first startup.
            proof = None if observation.termination_confirmed else self._authority(claim)
            if proof is not None and (
                proof.expires_at <= self.now() or proof.hard_expires_at <= self.now()
            ):
                raise DockerServiceError("worker authority expired")
            if not row["started"]:
                with self._db() as db:
                    db.execute(
                        "UPDATE operations SET started=1 WHERE operation=?",
                        (claim.package.operation_id,),
                    )
                observation = adapter.submit(envelope)
            elif not observation.termination_confirmed and self.now() >= datetime.fromisoformat(
                row["next_renewal"]
            ):
                renewed = adapter.renew(envelope, _RenewChannel(self, claim))
                next_at = min(
                    self.now() + timedelta(seconds=10), renewed.expires_at - timedelta(seconds=5)
                )
                with self._db() as db:
                    db.execute(
                        "UPDATE operations SET next_renewal=? WHERE operation=?",
                        (next_at.isoformat(), claim.package.operation_id),
                    )
            if not observation.termination_confirmed and adapter.ready_for_collection(envelope):
                adapter.collect_outputs(envelope)
            observation = adapter.observe(envelope)
            if observation.state == "succeeded":
                outputs = adapter.collect_outputs(envelope)
                manifest = NativeDockerManifest.from_capture(
                    claim, adapter.output_manifest(envelope), outputs
                )
                self._queue(claim, "output", manifest)
        except Exception:
            cancelled = True
            try:
                adapter.cancel(envelope)
            except Exception:
                pass
            observation = adapter.observe(envelope)
        if observation.handle.provider_id is not None:
            with self._db() as db:
                existing = db.execute(
                    "SELECT container FROM operations WHERE operation=?",
                    (claim.package.operation_id,),
                ).fetchone()[0]
                if existing not in {None, observation.handle.provider_id}:
                    raise DockerServiceError("worker container identity changed")
                db.execute(
                    "UPDATE operations SET container=? WHERE operation=?",
                    (observation.handle.provider_id, claim.package.operation_id),
                )
        stopped = adapter.stop_status(envelope).requested
        report = NativeDockerReport.from_observation(claim, observation, stopped=stopped)
        report = report.model_copy(update={"exit_code": adapter.exit_code(envelope)})
        self._queue(claim, "cancel" if stopped else "report", report)
        try:
            self._drain(claim, adapter)
        except Exception:
            pass  # unknown delivery remains byte/key stable, never rekeyed
        pending = self._pending(claim.package.operation_id)
        terminal = observation.termination_confirmed and observation.state in {
            "succeeded",
            "failed",
        }
        if terminal and not pending:
            with self._db() as db:
                db.execute(
                    "UPDATE operations SET active=0 WHERE operation=?",
                    (claim.package.operation_id,),
                )
        status = (
            "stopped"
            if cancelled and terminal
            else "completed"
            if terminal and not pending
            else "report_draining"
            if terminal
            else "recovery_required"
            if cancelled or observation.state == "unknown"
            else "running"
        )
        return DockerWorkerStep(
            status,
            claim.package.operation_id,
            observation.handle.provider_id,
            observation.termination_confirmed,
            not pending,
        )

    def run(self, *, max_cycles=None, install_signal_handlers=False):
        if max_cycles is not None and (type(max_cycles) is not int or max_cycles < 1):
            raise DockerServiceError("positive test cycle bound required")
        handlers = {}
        if install_signal_handlers:
            for kind in (signal.SIGTERM, signal.SIGINT):
                handlers[kind] = signal.signal(kind, lambda *_: setattr(self, "_signal_stop", True))
        cycles, result = 0, DockerWorkerStep("idle")
        try:
            while True:
                try:
                    result = self.step()
                except Exception:
                    # Corrupt descriptors/model errors can include private argv
                    # in their exception text. Never let those reach service logs.
                    self._signal_stop = True
                    try:
                        self.request_stop()
                    except Exception:
                        pass
                    return DockerWorkerStep("recovery_required")
                cycles += 1
                if self._stopped() or max_cycles is not None and cycles >= max_cycles:
                    return result
                self.sleep(0.25 if result.status == "running" else 1.0)
        finally:
            for kind, previous in handlers.items():
                signal.signal(kind, previous)
