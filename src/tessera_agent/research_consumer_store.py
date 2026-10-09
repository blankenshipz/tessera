"""Private durable research stages; no prompt, tool-body or provider-state storage."""

import hashlib
import json
from pathlib import Path

from .remote_reasoning_store import ReasoningStore


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class ConsumerBlocked(ValueError):
    """Closed diagnostic codes only; never upstream response or model content."""


class ResearchConsumerStore:
    """Reuse private-directory checks, keep immutable identities across restart.

    Only digests and opaque effect/result references belong here. Generated public
    bytes belong in the separately human-admitted central registry, not this DB.
    """

    def __init__(self, directory: Path):
        self.runtime = ReasoningStore(directory)
        self.db = self.runtime.db
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS research_consumers (
            run_id TEXT PRIMARY KEY, binding TEXT NOT NULL, stage TEXT NOT NULL,
            output_id TEXT, source_id TEXT, source_sequence INTEGER, output_digest TEXT);
          CREATE TABLE IF NOT EXISTS research_effects (
            run_id TEXT NOT NULL, boundary TEXT NOT NULL, identity TEXT NOT NULL,
            payload_digest TEXT NOT NULL, state TEXT NOT NULL, result_id TEXT,
            PRIMARY KEY(run_id,boundary));
          CREATE TABLE IF NOT EXISTS research_journeys (
            id TEXT PRIMARY KEY, binding TEXT NOT NULL, stage TEXT NOT NULL,
            sequence INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS research_journey_refs (
            journey_id TEXT NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL,
            PRIMARY KEY(journey_id,name));
        """)

    def close(self):
        self.runtime.close()

    def bind_journey(self, journey_id, binding):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO research_journeys(id,binding,stage) VALUES(?,?,?)",
                (journey_id, canonical(binding), "planner"),
            )
            if self.journey(journey_id)["binding"] != canonical(binding):
                raise ConsumerBlocked("journey_binding_changed")

    def journey(self, journey_id):
        row = self.db.execute(
            "SELECT * FROM research_journeys WHERE id=?", (journey_id,)
        ).fetchone()
        if row is None:
            raise ConsumerBlocked("journey_not_bound")
        return dict(row)

    def journey_stage(self, journey_id, stage):
        with self.db:
            self.db.execute("UPDATE research_journeys SET stage=? WHERE id=?", (stage, journey_id))

    def reference(self, journey_id, name, value=None):
        with self.db:
            if value is not None:
                encoded = canonical(value)
                self.db.execute(
                    "INSERT OR IGNORE INTO research_journey_refs VALUES(?,?,?)",
                    (journey_id, name, encoded),
                )
            row = self.db.execute(
                "SELECT value FROM research_journey_refs WHERE journey_id=? AND name=?",
                (journey_id, name),
            ).fetchone()
            if value is not None and row[0] != encoded:
                raise ConsumerBlocked("journey_reference_changed")
            return json.loads(row[0]) if row else None

    def next_poll(self, journey_id):
        with self.db:
            self.db.execute(
                "UPDATE research_journeys SET sequence=sequence+1 WHERE id=?", (journey_id,)
            )
            return self.journey(journey_id)["sequence"]

    def bind(self, run_id, binding):
        encoded = canonical(binding)
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO research_consumers(run_id,binding,stage) VALUES(?,?,?)",
                (run_id, encoded, "reasoning"),
            )
            row = self.get(run_id)
            if row["binding"] != encoded:
                raise ConsumerBlocked("consumer_binding_changed")
        return row

    def get(self, run_id):
        row = self.db.execute(
            "SELECT * FROM research_consumers WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise ConsumerBlocked("consumer_not_bound")
        return dict(row)

    def stage(self, run_id, stage):
        with self.db:
            self.db.execute("UPDATE research_consumers SET stage=? WHERE run_id=?", (stage, run_id))

    def begin(self, run_id, boundary, payload_digest):
        identity = "research-" + hashlib.sha256((run_id + "\n" + boundary).encode()).hexdigest()
        with self.db:
            existing = self.db.execute(
                "SELECT * FROM research_effects WHERE run_id=? AND boundary=?",
                (run_id, boundary),
            ).fetchone()
            if existing:
                if existing["payload_digest"] != payload_digest:
                    raise ConsumerBlocked("consumer_effect_changed")
                return dict(existing), False
            self.db.execute(
                "INSERT INTO research_effects VALUES(?,?,?,?,?,NULL)",
                (run_id, boundary, identity, payload_digest, "unknown"),
            )
        return {"identity": identity, "state": "unknown", "result_id": None}, True

    def acknowledged(self, run_id, boundary, result_id):
        with self.db:
            self.db.execute(
                "UPDATE research_effects SET state='acknowledged',result_id=? "
                "WHERE run_id=? AND boundary=? AND state='unknown'",
                (result_id, run_id, boundary),
            )

    def effect_result(self, run_id, boundary):
        row = self.db.execute(
            "SELECT result_id FROM research_effects WHERE run_id=? AND boundary=? "
            "AND state='acknowledged'",
            (run_id, boundary),
        ).fetchone()
        return row[0] if row else None

    def registered(self, run_id, output_id, source_id, sequence, digest):
        with self.db:
            row = self.get(run_id)
            pins = (output_id, source_id, sequence, digest)
            old = tuple(
                row[k] for k in ("output_id", "source_id", "source_sequence", "output_digest")
            )
            if row["output_id"] and old != pins:
                raise ConsumerBlocked("consumer_output_changed")
            self.db.execute(
                "UPDATE research_consumers SET output_id=?,source_id=?,source_sequence=?,"
                "output_digest=?,stage='awaiting_output_disclosure' WHERE run_id=?",
                (*pins, run_id),
            )
