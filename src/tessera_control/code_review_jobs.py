"""Code review jobs: side table and the system runner protocol (ADR 0092).

A ``code_review`` review job (ADR 0084) carries a content-addressed subject (snapshot or
base..head diff plus a pinned rubric) in ``code_review_jobs``. The runner claims, leases and
finishes these jobs through the methods below; each is one ``BEGIN IMMEDIATE`` transaction that
writes the job row, the side row and the event together. No method here runs a model or invents
a receipt, finding or verdict; ``complete_code_review`` stores only validated runner output.

Every attempt's outcome is appended to ``attempt_receipts_json`` with the model-call receipt the
runner reported (or none), so a paid call that failed or vanished still leaves its cost record.
A receipt is ``bound`` when it names exactly the job's inputs (by snapshot id and digest), the
job's prompt digest, the most private input class, the job's destination as its provider, and
(for ``anthropic``) a pinned-price cost and a provider-reported model; only a bound receipt can
complete a job.

A runner whose lease lapsed (expired, or already reaped) may still report the receipt of the
attempt it held: it is appended as a ``late`` entry with a ``review_job.late_receipt`` event,
the job's state, findings and verdict are left unchanged, and the call is still a Conflict.
``lease_history_json`` records which runner held each attempt, so only that runner can report.
"""

import hashlib
import json

from pydantic import ValidationError

from .review_jobs_models import ModelCallReceipt
from .store import Conflict, NotFound, _connect, canonical_digest

RUNNER_ACTOR = "system:code-review-runner"
FAIL_REASONS = (
    "budget_exceeded",
    "model_failed",
    "failed_output_invalid",
    "model_child_vanished",
    "input_too_large",
    "input_unavailable",
    "prompt_not_pinned",
)
MAX_ATTEMPTS = 2
MAX_RUNNERS_BOUND = 8
# Least to most private (ADR 0047 classes); a receipt names the most private class it was sent.
CLASS_ORDER = ("public", "private_code_approved", "private_derived")
# Total custody bytes a code review may read across its inputs (the legacy snapshot bound).
MAX_CODE_REVIEW_INPUT_BYTES = 16 * 1024 * 1024


def create_code_review_schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS code_review_jobs (
        job_id TEXT PRIMARY KEY REFERENCES review_jobs(id),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        mode TEXT NOT NULL CHECK(mode IN ('snapshot','diff')),
        base_digest TEXT CHECK(base_digest IS NULL OR length(base_digest)=64),
        head_digest TEXT NOT NULL CHECK(length(head_digest)=64),
        rubric_digest TEXT NOT NULL CHECK(length(rubric_digest)=64),
        verdict TEXT CHECK(verdict IS NULL OR verdict IN
            ('MERGE','MERGE-WITH-FOLLOWUPS','CHANGES-REQUIRED')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 2),
        lease_owner TEXT, lease_expires_at REAL,
        receipt_json TEXT, review_receipt_json TEXT, review_receipt_digest TEXT,
        attempt_receipts_json TEXT NOT NULL DEFAULT '[]',
        lease_history_json TEXT NOT NULL DEFAULT '{}')""")
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS code_review_jobs_no_delete BEFORE DELETE ON code_review_jobs "
        "BEGIN SELECT RAISE(ABORT,'code review jobs are permanent'); END"
    )
    # Host-wide bound: every live runner records its configured limit; a claim honours the
    # smallest limit among runners seen within the claimant's lease window.
    connection.execute("""CREATE TABLE IF NOT EXISTS code_review_runners (
        runner_id TEXT PRIMARY KEY,
        max_running INTEGER NOT NULL CHECK(max_running BETWEEN 1 AND 8),
        seen_at REAL NOT NULL)""")


def most_private_class(inputs):
    """The most private class among ``inputs``; an unknown or missing class reads as the most
    private (fail closed)."""
    ranks = [
        CLASS_ORDER.index(item.get("data_class"))
        if item.get("data_class") in CLASS_ORDER
        else len(CLASS_ORDER) - 1
        for item in inputs
    ]
    return CLASS_ORDER[max(ranks, default=len(CLASS_ORDER) - 1)]


def receipt_bound(row, receipt):
    """True when a normalized receipt is bound to the job ``row`` (a ``review_jobs`` row).

    (a) its input digests are exactly ``{snapshot_id: digest}`` for every job input; (b) its
    ``prompt_sha256`` is the job's prompt digest; (c) it names the most private input class;
    (d) the provider prefix of ``model_configured`` is the job's destination; (e) for
    ``anthropic``, the cost is computed from pinned prices and the model is the one the
    provider's response named (no subscription or harness-reported route).
    """
    inputs = json.loads(row["inputs_json"])
    expected = {item["snapshot_id"]: item["digest"] for item in inputs}
    if len(expected) != len(inputs) or receipt["input_digests"] != expected:
        return False
    prompt = receipt.get("prompt_sha256")
    if prompt is None or prompt != row["prompt_digest"]:
        return False
    if receipt.get("input_class") != most_private_class(inputs):
        return False
    provider, separator, model = receipt["model_configured"].partition("/")
    if not separator or not model or provider != row["destination_id"]:
        return False
    if row["destination_id"] == "anthropic" and (
        receipt["cost_basis"] != "computed_from_pinned_prices"
        or receipt["model_identity_basis"] != "provider_response_model"
    ):
        return False
    return True


def normalized_receipt(value):
    """``(receipt, invalid)``: the stored form of a structurally valid receipt, or ``None``."""
    if value is None:
        return None, False
    try:
        return ModelCallReceipt.model_validate(value).model_dump(mode="json"), False
    except ValidationError:
        return None, True


def attempt_entry(row, attempt, outcome, reason, receipt):
    """One ``attempt_receipts`` entry. An invalid receipt is not stored, only flagged."""
    stored, invalid = normalized_receipt(receipt)
    return {
        "attempt": attempt,
        "outcome": outcome,
        "reason": reason,
        "receipt": stored,
        "receipt_digest": canonical_digest(stored) if stored is not None else None,
        "bound": stored is not None and receipt_bound(row, stored),
        "receipt_invalid": invalid,
    }


def append_attempt(connection, job_id, entry):
    current = connection.execute(
        "SELECT attempt_receipts_json FROM code_review_jobs WHERE job_id=?", (job_id,)
    ).fetchone()[0]
    entries = json.loads(current or "[]")
    entries.append(entry)
    connection.execute(
        "UPDATE code_review_jobs SET attempt_receipts_json=? WHERE job_id=?",
        (json.dumps(entries, sort_keys=True), job_id),
    )


def code_review_record(connection, job_id):
    row = connection.execute("SELECT * FROM code_review_jobs WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        return None
    attempts = json.loads(row["attempt_receipts_json"] or "[]")
    return {
        "mode": row["mode"],
        "base_digest": row["base_digest"],
        "head_digest": row["head_digest"],
        "rubric_digest": row["rubric_digest"],
        "verdict": row["verdict"],
        "attempts": row["attempts"],
        "receipt": json.loads(row["receipt_json"]) if row["receipt_json"] else None,
        "review_receipt_digest": row["review_receipt_digest"],
        "attempt_receipts": attempts,
        "cost_cents_total": sum(
            (entry["receipt"].get("cost_cents") or 0)
            for entry in attempts
            if entry.get("receipt") is not None
        ),
    }


def _snapshot_content(connection, row, item):
    """Verified custody bytes of one job input, read in the job's own project and tenant."""
    from .snapshot_chunks import check_chunk_metadata, read_chunk
    from .snapshots import MAX_SNAPSHOT_BYTES, MAX_SNAPSHOT_FILES, SnapshotStore

    snapshot = connection.execute(
        "SELECT s.id, s.digest, substr(s.manifest_json, 1, 4194305) AS manifest_json, "
        "EXISTS(SELECT 1 FROM snapshot_chunk_uploads u WHERE u.id = s.id "
        "AND u.status = 'completed') AS chunked "
        "FROM source_snapshots s JOIN projects p ON p.id = s.project_id "
        "WHERE s.id=? AND s.project_id=? AND p.org_id=?",
        (item["snapshot_id"], row["project_id"], row["org_id"]),
    ).fetchone()
    if snapshot is None or snapshot["digest"] != item["digest"]:
        raise Conflict("Code review input is not in the job's custody.")
    # Parses the manifest within its bounds and checks it hashes to the snapshot digest.
    manifest = SnapshotStore._response(snapshot)["manifest"]
    entries = manifest["entries"]
    files = []
    if snapshot["chunked"]:
        if connection.execute(
            "SELECT count(*) FROM snapshot_files WHERE snapshot_id=?", (snapshot["id"],)
        ).fetchone()[0]:
            raise Conflict("Snapshot file set integrity check failed.")
        check_chunk_metadata(connection, snapshot["id"], entries)
        seq = 0
        for entry in entries:
            parts, total = [], 0
            while total < entry["size"]:
                meta, content = read_chunk(connection, snapshot["id"], seq)
                if meta["relative_path"] != entry["relative_path"] or meta["offset"] != total:
                    raise Conflict("Snapshot chunk set integrity check failed.")
                parts.append(content)
                total += len(content)
                seq += 1
            content = b"".join(parts)
            if len(content) != entry["size"] or (
                hashlib.sha256(content).hexdigest() != entry["sha256"]
            ):
                raise Conflict("Snapshot file integrity check failed.")
            files.append({"path": entry["relative_path"], "content": content})
        return files
    if len(entries) > MAX_SNAPSHOT_FILES or manifest["total_bytes"] > MAX_SNAPSHOT_BYTES:
        raise Conflict("Snapshot exceeds its bound.")
    count = connection.execute(
        "SELECT count(*) FROM snapshot_files WHERE snapshot_id=?", (snapshot["id"],)
    ).fetchone()[0]
    if count != len(entries):
        raise Conflict("Snapshot file set integrity check failed.")
    for entry in entries:
        stored = connection.execute(
            "SELECT CASE WHEN length(content)=? AND length(content)<=? THEN content ELSE NULL END "
            "AS content FROM snapshot_files WHERE snapshot_id=? AND relative_path=?",
            (entry["size"], MAX_SNAPSHOT_BYTES, snapshot["id"], entry["relative_path"]),
        ).fetchone()
        content = None if stored is None else stored["content"]
        if (
            not isinstance(content, bytes)
            or len(content) != entry["size"]
            or hashlib.sha256(content).hexdigest() != entry["sha256"]
        ):
            raise Conflict("Snapshot file integrity check failed.")
        files.append({"path": entry["relative_path"], "content": content})
    return files


# Outcomes a runner reports itself; once an attempt has one, no late receipt is taken for it.
RUNNER_REPORTED_OUTCOMES = ("completed", "failed", "late")


class _LateReceipt:
    """Sentinel returned from a transaction that recorded a late receipt, so the append commits
    before the caller raises the Conflict."""

    def __init__(self, message):
        self.message = message


class CodeReviewRunStore:
    """System-level runner protocol (no principal). Mixed into ``ReviewJobStore``.

    Events are attributed to the job's submitting principal (``events.actor_principal_id`` is a
    foreign key to principals) and carry ``actor: "system:code-review-runner"`` in the payload.
    """

    # -- helpers --------------------------------------------------------------------------

    def _system(self, work):
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = work(connection)
            connection.execute("COMMIT")
            return result
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _system_read(self, work):
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN")
            return work(connection)
        finally:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            connection.close()

    def _system_event(self, connection, row, event_type, payload):
        self._event(
            connection,
            {"id": row["actor_id"], "org_id": row["org_id"]},
            row["project_id"],
            None,
            event_type,
            row["revision"] + 1,
            {"job_id": row["id"], "actor": RUNNER_ACTOR, **payload},
        )

    @staticmethod
    def _runner_job(connection, job_id, runner_id, now=None):
        """The running job leased by ``runner_id``; with ``now``, the lease must still be valid
        (an expired but unreaped lease is a Conflict: the reaper owns that job)."""
        row = connection.execute(
            "SELECT j.*, c.attempts AS cr_attempts, c.lease_owner AS cr_lease_owner, "
            "c.lease_expires_at AS cr_lease_expires_at "
            "FROM review_jobs j JOIN code_review_jobs c ON c.job_id=j.id "
            "WHERE j.id=? AND c.org_id=j.org_id",
            (job_id,),
        ).fetchone()
        if row is None:
            raise NotFound("Code review job was not found.")
        if row["kind"] != "code_review" or row["state"] != "running":
            raise Conflict("Code review job is not running.")
        if runner_id is not None and row["cr_lease_owner"] != runner_id:
            raise Conflict("Code review job is leased by another runner.")
        if now is not None and (
            row["cr_lease_expires_at"] is None or row["cr_lease_expires_at"] < now
        ):
            raise Conflict("Code review job lease has expired.")
        return row

    @staticmethod
    def _late_attempt(connection, job_id, runner_id, now):
        """``(row, attempt)`` when ``runner_id`` held ``attempt`` of this code review and its
        lease has lapsed (expired, or reaped and cleared), and that attempt has no receipt or
        runner-reported outcome yet; otherwise ``(row, None)``. Never changes anything."""
        row = connection.execute(
            "SELECT j.*, c.attempts AS cr_attempts, c.lease_owner AS cr_lease_owner, "
            "c.lease_expires_at AS cr_lease_expires_at, "
            "c.lease_history_json AS cr_lease_history_json, "
            "c.attempt_receipts_json AS cr_attempt_receipts_json "
            "FROM review_jobs j JOIN code_review_jobs c ON c.job_id=j.id "
            "WHERE j.id=? AND c.org_id=j.org_id",
            (job_id,),
        ).fetchone()
        if row is None or row["kind"] != "code_review" or not runner_id:
            return row, None
        if row["state"] == "running" and row["cr_lease_owner"] == runner_id:
            if row["cr_lease_expires_at"] is not None and row["cr_lease_expires_at"] >= now:
                return row, None  # a valid lease: the normal path decides
            attempt = row["cr_attempts"]
        else:
            history = json.loads(row["cr_lease_history_json"] or "{}")
            held = [int(n) for n, owner in history.items() if owner == runner_id]
            if not held:
                return row, None
            attempt = max(held)
            if row["cr_lease_owner"] == runner_id and attempt == row["cr_attempts"]:
                return row, None  # still this runner's live attempt (not running: not late)
        for entry in json.loads(row["cr_attempt_receipts_json"] or "[]"):
            if entry.get("attempt") != attempt:
                continue
            if entry.get("outcome") in RUNNER_REPORTED_OUTCOMES or entry.get("receipt") is not None:
                return row, None
        return row, attempt

    def _record_late(self, connection, job_id, runner_id, reason, receipt, now):
        """Append a lapsed lease holder's receipt (state, findings and verdict unchanged) and
        return the sentinel; ``None`` when there is nothing to record."""
        if receipt is None:
            return None
        row, attempt = self._late_attempt(connection, job_id, runner_id, now)
        if attempt is None:
            return None
        entry = attempt_entry(row, attempt, "late", reason, receipt)
        append_attempt(connection, job_id, entry)
        self._event(
            connection,
            {"id": row["actor_id"], "org_id": row["org_id"]},
            row["project_id"],
            None,
            "review_job.late_receipt",
            row["revision"],  # the job's state is unchanged
            {
                "job_id": job_id,
                "actor": RUNNER_ACTOR,
                "attempt": attempt,
                "reason": reason,
                "receipt_digest": entry["receipt_digest"],
            },
        )
        return _LateReceipt("Code review job lease has lapsed; the late receipt was recorded.")

    def _system_late(self, work):
        """Run ``work``; a recorded late receipt commits, then raises its Conflict."""
        result = self._system(work)
        if isinstance(result, _LateReceipt):
            raise Conflict(result.message)
        return result

    def _fresh(self, connection, job_id):
        return self._job_record(
            connection,
            connection.execute("SELECT * FROM review_jobs WHERE id=?", (job_id,)).fetchone(),
        )

    @staticmethod
    def _clear_lease(connection, job_id):
        connection.execute(
            "UPDATE code_review_jobs SET lease_owner=NULL,lease_expires_at=NULL WHERE job_id=?",
            (job_id,),
        )

    def _vanished(self, connection, row, receipt=None):
        self._clear_lease(connection, row["id"])
        attempt = row["cr_attempts"]
        entry = attempt_entry(row, attempt, "vanished", "model_child_vanished", receipt)
        append_attempt(connection, row["id"], entry)
        payload = {"attempt": attempt, "reason": "model_child_vanished"}
        if entry["receipt_digest"] is not None:
            payload["receipt_digest"] = entry["receipt_digest"]
        if attempt < MAX_ATTEMPTS:
            self._bump(connection, row["id"], "queued", row["revision"])
            self._system_event(connection, row, "review_job.retry_scheduled", payload)
            return "queued"
        self._bump(connection, row["id"], "failed", row["revision"], "model_child_vanished")
        self._system_event(connection, row, "review_job.failed", payload)
        return "failed"

    # -- protocol -------------------------------------------------------------------------

    def claim_code_review(self, runner_id, max_running, lease_seconds, now):
        if not runner_id or lease_seconds <= 0:
            raise ValueError("runner_id and a positive lease are required")
        if (
            not isinstance(max_running, int)
            or isinstance(max_running, bool)
            or not 1 <= max_running <= MAX_RUNNERS_BOUND
        ):
            raise ValueError("max_running must be between 1 and 8")

        def work(connection):
            connection.execute(
                "INSERT INTO code_review_runners (runner_id,max_running,seen_at) VALUES (?,?,?) "
                "ON CONFLICT(runner_id) DO UPDATE SET max_running=excluded.max_running,"
                "seen_at=excluded.seen_at",
                (runner_id, max_running, now),
            )
            # Host bound: the smallest limit among live runners (this one included) applies to
            # running code reviews across every org.
            bound = connection.execute(
                "SELECT MIN(max_running) FROM code_review_runners WHERE seen_at>=?",
                (now - lease_seconds,),
            ).fetchone()[0]
            running = connection.execute(
                "SELECT COUNT(*) FROM review_jobs WHERE kind='code_review' AND state='running'"
            ).fetchone()[0]
            if running >= bound:
                return None
            row = connection.execute(
                "SELECT j.*, c.attempts AS cr_attempts FROM review_jobs j "
                "JOIN code_review_jobs c ON c.job_id=j.id AND c.org_id=j.org_id "
                "WHERE j.kind='code_review' AND j.state='queued' AND c.attempts<? "
                "ORDER BY j.sequence LIMIT 1",
                (MAX_ATTEMPTS,),
            ).fetchone()
            if row is None:
                return None
            attempt = row["cr_attempts"] + 1
            history = json.loads(
                connection.execute(
                    "SELECT lease_history_json FROM code_review_jobs WHERE job_id=?", (row["id"],)
                ).fetchone()[0]
                or "{}"
            )
            history[str(attempt)] = runner_id
            connection.execute(
                "UPDATE code_review_jobs SET attempts=?,lease_owner=?,lease_expires_at=?,"
                "lease_history_json=? WHERE job_id=?",
                (
                    attempt,
                    runner_id,
                    now + lease_seconds,
                    json.dumps(history, sort_keys=True),
                    row["id"],
                ),
            )
            self._bump(connection, row["id"], "running", row["revision"])
            self._system_event(connection, row, "review_job.running", {"attempt": attempt})
            return self._fresh(connection, row["id"])

        return self._system(work)

    def renew_code_review_lease(self, job_id, runner_id, lease_seconds, now):
        if lease_seconds <= 0:
            raise ValueError("a positive lease is required")

        def work(connection):
            updated = connection.execute(
                "UPDATE code_review_jobs SET lease_expires_at=? WHERE job_id=? AND lease_owner=? "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at>=? "
                "AND EXISTS (SELECT 1 FROM review_jobs j WHERE j.id=code_review_jobs.job_id "
                "AND j.state='running' AND j.kind='code_review')",
                (now + lease_seconds, job_id, runner_id, now),
            ).rowcount
            if updated == 1:
                # A busy runner stays counted in the host bound while it holds leases.
                connection.execute(
                    "UPDATE code_review_runners SET seen_at=MAX(seen_at,?) WHERE runner_id=?",
                    (now, runner_id),
                )
            return updated == 1

        return self._system(work)

    def code_review_inputs(self, job_id, runner_id, now):
        """Verified custody bytes for each job input, for the runner holding a valid lease.

        Returns ``[{"snapshot_id", "digest", "data_class", "files": [{"path", "content"}]}]``.
        Content is read only from the job's own project and tenant and must hash to the
        snapshot's manifest and digest; any mismatch, or a total above the byte bound, is a
        Conflict.
        """

        def work(connection):
            row = self._runner_job(connection, job_id, runner_id, now)
            items, total = [], 0
            for item in json.loads(row["inputs_json"]):
                files = _snapshot_content(connection, row, item)
                total += sum(len(entry["content"]) for entry in files)
                if total > MAX_CODE_REVIEW_INPUT_BYTES:
                    raise Conflict("Code review inputs exceed their byte bound.")
                items.append(
                    {
                        "snapshot_id": item["snapshot_id"],
                        "digest": item["digest"],
                        "data_class": item["data_class"],
                        "files": files,
                    }
                )
            return items

        return self._system_read(work)

    def requeue_vanished_code_review(self, job_id, runner_id, now, receipt=None):
        return self._system(
            lambda connection: self._vanished(
                connection, self._runner_job(connection, job_id, runner_id), receipt
            )
        )

    def complete_code_review(self, job_id, runner_id, output, now):
        """Store validated runner output. A lapsed lease holder's receipt is recorded as late
        (no findings or verdict stored) and the call is still a Conflict."""

        def work(connection):
            receipt = output.get("receipt") if isinstance(output, dict) else None
            late = self._record_late(
                connection, job_id, runner_id, "completed_after_lease", receipt, now
            )
            if late is not None:
                return late
            row = self._runner_job(connection, job_id, runner_id, now)
            body = self._validated_output(row, output)
            self._clear_lease(connection, job_id)
            # _store_output appends the completed attempt's receipt.
            return self._store_output(
                connection,
                {"id": row["actor_id"], "org_id": row["org_id"]},
                row,
                body,
                RUNNER_ACTOR,
            )

        return self._system_late(work)

    def fail_code_review(self, job_id, runner_id, reason, now, receipt=None):
        if reason not in FAIL_REASONS:
            raise ValueError("unknown code review failure reason")

        def work(connection):
            late = self._record_late(connection, job_id, runner_id, reason, receipt, now)
            if late is not None:
                return late
            row = self._runner_job(connection, job_id, runner_id, now)
            entry = attempt_entry(row, row["cr_attempts"], "failed", reason, receipt)
            append_attempt(connection, job_id, entry)
            self._clear_lease(connection, job_id)
            columns = {}
            payload = {"attempt": row["cr_attempts"], "reason": reason}
            if entry["receipt_digest"] is not None:
                columns["receipt_digest"] = entry["receipt_digest"]
                payload["receipt_digest"] = entry["receipt_digest"]
            self._bump(connection, job_id, "failed", row["revision"], reason, **columns)
            self._system_event(connection, row, "review_job.failed", payload)
            return self._fresh(connection, job_id)

        return self._system_late(work)

    def not_start_code_review(self, job_id, runner_id, reason, now):
        from .review_jobs import NOT_START_REASONS

        if reason not in NOT_START_REASONS:
            raise ValueError("unknown code review not-started reason")

        def work(connection):
            row = self._runner_job(connection, job_id, runner_id)
            self._clear_lease(connection, job_id)
            self._bump(connection, job_id, "not_started", row["revision"], reason)
            self._system_event(connection, row, "review_job.not_started", {"reason": reason})
            return self._fresh(connection, job_id)

        return self._system(work)

    def reap_expired_code_review_leases(self, now):
        def work(connection):
            expired = connection.execute(
                "SELECT j.id FROM review_jobs j JOIN code_review_jobs c ON c.job_id=j.id "
                "WHERE j.kind='code_review' AND j.state='running' "
                "AND c.lease_expires_at IS NOT NULL AND c.lease_expires_at<? ORDER BY j.sequence",
                (now,),
            ).fetchall()
            for item in expired:
                self._vanished(connection, self._runner_job(connection, item["id"], None))
            return [item["id"] for item in expired]

        return self._system(work)
