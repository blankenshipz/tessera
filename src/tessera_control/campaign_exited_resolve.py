"""Automatic resolution of campaign attempts whose worker confirmed the exit (ADR 0053).

A worker reports ``exited`` with termination confirmed when its container is gone. Normally the
run receipt and output manifest follow within moments and the campaign collects and settles.
When they do not (the receipt report was lost, the first observation was unconfirmed and the
attempt went ``unknown``), the attempt and its grant stayed ``running`` or ``unknown`` and the
grant kept holding a worker slot until it expired or a person reconciled it. ``assess`` decides,
from control-plane records only, whether such an attempt can be resolved on evidence. It is a
pure read: the runner applies the result in one transaction
(``CampaignRunner._resolve_exited``).

Every guard must hold, and nothing is invented:

* the attempt is ``admitted``, ``running`` or ``unknown``, unsettled, and its stage's provider
  is a non-billable worker-grant provider (the caller reads the descriptor); a billable or
  route-based provider settles through its own guardian and is never touched here;
* its grant is unresolved and has exactly one external operation;
* that operation's current reconciliation is a confirmed exit with NO contradiction (``exited``,
  termination confirmed, ``contradiction_state == "none"``): an unconfirmed, running or unknown
  latest observation, or a later adverse one, never resolves anything, and an attempt with no
  observation at all stays exactly as it is;
* an ``admitted`` or ``running`` attempt is left to the normal flow whenever a run receipt
  exists, and until its grant expires whenever a lease for it is still live or an artifact
  upload for it (small, or a large chunked one) is still ``uploading`` (the worker reports
  ``exited`` before it collects, uploads and posts the receipt); otherwise it is given
  ``GRACE_SECONDS``, measured from when control recorded the confirmed exit, for the receipt
  and manifest to arrive;
* whatever the attempt's status and whether or not its grant has expired, it is held while an
  output upload for it is still arriving (ADR 0064): a large chunked upload that is open,
  unexpired and has made progress within ``UPLOAD_STALL_SECONDS`` (``UploadProgress`` watches the
  bytes control holds, so a restarted worker resuming its upload counts as progress), or an
  upload or artifact that finished within ``GRACE_SECONDS`` (the manifest and receipt follow it).
  It is closed only after the uploads finish and the grace passes, or an upload stalls or
  expires;
* the cost is recorded and never above the hold: a worker-grant provider costs nothing
  (``local_zero``), and the recorded figure is clamped to the grant's hold.

The outcome is never a pass: ``succeeded`` needs the normal collect-and-verify path. A recorded
non-zero exit is classified as the normal observation does (declared transient code or
permanent); anything else is ``failed_transient`` with ``exit_confirmed_unresolved``, which the
stage's retry policy handles like any transient failure."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .artifact_limits import MAX_EXTENDED_HOLD_SECONDS, OUTPUT_REGISTRATION_GRACE_SECONDS
from .execution import reconcile_grant_row
from .external_operations import confirmed_exit_code, current_operation_reconciliation
from .store import Conflict, canonical_digest

EXIT_CONFIRMED_UNRESOLVED = "exit_confirmed_unresolved"
# Exit codes that mean the infrastructure ended the container (the kernel's out-of-memory killer
# or the engine, SIGKILL), not that the task failed. Unless the spec enabled infrastructure
# retries (ADR 0058) they keep the transient, retried path; every other confirmed nonzero code
# is a final failure carrying that code (ADR 0063). The spec's own codes replace this default.
INFRA_EXIT_CODES = frozenset({137})
GRACE_SECONDS = OUTPUT_REGISTRATION_GRACE_SECONDS
# A large upload whose held bytes have not changed for this long no longer counts as arriving.
UPLOAD_STALL_SECONDS = 600
# Past its grant's expiry an exited attempt is held for its outputs at most this long after control
# recorded the exit, and never beyond the campaign's approval window: a trickle of bytes cannot
# keep a slot and a grant open for the upload's own 24 hours (ADR 0064).
RESOLVABLE = ("admitted", "running", "unknown")
UNRESOLVED_GRANT = ("active", "cancel_requested")


@dataclass(frozen=True)
class Resolution:
    """An attempt resolved on a worker's confirmed exit, and the evidence for it."""

    status: str
    failure_class: str
    detail: str
    cents: int
    basis: str
    evidence: dict[str, Any]

    @property
    def digest(self) -> str:
        return canonical_digest(self.evidence)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _collecting(connection: sqlite3.Connection, grant_id: str, now: datetime) -> bool:
    """True while a lease of the grant is unexpired or an upload (small or large) is going."""
    for lease in connection.execute(
        "SELECT expires_at FROM execution_leases WHERE grant_id=?", (grant_id,)
    ):
        if now < _parse(lease["expires_at"]):
            return True
    if (
        connection.execute(
            """SELECT 1 FROM artifact_uploads u JOIN execution_leases l ON l.id = u.lease_id
            WHERE l.grant_id=? AND u.status='uploading' LIMIT 1""",
            (grant_id,),
        ).fetchone()
        is not None
    ):
        return True  # a small staged upload (single BLOB, at most 16 MiB)
    # A large artifact upload is a ``snapshot_chunk_uploads`` row whose manifest names the lease
    # (large_artifacts.py); an expired upload no longer counts, as for the output budget.
    for upload in connection.execute(
        """SELECT u.expires_at FROM snapshot_chunk_uploads u JOIN execution_leases l
        ON l.id = json_extract(u.manifest_json, '$.lease_id')
        WHERE l.grant_id=? AND u.status='uploading'
        AND json_extract(u.manifest_json, '$.kind') = 'artifact'""",
        (grant_id,),
    ):
        if now < _parse(upload["expires_at"]):
            return True
    return False


class UploadProgress:
    """Watches how many bytes control holds for each open upload, to tell progress from a stall.

    The chunk store keeps no per-write timestamp, so the runner (long-lived, ticking every few
    seconds) records when it last saw an upload's held bytes change. A runner that has just
    started has seen nothing, so an upload is first treated as having just progressed: a restart
    delays a resolution by at most one stall window and never closes an upload early."""

    MAX_TRACKED = 1024

    def __init__(self, stall_seconds: int = UPLOAD_STALL_SECONDS) -> None:
        self.stall_seconds = stall_seconds
        self._seen: dict[str, tuple[int, datetime]] = {}

    def last_change(self, upload_id: str, held: int, now: datetime) -> datetime:
        previous = self._seen.get(upload_id)
        if previous is None or previous[0] != held:
            if len(self._seen) >= self.MAX_TRACKED and upload_id not in self._seen:
                self._seen.pop(next(iter(self._seen)))
            self._seen[upload_id] = (held, now)
            return now
        return previous[1]

    def forget(self, upload_id: str) -> None:
        self._seen.pop(upload_id, None)


def open_uploads(connection: sqlite3.Connection, grant_id: str) -> list[sqlite3.Row]:
    """Large artifact uploads of the grant's leases that are still ``uploading``."""
    return connection.execute(
        """SELECT u.id, u.expires_at FROM snapshot_chunk_uploads u JOIN execution_leases l
        ON l.id = json_extract(u.manifest_json, '$.lease_id')
        WHERE l.grant_id=? AND u.status='uploading'
        AND json_extract(u.manifest_json, '$.kind') = 'artifact'""",
        (grant_id,),
    ).fetchall()


def hold_deadline(
    grant: sqlite3.Row | dict[str, Any],
    exit_recorded: datetime,
    approval_expires: datetime | None = None,
) -> datetime:
    """The latest moment outputs can still hold an exited attempt (ADR 0064).

    The grant's own lifetime always counts (as before this ADR); beyond it the hold runs at most
    ``MAX_EXTENDED_HOLD_SECONDS`` from the recorded exit and never past the approval window."""
    extended = exit_recorded + timedelta(seconds=MAX_EXTENDED_HOLD_SECONDS)
    if approval_expires is not None:
        extended = min(extended, approval_expires)
    return max(_parse(grant["expires_at"]), extended)


def uploads_arriving(
    connection: sqlite3.Connection,
    grant_id: str,
    now: datetime,
    progress: UploadProgress | None = None,
    deadline: datetime | None = None,
    exit_recorded: datetime | None = None,
) -> bool:
    """True while this grant's outputs are still reaching control (ADR 0064).

    With ``exit_recorded`` the first ``GRACE_SECONDS`` after the exit always count, whether or not
    the grant has expired: the worker reports the exit before it hashes and registers its outputs,
    so nothing is open yet (ADR 0086).

    An open large upload that has not expired counts while its held bytes changed within the
    stall window (always, when no ``progress`` watcher is given); a completed upload or an
    artifact that landed within ``GRACE_SECONDS`` counts too, because the manifest and the run
    receipt are posted right after the last byte. Nothing holds at or after ``deadline``.

    The held bytes are every chunk row of the upload, including a chunk that is only partly
    stored (a worker sending a chunk in parts, ADR 0050): each part moves the number, so progress
    is not coarser than one request and survives a worker restart that resumes mid-chunk. A single
    whole-chunk request is bounded by the worker's own request timeout (seconds), far below the
    stall window."""
    if exit_recorded is not None and (now - exit_recorded).total_seconds() < GRACE_SECONDS:
        if progress is not None:  # keep watching, so a stall window is measured from first sight
            for upload in open_uploads(connection, grant_id):
                progress.last_change(upload["id"], held_bytes(connection, upload["id"]), now)
        return True
    if deadline is not None and now >= deadline:
        return False
    for upload in open_uploads(connection, grant_id):
        if now >= _parse(upload["expires_at"]):
            continue
        if progress is None:
            return True
        if (
            now - progress.last_change(upload["id"], held_bytes(connection, upload["id"]), now)
        ).total_seconds() < progress.stall_seconds:
            return True
    if progress is not None:  # a finished upload needs no more watching
        for done in connection.execute(
            """SELECT u.id FROM snapshot_chunk_uploads u JOIN execution_leases l
            ON l.id = json_extract(u.manifest_json, '$.lease_id')
            WHERE l.grant_id=? AND u.status='completed'
            AND json_extract(u.manifest_json, '$.kind') = 'artifact'""",
            (grant_id,),
        ):
            progress.forget(done["id"])
    latest = connection.execute(
        """SELECT max(t) FROM (
            SELECT a.created_at AS t FROM artifacts a JOIN execution_leases l ON l.id=a.lease_id
            WHERE l.grant_id=?
            UNION ALL
            SELECT u.completed_at FROM snapshot_chunk_uploads u JOIN execution_leases l
            ON l.id = json_extract(u.manifest_json, '$.lease_id')
            WHERE l.grant_id=? AND json_extract(u.manifest_json, '$.kind') = 'artifact'
            UNION ALL
            SELECT au.completed_at FROM artifact_uploads au JOIN execution_leases l
            ON l.id = au.lease_id WHERE l.grant_id=?)""",
        (grant_id, grant_id, grant_id),
    ).fetchone()[0]
    return latest is not None and (now - _parse(latest)).total_seconds() < GRACE_SECONDS


def held_bytes(connection: sqlite3.Connection, upload_id: str) -> int:
    return connection.execute(
        "SELECT coalesce(sum(size), 0) FROM snapshot_chunks WHERE owner_id=?", (upload_id,)
    ).fetchone()[0]


def exit_recorded_at(connection: sqlite3.Connection, seen: dict[str, Any]) -> datetime:
    """When control recorded the exit (its own clock), else the reported time."""
    cursor = str(seen["cursor"])
    if cursor.startswith("observation:"):
        row = connection.execute(
            "SELECT recorded_at FROM execution_observations WHERE id=?",
            (cursor.removeprefix("observation:"),),
        ).fetchone()
        if row is not None:
            return _parse(row["recorded_at"])
    return _parse(seen["observed_at"])


def assess(
    connection: sqlite3.Connection,
    attempt: sqlite3.Row | dict[str, Any],
    *,
    now: datetime,
    transient_codes: frozenset[int] | set[int] | tuple[int, ...] = (),
    free_worker_grant: bool = False,
    infra_exit_codes: frozenset[int] | set[int] | tuple[int, ...] = INFRA_EXIT_CODES,
    infra_retries_enabled: bool = False,
    progress: UploadProgress | None = None,
    approval_expires: datetime | None = None,
) -> Resolution | None:
    """A ``Resolution`` when every guard holds, else ``None`` (and the attempt is untouched).

    ``free_worker_grant`` is the caller's reading of the stage's provider descriptor: a
    worker-grant provider that is not billable. Without it nothing is resolved."""
    if (
        not free_worker_grant
        or attempt["status"] not in RESOLVABLE
        or attempt["grant_id"] is None
        or attempt["settled_cents"] is not None
    ):
        return None
    attempt_id, grant_id = attempt["id"], attempt["grant_id"]
    grant = connection.execute("SELECT * FROM execution_grants WHERE id=?", (grant_id,)).fetchone()
    if grant is None or grant["status"] not in UNRESOLVED_GRANT:
        return None
    operations = connection.execute(
        "SELECT operation_id FROM external_operations WHERE grant_id=?", (grant_id,)
    ).fetchall()
    if len(operations) != 1:
        return None
    operation_id = operations[0]["operation_id"]
    seen = current_operation_reconciliation(connection, operation_id)
    if (
        seen is None
        or seen["observed_state"] != "exited"
        or not seen["termination_confirmed"]
        or seen["contradiction_state"] != "none"
    ):
        return None
    receipt = connection.execute(
        "SELECT receipt_digest, exit_code FROM execution_run_receipts WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    manifest = connection.execute(
        "SELECT id FROM artifact_manifests WHERE grant_id=?", (grant_id,)
    ).fetchone()
    exit_recorded = exit_recorded_at(connection, seen)
    deadline = hold_deadline(grant, exit_recorded, approval_expires)
    pre_registration = (
        exit_recorded if attempt["status"] != "unknown" and manifest is None else None
    )
    if uploads_arriving(connection, grant_id, now, progress, deadline, pre_registration):
        return None  # outputs are still reaching control: closing now would discard them
    if attempt["status"] != "unknown":  # admitted or running: the normal flow may still collect it
        if receipt is not None:
            return None  # the normal flow collects and settles it
        if now < _parse(grant["expires_at"]) and _collecting(connection, grant_id, now):
            return None  # a live lease or an upload in progress: the worker is still at it
    hold = int(grant["max_exposure_cents"])
    cents = 0  # a non-billable worker-grant provider costs nothing, so never above the hold
    exit_code = receipt["exit_code"] if receipt is not None else None
    if exit_code is None:
        # No run receipt (a run that left no outputs never posts one): the worker reported
        # the code with its confirmed exit before collecting.
        exit_code = confirmed_exit_code(connection, operation_id)
        if exit_code == 0:
            exit_code = None  # an unreceipted zero is never recorded as if it were evidence
    if exit_code in infra_exit_codes and receipt is None and not infra_retries_enabled:
        # With infrastructure retries off this is the historical retry of a killed container.
        # With them on, the exit is a final nonzero failure here and the runner reclassifies it
        # under the spec's own bound (ADR 0058), so it never spends a scientific retry.
        status, failure_class = "failed_transient", EXIT_CONFIRMED_UNRESOLVED
        detail = (
            f"exit_code:{exit_code}; infrastructure exit, the run receipt or outputs never "
            "completed"
        )
    elif exit_code is not None and exit_code != 0:
        transient = exit_code in transient_codes
        status = "failed_transient" if transient else "failed_permanent"
        failure_class = "t2_declared_exit_code" if transient else "undeclared_exit_code"
        detail = f"exit_code:{exit_code}; resolved on the worker's confirmed exit"
    else:
        status, failure_class = "failed_transient", EXIT_CONFIRMED_UNRESOLVED
        detail = (
            "the worker confirmed the exit but the run receipt or outputs never completed"
            if receipt is None
            else "the worker confirmed the exit after the attempt had gone unknown; run again"
        )
    still_open = [u for u in open_uploads(connection, grant_id) if now < _parse(u["expires_at"])]
    expired_open = len(open_uploads(connection, grant_id)) - len(still_open)
    if still_open:
        why = "the hold ceiling was reached" if now >= deadline else "no bytes arrived in time"
        detail = f"{detail}; {len(still_open)} output upload(s) were still open ({why})"
    evidence = {
        # Exactly what was open when the attempt was settled, and why the hold ended.
        "open_uploads": {"unexpired": len(still_open), "expired": expired_open},
        "hold_ended_by": (None if not still_open else "ceiling" if now >= deadline else "stall"),
        "attempt_id": attempt_id,
        "grant_id": grant_id,
        "operation_id": operation_id,
        "observation_cursor": seen["cursor"],
        "observed_at": seen["observed_at"],
        "run_receipt_digest": None if receipt is None else receipt["receipt_digest"],
        "exit_code": exit_code,
        "manifest_present": manifest is not None,
        "hold_cents": hold,
        "cents": cents,
        "was_status": attempt["status"],
    }
    return Resolution(status, failure_class, detail[:500], cents, "local_zero", evidence)


def release_grant(
    connection: sqlite3.Connection,
    grant: sqlite3.Row | dict[str, Any],
    *,
    cents: int,
    principal_id: str,
    now: str,
) -> int | None:
    """Reconcile one unresolved grant and move its hold to settled; the new revision, else ``None``.

    The single release path shared with the human route and the release of expired finished
    grants (ADR 0048): ``execution.reconcile_grant_row`` does the accounting; this only
    refuses a settlement above the grant's hold. ``None`` when the ledger cannot cover the
    hold (nothing is written)."""
    if cents < 0 or cents > int(grant["max_exposure_cents"]):
        raise ValueError("a settlement may never exceed the grant's hold")
    if (
        int(grant["max_exposure_cents"]) == 0
        and not connection.execute(
            "SELECT 1 FROM execution_project_limits WHERE project_id=?", (grant["project_id"],)
        ).fetchone()
    ):
        # A zero hold moves nothing in a ledger that has no row; only the grant is resolved.
        changed = connection.execute(
            """UPDATE execution_grants SET status='reconciled', revision=revision+1,
            updated_at=?, reconciled_at=?, reconciled_by_principal_id=?, settlement_cents=0
            WHERE id=? AND revision=? AND status IN ('active','cancel_requested')""",
            (now, now, principal_id, grant["id"], grant["revision"]),
        ).rowcount
        if changed != 1:
            raise Conflict("Execution authority is no longer unresolved.")
        return int(grant["revision"]) + 1
    return reconcile_grant_row(
        connection, grant, settlement_cents=cents, principal_id=principal_id, now=now
    )
