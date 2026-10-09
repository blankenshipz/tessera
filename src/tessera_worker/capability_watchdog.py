"""Durable external-watchdog protocol core, without a daemon or network listener.

The scheduler and authenticated Kubernetes kill/observe adapters are deployment
prerequisites. Calling tick in the broker thread does not satisfy independence.
This module cannot submit/restart work and never turns a kill reply into proof.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import Field

from tessera_control.capability_models import Digest, Identifier

from .capability_activation import (
    ActivationManifest,
    Frozen,
    Nonce,
    Principal,
    canonical,
    stamp,
    verify_document,
)
from .capability_effects import BackendBlocked
from .capability_journal import CapabilityJournal


class Heartbeat(Frozen):
    manifest_digest: Digest
    nonce: Nonce
    sequence: int = Field(ge=1, le=10000)


class TerminationEvidence(Frozen):
    manifest_digest: Digest
    nonce: Nonce
    watchdog_identity: Principal
    job_uid: Identifier
    observed_at: str = Field(max_length=40)
    # Two fresh authenticated API snapshots; retained outside this protocol DB.
    job_absence_response_digest: Digest
    pods_absence_response_digest: Digest
    credential_revocation_digest: Digest
    result: Literal["job-and-owned-pods-absent-credentials-revoked"]


class WatchdogJournal(CapabilityJournal):
    def __init__(self, root):
        super().__init__(root)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sandbox_watchdog (
                    digest TEXT PRIMARY KEY, operation_id TEXT UNIQUE NOT NULL,
                    nonce TEXT UNIQUE NOT NULL, manifest BLOB NOT NULL,
                    job_uid TEXT NOT NULL, sequence INTEGER NOT NULL DEFAULT 0,
                    deadline TEXT NOT NULL, observed_clock TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN
                        ('armed','terminate_requested','unknown','terminated')));
                CREATE TABLE IF NOT EXISTS sandbox_watchdog_events (
                    id INTEGER PRIMARY KEY, digest TEXT NOT NULL, state TEXT NOT NULL,
                    observed_at TEXT NOT NULL, evidence_digest TEXT);
                CREATE TRIGGER IF NOT EXISTS sandbox_watchdog_events_no_update
                    BEFORE UPDATE ON sandbox_watchdog_events
                    BEGIN SELECT RAISE(ABORT,'append only'); END;
                CREATE TRIGGER IF NOT EXISTS sandbox_watchdog_events_no_delete
                    BEFORE DELETE ON sandbox_watchdog_events
                    BEGIN SELECT RAISE(ABORT,'retained watchdog evidence'); END;
            """)


class LeaseWatchdog:
    """One independent owner, authenticated sequential renewal, no automatic retry.

    register() binds an already-known suspended Job UID. A live integration MUST
    arm this lease before unsuspending/submitting work, otherwise a submit/arm gap
    exists. That integration is deliberately not supplied by this protocol core.
    On watchdog restart all prior leases request termination, never resume.
    """

    def __init__(
        self,
        *,
        journal,
        identity,
        heartbeat_key,
        observer_key,
        request_termination,
        authority,
        now=None,
    ):
        if (
            type(heartbeat_key) is not bytes
            or type(observer_key) is not bytes
            or min(len(heartbeat_key), len(observer_key)) < 32
            or heartbeat_key == observer_key
        ):
            raise ValueError("distinct explicit watchdog protocol keys required")
        self.journal, self.identity = journal, identity
        self._heartbeat_key, self._observer_key = heartbeat_key, observer_key
        self.request_termination, self.authority = request_termination, authority
        self.now = now or (lambda: datetime.now(UTC))
        # Persist desired cancellation before any callback. A crash between commit
        # and request remains terminate_requested and is dispatched on tick/reopen.
        with journal.connect() as db:
            rows = db.execute("SELECT digest FROM sandbox_watchdog WHERE state='armed'").fetchall()
            for row in rows:
                self._transition(db, row[0], "terminate_requested")

    def _transition(self, db, digest, state, evidence_digest=None):
        db.execute("UPDATE sandbox_watchdog SET state=? WHERE digest=?", (state, digest))
        db.execute(
            "INSERT INTO sandbox_watchdog_events(digest,state,observed_at,evidence_digest) "
            "VALUES (?,?,?,?)",
            (digest, state, self.now().isoformat(), evidence_digest),
        )

    def _authorized(self, manifest):
        try:
            return self.authority(manifest) is True
        except Exception:
            return False

    def register(self, manifest, *, job_uid):
        # Reparse to avoid mutable/copy/construct bypass of the strict config.
        try:
            manifest = ActivationManifest.model_validate_json(canonical(manifest))
            from pydantic import TypeAdapter

            TypeAdapter(Identifier).validate_python(job_uid, strict=True)
        except Exception:
            raise BackendBlocked("watchdog registration denied") from None
        from hashlib import sha256

        digest = sha256(canonical(manifest)).hexdigest()
        authorized = self._authorized(manifest)
        now = self.now()
        if (
            manifest.watchdog.identity != self.identity
            or not authorized
            or not stamp(manifest.issued_at) <= now < stamp(manifest.expires_at)
        ):
            raise BackendBlocked("watchdog registration denied")
        deadline = min(
            now + timedelta(seconds=manifest.watchdog.lease_seconds), stamp(manifest.expires_at)
        )
        try:
            with self.journal.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT INTO sandbox_watchdog VALUES (?,?,?,?,?,0,?,?,'armed')",
                    (
                        digest,
                        manifest.operation_id,
                        manifest.nonce,
                        canonical(manifest),
                        job_uid,
                        deadline.isoformat(),
                        now.isoformat(),
                    ),
                )
                self._transition(db, digest, "armed")
        except Exception:
            raise BackendBlocked("watchdog registration conflict") from None
        return digest

    def heartbeat(self, document):
        message = verify_document(
            document, Heartbeat, key=self._heartbeat_key, domain=b"tessera-heartbeat-v1"
        )
        denied = False
        with self.journal.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM sandbox_watchdog WHERE digest=?", (message.manifest_digest,)
            ).fetchone()
            if row is None or row["nonce"] != message.nonce:
                raise BackendBlocked("unknown watchdog lease")
            manifest = ActivationManifest.model_validate_json(row["manifest"])
            now = self.now()
            # Control can block/delay; re-read time AFTER its reply. No background
            # renewer may outlive a dead broker or a fenced control authority.
            authorized = self._authorized(manifest)
            now = max(now, self.now())
            if (
                row["state"] != "armed"
                or message.sequence != row["sequence"] + 1
                or now < stamp(row["observed_clock"])
                or now >= stamp(row["deadline"])
                or now >= stamp(manifest.expires_at)
                or not authorized
            ):
                if row["state"] == "armed":
                    self._transition(db, row["digest"], "terminate_requested")
                denied = True
            else:
                deadline = min(
                    now + timedelta(seconds=manifest.watchdog.lease_seconds),
                    stamp(manifest.expires_at),
                )
                db.execute(
                    "UPDATE sandbox_watchdog SET sequence=?, deadline=?, observed_clock=? "
                    "WHERE digest=?",
                    (message.sequence, deadline.isoformat(), now.isoformat(), row["digest"]),
                )
        if denied:
            self.tick()
            raise BackendBlocked("watchdog renewal denied")

    def revoke(self, digest):
        with self.journal.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state FROM sandbox_watchdog WHERE digest=?", (digest,)
            ).fetchone()
            if row is None:
                raise BackendBlocked("unknown watchdog lease")
            if row[0] == "armed":
                self._transition(db, digest, "terminate_requested")
        self.tick()

    def tick(self):
        """Called by an EXTERNAL scheduler; never evidence that one actually exists."""
        with self.journal.connect() as db:
            rows = db.execute(
                "SELECT * FROM sandbox_watchdog WHERE state IN ('armed','terminate_requested')"
            ).fetchall()
        for row in rows:
            manifest = ActivationManifest.model_validate_json(row["manifest"])
            authorized = self._authorized(manifest)
            now = self.now()
            stop = (
                row["state"] == "terminate_requested"
                or not authorized
                or now >= stamp(row["deadline"])
                or now < stamp(row["observed_clock"])
            )
            if not stop:
                continue
            with self.journal.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute(
                    "SELECT state FROM sandbox_watchdog WHERE digest=?", (row["digest"],)
                ).fetchone()[0]
                if current not in {"armed", "terminate_requested"}:
                    continue
                self._transition(db, row["digest"], "terminate_requested")
                # Intent/uncertainty commits BEFORE external kill. On crash or lost
                # reply this is an unresolved operation, not permission to retry.
                self._transition(db, row["digest"], "unknown")
            try:
                self.request_termination(manifest, row["job_uid"])
            except Exception:
                pass  # Raw provider diagnostics are never persisted or returned.

    def reconcile(self, document):
        report = verify_document(
            document, TerminationEvidence, key=self._observer_key, domain=b"tessera-termination-v1"
        )
        with self.journal.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM sandbox_watchdog WHERE digest=?", (report.manifest_digest,)
            ).fetchone()
            intent = db.execute(
                "SELECT observed_at FROM sandbox_watchdog_events WHERE digest=? "
                "AND state='unknown' ORDER BY id DESC LIMIT 1",
                (report.manifest_digest,),
            ).fetchone()
            if (
                row is None
                or intent is None
                or row["state"] != "unknown"
                or (report.nonce, report.job_uid, report.watchdog_identity)
                != (row["nonce"], row["job_uid"], self.identity)
                or not 0 <= (self.now() - stamp(report.observed_at)).total_seconds() <= 5
                or stamp(report.observed_at) < stamp(intent[0])
                or "0" * 64
                in (
                    report.job_absence_response_digest,
                    report.pods_absence_response_digest,
                    report.credential_revocation_digest,
                )
            ):
                raise BackendBlocked("termination evidence rejected")
            self._transition(db, row["digest"], "terminated", document.digest)
        # Historical termination evidence does NOT make the operation re-executable
        # or turn the broker's unknown receipt into completed.
        return {"state": "terminated", "evidence_digest": document.digest}

    def status(self, digest):
        with self.journal.connect() as db:
            row = db.execute(
                "SELECT state FROM sandbox_watchdog WHERE digest=?", (digest,)
            ).fetchone()
        if row is None:
            raise BackendBlocked("unknown watchdog lease")
        return row[0]

    def events(self, digest):
        with self.journal.connect() as db:
            rows = db.execute(
                "SELECT state,observed_at,evidence_digest FROM sandbox_watchdog_events "
                "WHERE digest=? ORDER BY id",
                (digest,),
            ).fetchall()
        return json.loads(json.dumps([dict(row) for row in rows]))
