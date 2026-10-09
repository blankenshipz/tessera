"""Durable, immutable output custody separate from execution supervision."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from .artifacts import upload_output_set
from .outputs import MAX_OUTPUT_BYTES, CollectedOutput, validate_output_paths


class KubernetesOutputDelivery:
    def __init__(self, root: Path):
        self.path = root / "output-delivery.sqlite3"
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS deliveries "
                "(operation TEXT PRIMARY KEY, identity TEXT NOT NULL, outputs TEXT, receipt TEXT)"
            )
        self.path.chmod(0o600)

    def connect(self):
        return sqlite3.connect(self.path)

    @staticmethod
    def identity(package):
        return json.dumps(
            {
                "attempt": package.binding.attempt_id,
                "lease": package.binding.lease_id,
                "epoch": package.binding.epoch,
                "package": package.package_digest,
                "pvc": package.output.__dict__,
            },
            sort_keys=True,
        )

    def pending(self, package):
        with self.connect() as db:
            row = db.execute(
                "SELECT identity FROM deliveries WHERE operation=?", (package.operation_id,)
            ).fetchone()
        if row and row[0] != self.identity(package):
            raise ValueError("Output delivery provenance changed")
        return row is not None

    def begin(self, package):
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO deliveries VALUES (?, ?, NULL, NULL)",
                (package.operation_id, self.identity(package)),
            )
        self.pending(package)

    def deliver(self, package, adapter, channel):
        identity = self.identity(package)
        self.begin(package)
        with self.connect() as db:
            row = db.execute(
                "SELECT identity,outputs,receipt FROM deliveries WHERE operation=?",
                (package.operation_id,),
            ).fetchone()
        if row[0] != identity:
            raise ValueError("Output delivery provenance changed")
        if row[2] is not None:
            return json.loads(row[2])
        if row[1] is None:
            outputs = adapter.collect_outputs(package)
            paths = validate_output_paths([item.relative_path for item in outputs])
            if (
                set(paths) != set(package.package["output_paths"])
                or sum(len(item.content) for item in outputs) > MAX_OUTPUT_BYTES
            ):
                raise ValueError("Output delivery declaration mismatch")
            for item in outputs:
                if hashlib.sha256(item.content).hexdigest() != item.sha256:
                    raise ValueError("Output delivery checksum mismatch")
            encoded = json.dumps(
                [[item.relative_path, item.content.hex(), item.sha256] for item in outputs]
            )
            with self.connect() as db:
                db.execute(
                    "UPDATE deliveries SET outputs=? WHERE operation=? AND outputs IS NULL",
                    (encoded, package.operation_id),
                )
                encoded = db.execute(
                    "SELECT outputs FROM deliveries WHERE operation=?", (package.operation_id,)
                ).fetchone()[0]
        else:
            encoded = row[1]
        outputs = tuple(
            CollectedOutput(path, bytes.fromhex(content), digest)
            for path, content, digest in json.loads(encoded)
        )
        if hasattr(adapter, "release_collector"):
            adapter.release_collector(package)
        receipt = upload_output_set(
            channel,
            lease_id=package.binding.lease_id,
            expected_epoch=package.binding.epoch,
            operation_id=package.operation_id,
            outputs=outputs,
        )
        with self.connect() as db:
            db.execute(
                "UPDATE deliveries SET receipt=? WHERE operation=?",
                (json.dumps(receipt, sort_keys=True), package.operation_id),
            )
        return receipt
