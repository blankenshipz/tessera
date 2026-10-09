"""Journaled, UID-fenced ordinary Docker execution behind a fixed worker transport."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import tarfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .execution_contracts import (
    AccountingObservation,
    CancellationObservation,
    CollectionHandoff,
    ExecutionCapabilities,
    Observation,
    OperationHandle,
    PreflightResult,
)
from .ordinary_docker_contract import DockerLease, DockerPackage, DockerTarget, digest
from .outputs import CollectedOutput


class DockerExecutionError(ValueError):
    pass


def archive_files(data: bytes, *, max_bytes: int, max_files: int = 128) -> dict[str, bytes]:
    if not isinstance(data, bytes) or len(data) > max_bytes + (max_files + 2) * 4096 + 10240:
        raise DockerExecutionError("bounded Docker archive required")
    result, total = {}, 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for index, member in enumerate(archive):
                if index > max_files * 4 or member.pax_headers:
                    raise DockerExecutionError("archive metadata limit exceeded")
                parts = member.name.split("/")
                if any(p in {"", ".", ".."} for p in parts) or member.name.startswith("/"):
                    raise DockerExecutionError("unsafe archive name")
                if member.isdir():
                    continue
                if not member.isfile() or member.name in result or len(result) >= max_files:
                    raise DockerExecutionError("archive type/replay limit exceeded")
                total += member.size
                if member.size < 0 or total > max_bytes:
                    raise DockerExecutionError("archive byte limit exceeded")
                source = archive.extractfile(member)
                if source is None:
                    raise DockerExecutionError("archive file missing")
                content = source.read(member.size + 1)
                if len(content) != member.size:
                    raise DockerExecutionError("archive truncated")
                result[member.name] = content
    except (tarfile.TarError, OSError):
        raise DockerExecutionError("invalid Docker archive") from None
    return result


class OrdinaryDockerExecutionAdapter:
    """Trusted worker seam; current_authority is an authenticated control recheck.

    That callback must fail on revoked grants/credentials/task revisions. Merely
    constructing an envelope is not authority. Docker transport is worker-owned,
    not request-selected. No socket, mount or provider key enters the workload.
    """

    def __init__(
        self,
        journal: Path,
        target: DockerTarget,
        transport,
        *,
        current_authority,
        now=lambda: datetime.now(UTC),
        monotonic=time.monotonic,
    ):
        target = DockerTarget.model_validate(target.model_dump())
        self.target, self.transport = target, transport
        self.current_authority, self.now, self.monotonic = current_authority, now, monotonic
        if transport.endpoint_digest != target.endpoint_digest:
            raise DockerExecutionError("Docker endpoint identity mismatch")
        journal = Path(journal)
        if not journal.is_absolute() or not journal.parent.is_dir() or journal.is_symlink():
            raise DockerExecutionError("owned absolute private journal required")
        info = journal.parent.stat()
        if info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise DockerExecutionError("private worker journal directory required")
        fd = os.open(journal, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        if journal.stat().st_mode & 0o077:
            raise DockerExecutionError("private worker journal required")
        self.journal = journal
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS docker_operations ("
                "operation TEXT PRIMARY KEY, package TEXT NOT NULL, container TEXT, "
                "create_intent INTEGER NOT NULL DEFAULT 0, "
                "start_intent INTEGER NOT NULL DEFAULT 0, "
                "stopped INTEGER NOT NULL DEFAULT 0, lease TEXT NOT NULL, outputs TEXT, "
                "last_wall TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS docker_renewals (operation TEXT, sequence INTEGER, "
                "key TEXT NOT NULL, body TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0, "
                "PRIMARY KEY(operation, sequence))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS docker_output_bytes (operation TEXT, path TEXT, "
                "content BLOB NOT NULL, sha256 TEXT NOT NULL, PRIMARY KEY(operation,path))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS docker_heartbeats (operation TEXT, sequence INTEGER, "
                "key TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0, "
                "PRIMARY KEY(operation,sequence))"
            )
        self._anchor_wall, self._anchor_mono = self.now(), self.monotonic()

    def _db(self):
        db = sqlite3.connect(self.journal, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def _package(self, envelope):
        package = envelope.payload
        if not isinstance(package, DockerPackage) or package.envelope(self.target) != envelope:
            raise DockerExecutionError("immutable Docker envelope mismatch")
        DockerPackage.model_validate(package.model_dump())
        return package

    def _clock(self):
        raw = self.now()
        if raw.tzinfo is None or raw < self._anchor_wall:
            raise DockerExecutionError("Docker worker clock rollback")
        return max(
            raw, self._anchor_wall + timedelta(seconds=max(0, self.monotonic() - self._anchor_mono))
        )

    def _lease(self, package, proof):
        lease = DockerLease.model_validate(
            proof.model_dump() if isinstance(proof, DockerLease) else proof
        )
        if (
            lease.id,
            lease.grant_id,
            lease.worker_principal_id,
            lease.epoch,
            lease.hard_expires_at,
        ) != (
            package.lease_id,
            package.grant_id,
            package.worker_principal_id,
            package.epoch,
            package.hard_expires_at,
        ):
            raise DockerExecutionError("lease identity mismatch")
        if lease.expires_at <= self._clock() or lease.hard_expires_at <= self._clock():
            raise DockerExecutionError("Docker authority expired")
        if lease.hard_expires_at > self._clock() + timedelta(seconds=3600):
            raise DockerExecutionError("Docker authority exceeds bounded profile")
        return lease

    def _bind(self, envelope):
        package = self._package(envelope)
        lease = self._lease(package, self.current_authority(envelope))
        encoded = package.model_dump_json()
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM docker_operations WHERE operation=?", (package.operation_id,)
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO docker_operations(operation,package,lease,last_wall) "
                    "VALUES (?,?,?,?)",
                    (
                        package.operation_id,
                        encoded,
                        lease.model_dump_json(),
                        self._clock().isoformat(),
                    ),
                )
            elif (
                row["package"] != encoded
                or row["stopped"]
                or datetime.fromisoformat(row["last_wall"]) > self.now()
            ):
                raise DockerExecutionError("Docker journal authority fenced")
            else:
                db.execute(
                    "UPDATE docker_operations SET last_wall=?,lease=? WHERE operation=?",
                    (self._clock().isoformat(), lease.model_dump_json(), package.operation_id),
                )
        return package, lease

    def _row(self, package):
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM docker_operations WHERE operation=?", (package.operation_id,)
            ).fetchone()
        if row is None or row["package"] != package.model_dump_json():
            raise DockerExecutionError("Docker operation is not journal-owned")
        return row

    def _engine(self, timeout=2):
        info = self.transport.info(timeout_seconds=timeout)
        if (
            not isinstance(info, dict)
            or info.get("ID") != self.target.daemon_id
            or info.get("OSType") != "linux"
            or info.get("Architecture") not in {"x86_64", "amd64"}
        ):
            raise DockerExecutionError("Docker daemon identity changed")
        if not all(
            info.get(key) is True
            for key in ("MemoryLimit", "SwapLimit", "CpuCfsQuota", "PidsLimit")
        ) or not any("seccomp" in value for value in info.get("SecurityOptions", [])):
            raise DockerExecutionError("Docker resource/security enforcement unavailable")

    def _image(self, package, timeout=2):
        image = self.transport.image(package.image, timeout_seconds=timeout)
        if (
            not isinstance(image, dict)
            or package.image not in image.get("RepoDigests", [])
            or image.get("Os") != "linux"
            or image.get("Architecture") != "amd64"
        ):
            raise DockerExecutionError("pinned local Docker image unavailable")
        config = image.get("Config", {})
        if config.get("Volumes") or config.get("OnBuild") or config.get("Healthcheck"):
            raise DockerExecutionError("implicit image volume/healthcheck effects unsupported")
        # Image configuration is immutable approved material, not ambient runtime
        # authority. Runtime credential/proxy/socket environment is never injected.
        if any(
            re.search(r"TOKEN|SECRET|PASSWORD|PROXY|DOCKER_HOST", value.split("=", 1)[0], re.I)
            for value in config.get("Env", [])
        ):
            raise DockerExecutionError("credential-bearing image environment unsupported")

    def _inspect(self, package, timeout=2):
        self._engine(timeout)
        row = self._row(package)
        item = self.transport.inspect(row["container"] or package.name, timeout_seconds=timeout)
        if item is None:
            return None
        identity = item.get("Id")
        expected = package.container_config()
        if (
            not isinstance(identity, str)
            or not re.fullmatch(r"[a-f0-9]{64}", identity)
            or row["container"] not in {None, identity}
        ):
            raise DockerExecutionError("Docker container identity changed")
        if item.get("Name") != "/" + package.name:
            raise DockerExecutionError("Docker container name changed")
        config, host = item.get("Config", {}), item.get("HostConfig", {})
        for key in ("Image", "User", "WorkingDir", "Entrypoint", "Cmd", "Labels", "Tty"):
            if config.get(key) != expected[key]:
                raise DockerExecutionError("Docker container config changed")
        for key, value in expected["HostConfig"].items():
            if key != "LogConfig" and host.get(key) != value:
                raise DockerExecutionError("Docker container security/resource config changed")
        if (
            item.get("Mounts", [])
            or config.get("Volumes")
            or any(host.get(key) for key in ("VolumesFrom", "DeviceRequests", "ExtraHosts"))
        ):
            raise DockerExecutionError("unexpected Docker mount/device/network capability")
        with self._db() as db:
            db.execute(
                "UPDATE docker_operations SET container=? WHERE operation=? AND container IS NULL",
                (identity, package.operation_id),
            )
        return item

    def capabilities(self, target):
        if target != self.target.binding:
            raise DockerExecutionError("Docker target mismatch")
        return ExecutionCapabilities(
            True,
            True,
            True,
            True,
            True,
            True,
            True,
            (
                "No input mounts, registry pulls, shells or agent sockets",
                "No scientific-validity or provider cost inference",
                "Source adapter requires installed guard and supervised worker wiring",
            ),
        )

    def preflight(self, request):
        try:
            self._bind(request.envelope)
            self._engine()
            self._image(self._package(request.envelope))
            return PreflightResult(request.target, self.capabilities(request.target), True)
        except Exception:
            return PreflightResult(
                self.target.binding,
                self.capabilities(self.target.binding),
                False,
                "docker_preflight_denied",
            )

    def _observation(self, package, item):
        state, terminated = "unknown", False
        if item is not None:
            current = item.get("State", {})
            if current.get("Running") is True and current.get("Status") == "running":
                state = "running"
            elif current.get("Status") == "created" and current.get("Running") is False:
                state = "pending"
            elif (
                current.get("Status") == "exited"
                and current.get("Running") is False
                and type(current.get("ExitCode")) is int
            ):
                state, terminated = ("succeeded" if current["ExitCode"] == 0 else "failed"), True
        return Observation(
            OperationHandle(
                self.target.binding, package.operation_id, None if item is None else item["Id"]
            ),
            state,
            self.now(),
            terminated,
        )

    def submit(self, envelope, *, timeout_seconds=None):
        package, _ = self._bind(envelope)
        timeout = min(2, timeout_seconds or 2)
        try:
            item = self._inspect(package, timeout)
            row = self._row(package)
            if item is None and not row["create_intent"]:
                self._image(package, timeout)
                with self._db() as db:
                    changed = db.execute(
                        "UPDATE docker_operations SET create_intent=1 "
                        "WHERE operation=? AND create_intent=0 AND stopped=0",
                        (package.operation_id,),
                    ).rowcount
                if not changed:
                    return self._observation(package, None)
                self._bind(envelope)
                self.transport.create(
                    package.name, package.container_config(), timeout_seconds=timeout
                )
                item = self._inspect(package, timeout)
            if item is not None and item.get("State", {}).get("Status") == "created":
                with self._db() as db:
                    changed = db.execute(
                        "UPDATE docker_operations SET start_intent=1 "
                        "WHERE operation=? AND start_intent=0 AND stopped=0",
                        (package.operation_id,),
                    ).rowcount
                if changed:
                    self._bind(envelope)
                    self.transport.start(item["Id"], timeout_seconds=timeout)
                    item = self._inspect(package, timeout)
            try:
                self._bind(envelope)
            except Exception:
                self.cancel(envelope)
                return self.observe(envelope)
            return self._observation(package, item)
        except Exception:
            # Durable create/start intent forbids resubmission on ambiguous replies.
            return self._observation(package, None)

    def observe(self, envelope, *, timeout_seconds=None):
        package = self._package(envelope)
        try:
            self._row(package)
            return self._observation(package, self._inspect(package, timeout_seconds or 2))
        except Exception:
            return self._observation(package, None)

    reconcile = observe

    def cancel(self, envelope, *, timeout_seconds=None):
        package = self._package(envelope)
        self._row(package)
        with self._db() as db:
            db.execute(
                "UPDATE docker_operations SET stopped=1 WHERE operation=?", (package.operation_id,)
            )
        try:
            item = self._inspect(package, timeout_seconds or 2)
            if item is not None and item.get("State", {}).get("Running") is True:
                self.transport.signal(item["Id"], "SIGKILL", timeout_seconds=timeout_seconds or 2)
            observation = self.observe(envelope, timeout_seconds=timeout_seconds)
        except Exception:
            observation = self._observation(package, None)
        return CancellationObservation(
            observation.handle,
            True,
            observation.termination_confirmed,
            not observation.termination_confirmed,
        )

    def renew(self, envelope, channel):
        package, _ = self._bind(envelope)
        row = self._row(package)
        cached = self._lease(package, DockerLease.model_validate_json(row["lease"]))
        if row["stopped"]:
            raise DockerExecutionError("Docker stop fence is permanent")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            pending = db.execute(
                "SELECT * FROM docker_renewals WHERE operation=? AND completed=0 "
                "ORDER BY sequence LIMIT 1",
                (package.operation_id,),
            ).fetchone()
            if pending is None:
                sequence = db.execute(
                    "SELECT coalesce(max(sequence),0)+1 FROM docker_renewals WHERE operation=?",
                    (package.operation_id,),
                ).fetchone()[0]
                body = {"lease_id": package.lease_id, "expected_epoch": package.epoch}
                key = "ordinary-docker-renew-" + digest([package.operation_id, sequence, body])
                db.execute(
                    "INSERT INTO docker_renewals VALUES (?,?,?,?,0)",
                    (package.operation_id, sequence, key, json.dumps(body)),
                )
            else:
                sequence, key, body = (
                    pending["sequence"],
                    pending["key"],
                    json.loads(pending["body"]),
                )
        proof = channel.post("/worker/leases/renew", body, key)
        # No exception text or new key on unknown response. Exact pending intent stays.
        proof = DockerLease.model_validate_json(
            json.dumps({key: proof[key] for key in DockerLease.model_fields})
        )
        updated = self._lease(package, proof)
        if updated.expires_at < cached.expires_at or updated.expires_at > self._clock() + timedelta(
            seconds=30
        ):
            raise DockerExecutionError("renewal exceeded bounded authority")
        self._bind(envelope)
        with self._db() as db:
            row = db.execute(
                "SELECT stopped FROM docker_operations WHERE operation=?", (package.operation_id,)
            ).fetchone()
            if row["stopped"]:
                raise DockerExecutionError("renewal raced with stop")
            db.execute(
                "UPDATE docker_operations SET lease=? WHERE operation=?",
                (updated.model_dump_json(), package.operation_id),
            )
            db.execute(
                "UPDATE docker_renewals SET completed=1 WHERE operation=? AND sequence=?",
                (package.operation_id, sequence),
            )
        return updated

    def heartbeat(self, envelope, channel):
        package = self._package(envelope)
        self._row(package)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM docker_heartbeats WHERE operation=? AND completed=0 "
                "ORDER BY sequence LIMIT 1",
                (package.operation_id,),
            ).fetchone()
            if row is None:
                sequence = db.execute(
                    "SELECT coalesce(max(sequence),0)+1 FROM docker_heartbeats WHERE operation=?",
                    (package.operation_id,),
                ).fetchone()[0]
                key = "ordinary-docker-heartbeat-" + digest([package.operation_id, sequence])
                db.execute(
                    "INSERT INTO docker_heartbeats VALUES (?,?,?,0)",
                    (package.operation_id, sequence, key),
                )
            else:
                sequence, key = row["sequence"], row["key"]
        result = channel.post("/worker/heartbeat", {}, key)
        at = datetime.fromisoformat(result["heartbeat_at"])
        if (
            at.tzinfo is None
            or at < self._clock() - timedelta(seconds=30)
            or at > self._clock() + timedelta(seconds=1)
        ):
            raise DockerExecutionError("worker heartbeat freshness is unknown")
        with self._db() as db:
            db.execute(
                "UPDATE docker_heartbeats SET completed=1 WHERE operation=? AND sequence=?",
                (package.operation_id, sequence),
            )

    def collect_outputs(self, envelope):
        package, _ = self._bind(envelope)
        row = self._row(package)
        if row["outputs"] is not None:
            return self._finish_capture(envelope)
        item = self._inspect(package)
        if item is None or item.get("State", {}).get("Running") is not True:
            raise DockerExecutionError("guard-held output required")
        marker = archive_files(
            self.transport.archive(item["Id"], "/scratch/tessera-result.json", limit=16384),
            max_bytes=1024,
            max_files=1,
        )
        try:
            report = json.loads(marker["tessera-result.json"])
        except (ValueError, KeyError):
            raise DockerExecutionError("guard output is not ready") from None
        if report != {"version": 1, "operation_id": package.operation_id, "exit_code": 0}:
            raise DockerExecutionError("workload did not complete successfully")
        data = self.transport.archive(
            item["Id"], "/outputs", limit=package.max_output_bytes + 1024**2
        )
        files = archive_files(data, max_bytes=package.max_output_bytes)
        expected = {"outputs/" + path for path in package.output_paths}
        if set(files) != expected:
            raise DockerExecutionError("declared Docker output set mismatch")
        self._bind(envelope)
        outputs = tuple(
            CollectedOutput(
                path, files["outputs/" + path], hashlib.sha256(files["outputs/" + path]).hexdigest()
            )
            for path in package.output_paths
        )
        # Snapshot retained by caller only after real container termination. No
        # directory extraction, symlink traversal, implicit manifest or fake Job UID.
        manifest = {
            "provider": "docker",
            "container_id": item["Id"],
            "operation_id": package.operation_id,
            "admitted_attempt_id": package.admitted_attempt_id,
            "package_digest": package.package_digest,
            "target_digest": package.target_digest,
            "lease_id": package.lease_id,
            "epoch": package.epoch,
            "agent_principal_id": package.agent_principal_id,
            "worker_principal_id": package.worker_principal_id,
            "grant_id": package.grant_id,
            "outputs": [
                {"path": o.relative_path, "sha256": o.sha256, "size": len(o.content)}
                for o in outputs
            ],
        }
        with self._db() as db:
            changed = db.execute(
                "UPDATE docker_operations SET outputs=? "
                "WHERE operation=? AND stopped=0 AND outputs IS NULL",
                (json.dumps(manifest, sort_keys=True), package.operation_id),
            ).rowcount
            if not changed:
                raise DockerExecutionError("output capture raced with stop/capture")
            for output in outputs:
                db.execute(
                    "INSERT INTO docker_output_bytes VALUES (?,?,?,?)",
                    (package.operation_id, output.relative_path, output.content, output.sha256),
                )
        return self._finish_capture(envelope)

    def _finish_capture(self, envelope):
        package = self._package(envelope)
        item = self._inspect(package)
        if item is not None and item.get("State", {}).get("Running") is True:
            self._bind(envelope)
            self.transport.signal(item["Id"], "SIGTERM", timeout_seconds=2)
        observed = self.observe(envelope)
        for _ in range(40):
            if observed.termination_confirmed:
                break
            self._bind(envelope)
            time.sleep(0.05)
            observed = self.observe(envelope)
        if observed.state != "succeeded" or not observed.termination_confirmed:
            raise DockerExecutionError("container output capture awaiting termination")
        with self._db() as db:
            rows = db.execute(
                "SELECT path,content,sha256 FROM docker_output_bytes "
                "WHERE operation=? ORDER BY path",
                (package.operation_id,),
            ).fetchall()
        outputs = tuple(CollectedOutput(row["path"], row["content"], row["sha256"]) for row in rows)
        if (
            {o.relative_path for o in outputs} != set(package.output_paths)
            or sum(len(o.content) for o in outputs) > package.max_output_bytes
            or any(hashlib.sha256(o.content).hexdigest() != o.sha256 for o in outputs)
        ):
            raise DockerExecutionError("retained Docker output custody changed")
        return outputs

    def output_manifest(self, envelope):
        package = self._package(envelope)
        self._finish_capture(envelope)
        return json.loads(self._row(package)["outputs"])

    def ready_for_collection(self, envelope):
        package, _ = self._bind(envelope)
        item = self._inspect(package)
        if item is None or item.get("State", {}).get("Running") is not True:
            return False
        data = self.transport.archive(item["Id"], "/scratch/tessera-result.json", limit=16384)
        if data is None:
            return False
        files = archive_files(data, max_bytes=1024, max_files=1)
        return json.loads(files["tessera-result.json"]) == {
            "version": 1,
            "operation_id": package.operation_id,
            "exit_code": 0,
        }

    def collect(self, envelope):
        package = self._package(envelope)
        row = self._row(package)
        if row["outputs"] is None or self.observe(envelope).state != "succeeded":
            raise DockerExecutionError("confirmed Docker output custody required")
        return CollectionHandoff(self.observe(envelope).handle, package.output_paths)

    def account(self, envelope):
        return AccountingObservation(self.observe(envelope).handle, "unknown")

    def stop_status(self, envelope):
        package = self._package(envelope)
        row = self._row(package)
        observation = self.observe(envelope)
        requested = bool(row["stopped"])
        return CancellationObservation(
            observation.handle,
            requested,
            observation.termination_confirmed,
            requested and not observation.termination_confirmed,
        )

    def exit_code(self, envelope):
        item = self._inspect(self._package(envelope))
        if (
            item is not None
            and self._observation(self._package(envelope), item).termination_confirmed
        ):
            code = item.get("State", {}).get("ExitCode")
            if type(code) is int and 0 <= code <= 255:
                return code
        return None

    def watch(self, envelope, channel, *, sleep=time.sleep, max_cycles=None):
        self._package(envelope)
        try:
            self._bind(envelope)
            self.heartbeat(envelope, channel)
            observation = self.submit(envelope)
        except Exception:
            # Losing fresh authority on restart never erases journal ownership or
            # prevents a local exact-ID stop of the existing container. A fresh
            # denied package has no journal and cannot touch an unrelated object.
            try:
                self.cancel(envelope)
            except DockerExecutionError:
                pass
            return self.observe(envelope)
        next_renewal, cycles = self._clock() + timedelta(seconds=10), 0
        next_heartbeat = self._clock() + timedelta(seconds=30)
        while observation.state not in {"succeeded", "failed"}:
            try:
                self._bind(envelope)
                if self._clock() >= next_heartbeat:
                    self.heartbeat(envelope, channel)
                    next_heartbeat = self._clock() + timedelta(seconds=30)
                if self._clock() >= next_renewal:
                    self.renew(envelope, channel)
                    next_renewal = self._clock() + timedelta(seconds=10)
                if self.ready_for_collection(envelope):
                    self.collect_outputs(envelope)
                    observation = self.observe(envelope)
                    if observation.termination_confirmed:
                        return observation
            except Exception:
                self.cancel(envelope)
                return self.observe(envelope)
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                return observation  # isolated test bound only, never service default
            sleep(0.25)
            observation = self.observe(envelope)
        return observation
