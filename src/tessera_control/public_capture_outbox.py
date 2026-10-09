"""Private source-metadata journal; independent of the control database/migrations."""

import os
import sqlite3
import stat
from pathlib import Path

from .public_capture import canonical


class PublicCaptureOutbox:
    """One admitted run per private journal. Delivery is at-least-once, never authority.

    No tokens/URLs/raw stream/callback exceptions are retained. A crash after send
    leaves unknown delivery; retry uses the identical envelope/key. Callers supply
    an authenticated append client, not owner proxy credentials.
    """

    def __init__(self, directory, adapter, *, capacity=10000):
        if type(capacity) is not int or not 1 <= capacity <= 10000:
            raise ValueError("Invalid bounded outbox capacity.")
        self.adapter, self.capacity = adapter, capacity
        directory = Path(directory)
        if not directory.exists():
            directory.mkdir(mode=0o700)
        self._private(directory, directory=True)
        path = directory / "public-capture.sqlite3"
        if path.exists() or path.is_symlink():
            self._private(path)
        else:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        # Refuse unsafe pre-existing SQLite sidecars, including hot rollback journals.
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                self._private(sidecar)
        self.connection = sqlite3.connect(path, timeout=2)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA journal_mode=DELETE")
            self.connection.execute("PRAGMA synchronous=FULL")
            with self.connection:
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS capture_meta "
                    "(id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL, "
                    "binding TEXT NOT NULL)"
                )
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS capture_events "
                    "(record_id TEXT PRIMARY KEY, source_id TEXT NOT NULL UNIQUE, "
                    "source_sequence INTEGER NOT NULL UNIQUE, watermark INTEGER NOT NULL, "
                    "envelope TEXT NOT NULL, delivery TEXT NOT NULL DEFAULT 'pending', "
                    "attempts INTEGER NOT NULL DEFAULT 0, collector_sequence INTEGER, "
                    "has_gaps INTEGER NOT NULL, capture_unknown INTEGER NOT NULL)"
                )
                row = self.connection.execute("SELECT * FROM capture_meta WHERE id=1").fetchone()
                binding = canonical(adapter.binding)
                if row and (row["version"] != 1 or row["binding"] != binding):
                    raise ValueError(
                        "Outbox cannot replace its version/enrollment/run admission pins."
                    )
                self.connection.execute(
                    "INSERT OR IGNORE INTO capture_meta VALUES(1,1,?)", (binding,)
                )
        except Exception:
            self.connection.close()
            raise

    @staticmethod
    def _private(path, *, directory=False):
        info = path.lstat()
        valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if not valid_type or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError(
                "Outbox requires an owned private directory and regular private files."
            )

    def close(self):
        self.connection.close()

    def capture(self, observation):
        """Validate BEFORE persistence. Unsupported/private events create no sequence or gap."""
        envelope = self.adapter.envelope(observation)
        record = envelope["record"]
        metadata = record["harness"]
        wire = canonical(envelope)
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            previous = self.connection.execute(
                "SELECT * FROM capture_events WHERE record_id=? OR source_id=? "
                "OR source_sequence=?",
                (record["record_id"], metadata["source_event_id"], metadata["source_sequence"]),
            ).fetchall()
            if previous:
                if len(previous) != 1 or previous[0]["envelope"] != wire:
                    raise ValueError(
                        "Source identity/sequence replay differs from its immutable envelope."
                    )
                return record["record_id"]
            count = self.connection.execute("SELECT COUNT(*) FROM capture_events").fetchone()[0]
            if count >= self.capacity:
                raise ValueError("Outbox capacity reached; source capture remains incomplete.")
            self.connection.execute(
                "INSERT INTO capture_events(record_id,source_id,source_sequence,watermark,envelope,"
                "has_gaps,capture_unknown) VALUES(?,?,?,?,?,?,?)",
                (
                    record["record_id"],
                    metadata["source_event_id"],
                    metadata["source_sequence"],
                    metadata["source_watermark"],
                    wire,
                    bool(metadata["gaps"]),
                    metadata["capture_state"] == "unknown",
                ),
            )
        return record["record_id"]

    @staticmethod
    def _ack(result, rid):
        """Normalize only the existing validated client DeliveryResult shape, not raw HTTP ACKs."""
        if set(vars(result)) != {"status", "code", "record_id", "status_code", "sequence"}:
            return "unknown", None
        if result.record_id != rid:
            return "unknown", None
        if (
            result.status == "accepted"
            and result.code == "collector_acknowledged"
            and type(result.status_code) is int
            and result.status_code in {200, 201}
            and type(result.sequence) is int
            and 1 <= result.sequence <= 2**63 - 1
        ):
            return "accepted", result.sequence
        if (
            result.status == "rejected"
            and result.code == "collector_rejected"
            and type(result.status_code) is int
            and 300 <= result.status_code < 500
            and result.sequence is None
        ):
            return "send_failed", None
        return "unknown", None

    def deliver_next(self, client):
        """One bounded attempt. No retries/sleeps, no raw response/exception persistence.

        Reuses the existing ProvenanceClient.append signature unchanged. Research
        output references have no Kubernetes manifest or output proof. The collector
        resolves the distinct registered research family, never worker artifacts.
        """
        import hashlib
        import json

        row = self.connection.execute(
            "SELECT * FROM capture_events WHERE delivery!='accepted' "
            "ORDER BY source_sequence LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        rid = row["record_id"]
        envelope = json.loads(row["envelope"])
        key = "provenance-" + hashlib.sha256(rid.encode()).hexdigest()
        # Commit uncertainty before crossing external boundary, even if process dies.
        with self.connection:
            self.connection.execute(
                "UPDATE capture_events SET delivery='unknown',attempts=attempts+1 "
                "WHERE record_id=?",
                (rid,),
            )
        try:
            self.adapter._disclosure()
            if envelope["output_proof"] is not None:
                raise ValueError("This source adapter cannot strip a worker output proof.")
            result = client.append(
                self.adapter.binding["project_id"],
                envelope["record"],
                grant_proof=envelope["grant_proof"],
                idempotency_key=key,
            )
            status, sequence = self._ack(result, rid)
        except Exception:
            status, sequence = "unknown", None
        with self.connection:
            # Concurrent/crash redelivery cannot downgrade a prior accepted result.
            self.connection.execute(
                "UPDATE capture_events SET delivery=?,collector_sequence=? "
                "WHERE record_id=? AND delivery!='accepted'",
                (status, sequence, rid),
            )
        return {"record_id": rid, "delivery": status, "collector_sequence": sequence}

    def watermarks(self):
        """Separate captured/public-source and acknowledged-collector evidence."""
        rows = self.connection.execute(
            "SELECT source_sequence,watermark,delivery,has_gaps,capture_unknown "
            "FROM capture_events ORDER BY source_sequence"
        ).fetchall()
        reported = max((row["watermark"] for row in rows), default=0)
        prefix = 0
        for row in rows:
            if row["source_sequence"] != prefix + 1 or row["delivery"] != "accepted":
                break
            prefix += 1
        unknown = sum(row["delivery"] == "unknown" for row in rows)
        failed = sum(row["delivery"] == "send_failed" for row in rows)
        return {
            "reported_source_watermark": reported,
            "captured_events": len(rows),
            "acknowledged_source_prefix": prefix,
            "acknowledged_events": sum(row["delivery"] == "accepted" for row in rows),
            "pending_events": sum(row["delivery"] == "pending" for row in rows),
            "unknown_delivery_events": unknown,
            "send_failed_events": failed,
            "missing_source_events": max(0, reported - len(rows)),
            "reported_gap_events": sum(row["has_gaps"] for row in rows),
            "unknown_capture_events": sum(row["capture_unknown"] for row in rows),
            "unseen_sources_and_private_parts": "not_measured",
            "raw_stream": "not_retained",
            "authority_effect": "none",
        }
