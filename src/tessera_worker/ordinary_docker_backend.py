"""Durable binding of the service protocol to real native and artifact HTTP APIs."""

import json
import os
import sqlite3
import stat
from datetime import datetime

from .artifacts import upload_output_set
from .control_channel import ControlChannelError
from .ordinary_docker_contract import DockerLease, DockerPackage, digest
from .ordinary_docker_http import DockerFence, DockerWireReport
from .ordinary_docker_messages import DockerClaim, DockerControlReadback, DockerDeliveryAck


class HttpDockerWorkerBackend:
    def __init__(self, client, journal, worker_id):
        self.client, self.journal, self.worker_id = client, journal, worker_id
        fd = os.open(journal, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ControlChannelError("private backend journal required")
        finally:
            os.close(fd)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS claims(key TEXT PRIMARY KEY,package TEXT)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS deliveries(key TEXT PRIMARY KEY,operation TEXT,"
                "local_digest TEXT,wire TEXT)"
            )

    def _db(self):
        return sqlite3.connect(self.journal)

    def heartbeat(self, worker_id, key, *, timeout_seconds):
        if worker_id != self.worker_id:
            raise ControlChannelError("worker identity mismatch")
        result = self.client.channel.post(
            "/worker/heartbeat", {}, key, timeout_seconds=timeout_seconds
        )
        if result.get("worker_principal_id") != worker_id:
            raise ControlChannelError("worker heartbeat identity mismatch")
        return datetime.fromisoformat(result["heartbeat_at"].replace("Z", "+00:00"))

    def claim(self, worker_id, target, key, *, timeout_seconds):
        if worker_id != self.worker_id:
            raise ControlChannelError("worker identity mismatch")
        with self._db() as db:
            row = db.execute("SELECT package FROM claims WHERE key=?", (key,)).fetchone()
        if row is None:
            pending = self.client.poll(target.binding.identity_digest).dispatches
            package = None if not pending else pending[0].package
            if package is not None:
                package.envelope(target)
                if package.worker_principal_id != worker_id:
                    raise ControlChannelError("worker claim identity mismatch")
            with self._db() as db:
                db.execute(
                    "INSERT INTO claims VALUES (?,?)",
                    (key, None if package is None else package.model_dump_json()),
                )
        else:
            package = None if row[0] is None else DockerPackage.model_validate_json(row[0])
        if package is None:
            return None
        receipt = self.client.claim(package, key)
        return DockerClaim(dispatch_id=package.operation_id, package=package, lease=receipt.lease)

    def current_authority(self, claim, *, timeout_seconds):
        return self.client.current_authority(claim.package)

    def renew(self, claim, key, *, timeout_seconds):
        result = self.client.channel.post(
            "/worker/leases/renew",
            {"lease_id": claim.package.lease_id, "expected_epoch": claim.package.epoch},
            key,
            timeout_seconds=timeout_seconds,
        )
        # Renewal's ordinary lease includes metadata beyond the six native pins.
        lease = DockerLease.model_validate_json(
            json.dumps({k: result[k] for k in DockerLease.model_fields})
        )
        self.client._lease(claim.package, lease)
        return lease

    @staticmethod
    def _wire(claim, value):
        value.require_claim(claim)
        state = value.state
        if value.stop_requested and value.termination_confirmed:
            state = "cancelled"
        elif state == "pending":
            state = "unknown"
        return DockerWireReport(
            **DockerFence.from_package(claim.package).model_dump(),
            container_id=value.container_id,
            state=state,
            termination_confirmed=value.termination_confirmed,
            stop_requested=value.stop_requested,
            exit_code=value.exit_code,
        )

    def report(self, claim, value, key, *, timeout_seconds):
        wire = self._wire(claim, value)
        local_digest = digest(value.model_dump(mode="json"))
        with self._db() as db:
            previous = db.execute(
                "SELECT wire,local_digest FROM deliveries WHERE key=?", (key,)
            ).fetchone()
            if previous is not None and previous != (wire.model_dump_json(), local_digest):
                raise ControlChannelError("delivery key changed")
            db.execute(
                "INSERT OR IGNORE INTO deliveries VALUES (?,?,?,?)",
                (key, claim.package.operation_id, local_digest, wire.model_dump_json()),
            )
        # The ACK below translates an actual exact echo, never a synthetic reply.
        self.client.report(wire, key)
        return DockerDeliveryAck(
            operation_id=claim.package.operation_id, key=key, payload_digest=local_digest
        )

    cancel = report

    def read(self, claim, *, timeout_seconds):
        result = self.client.readback(claim.package)
        acknowledgements = []
        with self._db() as db:
            rows = db.execute(
                "SELECT key,local_digest,wire FROM deliveries WHERE operation=? LIMIT 128",
                (claim.package.operation_id,),
            ).fetchall()
        for key, local_digest, wire in rows:
            reply = self.client.readback(claim.package, delivery_key=key)
            if reply.delivery is not None:
                expected = DockerWireReport.model_validate_json(wire)
                observed = reply.delivery.response.model_dump(
                    exclude={"scientific_validity", "human_resolution_required"}
                )
                if expected.model_dump() != observed:
                    raise ControlChannelError("delivery readback changed")
                acknowledgements.append(
                    DockerDeliveryAck(
                        operation_id=claim.package.operation_id,
                        key=key,
                        payload_digest=local_digest,
                    )
                )
        return DockerControlReadback(
            operation_id=claim.package.operation_id,
            container_id=result.container_id,
            desired="run" if result.effective_action == "run" else "cancel_requested",
            acknowledgements=tuple(acknowledgements),
        )

    def output(self, claim, value, outputs, key, *, timeout_seconds):
        receipt = upload_output_set(
            self.client.channel,
            lease_id=claim.package.lease_id,
            expected_epoch=claim.package.epoch,
            operation_id=claim.package.operation_id,
            outputs=outputs,
        )
        for field in ("task_id", "grant_id", "worker_principal_id"):
            if receipt[field] != getattr(claim.package, field):
                raise ControlChannelError("output custody attribution mismatch")
        return DockerDeliveryAck(
            operation_id=claim.package.operation_id,
            key=key,
            payload_digest=digest(value.model_dump(mode="json")),
            manifest_id=receipt["id"],
            custody="quarantined",
        )
