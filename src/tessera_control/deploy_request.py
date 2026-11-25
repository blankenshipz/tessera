"""Deploy requests: a gated release deploys itself once its blockers clear (ADR 0165).

A deploy that is ready (merged, gated) but blocked, for example by a remote route still
running, used to wait for a person to notice the blocker cleared. A deploy request records the
intent once: the requested release (a 40-hex revision), who asked, the digest of the gate
evidence that cleared it, the operator plan it runs with, and an expiry. ``tick`` evaluates the
one open request; when every blocker is clear it runs the existing checked upgrade
(``deploy_upgrade.run_upgrade``) exactly once.

Invariants (each one is a test):

* Single flight: every state change happens under an exclusive, non-blocking lock on the
  request directory, held for the whole evaluation and fire. A second ``tick`` while one runs
  is ``busy`` and changes nothing.
* At most once: the request is marked ``firing`` (and the mark is on disk) before the upgrade
  starts. A ``firing`` request found by a later tick was interrupted; it becomes ``failed``
  (``fire_interrupted``) and is never fired again.
* Only the requested release: at fire time the plan file must still have the digest it had
  when the request was made, its ``source_revision`` must equal the release, and the head of
  the named main ref in the plan's ``repo_root`` must still be the release. A main that moved
  refuses the request (``main_moved``); a new request with new gate evidence is needed.
* Fail closed: an expired request refuses (``expired``); a changed plan or gate evidence file
  refuses; an unreadable state database or main head is a blocker, never a pass.
* Receipts: every request, wait (when the blocker set changes), fire, result, refusal and
  cancellation writes an owner-only receipt, each chained to the previous one by digest.

The records live in ``<state-dir>/deploy-requests``, which the control plane reads (never
writes) for ``GET /admin/deploy-requests``. Records hold no secret; the plan and gate evidence
paths are local to the operator host and are left out of the read projection.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import deploy_check

REQUEST_SCHEMA = "tessera.deploy-request.v1"
RECEIPT_SCHEMA = "tessera.deploy-request-receipt.v1"
REQUESTS_DIRNAME = "deploy-requests"
RECEIPTS_DIRNAME = "receipts"
LOCK_NAME = ".lock"
DATABASE_NAME = "tessera.sqlite3"  # the same name store.py uses

OPEN = ("pending", "firing")
STATUSES = ("pending", "firing", "fired", "failed", "refused", "expired", "cancelled")
DEFAULT_TTL_HOURS = 24
MAX_TTL_HOURS = 168
MAX_GATE_EVIDENCE_BYTES = 16 * 1024 * 1024
MAX_RECORD_BYTES = 65536
MAX_LISTED = 50

_REVISION = re.compile(r"^[a-f0-9]{40}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_REQUESTER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:+-]{0,63}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_REQUEST_ID = re.compile(r"^dr-[a-f0-9]{32}$")

# Campaign stage attempt statuses in which compute may still be running or its state is not
# yet known (a remote route, a worker run, an unresolved outcome). A deploy waits while any
# attempt is in one of them: the restart must not race an in-flight route.
IN_FLIGHT_ATTEMPT_STATUSES = ("granted", "admitted", "running", "exited", "collected", "unknown")


class RequestRefused(ValueError):
    """A request could not be made or changed; the message names no path or file value."""


class Busy(RequestRefused):
    """Another tick, create or cancel holds the request directory."""


@dataclass(frozen=True)
class Blocker:
    blocker_id: str
    summary: str

    def payload(self) -> dict[str, str]:
        return {"id": self.blocker_id, "summary": self.summary}


BlockerProbe = Callable[[Path], list[Blocker]]


# -- time, paths, files ---------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def requests_dir(state_dir: Path) -> Path:
    return Path(state_dir) / REQUESTS_DIRNAME


def _ensure_dirs(state_dir: Path) -> Path:
    state_dir = Path(state_dir)
    if not state_dir.is_absolute() or state_dir.is_symlink() or not state_dir.is_dir():
        raise RequestRefused("The state directory must be an existing absolute directory.")
    directory = requests_dir(state_dir)
    for path in (directory, directory / RECEIPTS_DIRNAME):
        if path.is_symlink():
            raise RequestRefused("The deploy request directory must not be a symlink.")
        path.mkdir(mode=0o700, exist_ok=True)
    return directory


@contextlib.contextmanager
def _locked(state_dir: Path) -> Iterator[Path]:
    """The exclusive single-flight lock; never waits."""
    directory = _ensure_dirs(state_dir)
    descriptor = os.open(directory / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy("Another deploy request evaluation is running; retry later.") from None
        yield directory
    finally:
        os.close(descriptor)


def _hash_file(path: Path, limit: int) -> str:
    flags = os.O_RDONLY | os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not _is_regular(info.st_mode) or info.st_size > limit:
            raise RequestRefused("The file must be a regular file within the size bound.")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _is_regular(mode: int) -> bool:
    return stat.S_ISREG(mode)


def _record_path(directory: Path, request_id: str) -> Path:
    if not _REQUEST_ID.match(request_id):
        raise RequestRefused("Unknown deploy request id.")
    return directory / f"{request_id}.json"


def _load_record(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or path.stat().st_size > MAX_RECORD_BYTES:
            return None
        document = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("schema") != REQUEST_SCHEMA:
        return None
    if document.get("status") not in STATUSES:
        return None
    return document


def _records(directory: Path) -> list[dict[str, Any]]:
    if not directory.is_dir():
        return []
    found = []
    for path in directory.glob("dr-*.json"):
        record = _load_record(path)
        if record is not None and record.get("request_id") == path.stem:
            found.append(record)
    found.sort(key=lambda item: (item.get("created_at", ""), item["request_id"]), reverse=True)
    return found


def _save(directory: Path, record: dict[str, Any], now: datetime) -> None:
    record["updated_at"] = _stamp(now)
    record["revision"] = int(record.get("revision", 0)) + 1
    deploy_check.write_private_json(_record_path(directory, record["request_id"]), record)


def _receipt(
    directory: Path, record: dict[str, Any], event: str, now: datetime, **detail: Any
) -> dict[str, Any]:
    """Append one receipt for ``event``, chained to the request's previous receipt."""
    sequence = int(record.get("receipts", 0)) + 1
    document: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "request_id": record["request_id"],
        "seq": sequence,
        "event": event,
        "at": _stamp(now),
        "release": record["release"],
        "status": record["status"],
        "previous_receipt_sha256": record.get("last_receipt_sha256"),
        **detail,
    }
    digest = deploy_check.sha256_hex(json.dumps(document, sort_keys=True).encode())
    document["receipt_sha256"] = digest
    name = f"{record['request_id']}-{sequence:05d}-{event}-{digest[:12]}.json"
    deploy_check.write_private_json(directory / RECEIPTS_DIRNAME / name, document)
    record["last_receipt_sha256"] = digest
    record["receipts"] = sequence
    return document


# -- probes -----------------------------------------------------------------------------------


def in_flight_attempts(state_dir: Path) -> list[Blocker]:
    """Campaign stage attempts that may still be running somewhere (provider-neutral).

    Read-only; an unreadable database is a blocker (fail closed), a database without
    campaign tables has nothing in flight."""
    database = Path(state_dir) / DATABASE_NAME
    marks = ",".join("?" for _ in IN_FLIGHT_ATTEMPT_STATUSES)
    try:
        connection = sqlite3.connect(database.absolute().as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            present = connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='campaign_stage_attempts'"
            ).fetchone()
            if present is None:
                return []
            rows = connection.execute(
                f"""SELECT coalesce(provider_id, 'unnamed') AS provider, status, count(*)
                FROM campaign_stage_attempts WHERE status IN ({marks})
                GROUP BY provider, status ORDER BY provider, status""",
                IN_FLIGHT_ATTEMPT_STATUSES,
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as error:
        return [
            Blocker(
                "state.unreadable",
                f"the control database could not be read ({type(error).__name__})",
            )
        ]
    return [
        Blocker(
            f"route.in_flight:{provider}:{status}",
            f"{count} campaign stage attempt(s) on provider {provider} are {status}",
        )
        for provider, status, count in rows
    ]


DEFAULT_PROBES: tuple[BlockerProbe, ...] = (in_flight_attempts,)


def preflight_blockers(plan: Any) -> list[Blocker]:
    """The checked upgrade's own preflight (dry-run findings that would stop it)."""
    from . import deploy_upgrade

    try:
        _findings, blocking = deploy_upgrade.preflight(plan, smoke=False)
    except (ValueError, OSError) as error:
        return [Blocker("preflight.error", f"preflight could not run ({type(error).__name__})")]
    return [Blocker(f"preflight:{check}", f"deploy check {check} fails") for check in blocking]


def main_head(repo_root: Path, ref: str) -> str | None:
    """The commit ``ref`` names in the checkout at ``repo_root``, or None when git cannot say."""
    if not _REF.match(ref) or ".." in ref:
        return None
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    head = done.stdout.strip()
    return head if done.returncode == 0 and _REVISION.match(head) else None


# -- create / cancel / read ------------------------------------------------------------------


def _load_plan(plan_path: Path) -> Any:
    from .deploy_upgrade_plan import PlanError, load_plan

    try:
        return load_plan(plan_path)
    except PlanError as error:
        raise RequestRefused(str(error)) from None


def create(
    state_dir: Path,
    *,
    plan_path: Path,
    release: str,
    requester: str,
    gate_evidence: Path | None = None,
    gate_evidence_sha256: str | None = None,
    main_ref: str = "main",
    ttl_hours: int = DEFAULT_TTL_HOURS,
    now: datetime | None = None,
    head_of: Callable[[Path, str], str | None] = main_head,
) -> dict[str, Any]:
    """Record a deploy request (idempotent for the same release, plan and gate evidence)."""
    now = now or _now()
    if not _REVISION.match(release):
        raise RequestRefused("The release must be a full 40-character lowercase revision.")
    if not _REQUESTER.match(requester):
        raise RequestRefused("The requester must be a short label (letters, digits, ._@:+-).")
    if not _REF.match(main_ref) or ".." in main_ref:
        raise RequestRefused("The main ref must be a plain git ref name.")
    if type(ttl_hours) is not int or not 1 <= ttl_hours <= MAX_TTL_HOURS:
        raise RequestRefused(f"The expiry must be 1 to {MAX_TTL_HOURS} hours.")
    if (gate_evidence is None) == (gate_evidence_sha256 is None):
        raise RequestRefused("Give exactly one of the gate evidence file or its sha256 digest.")
    if gate_evidence is not None:
        if not Path(gate_evidence).is_absolute():
            raise RequestRefused("The gate evidence file must be an absolute path.")
        try:
            gate_digest = _hash_file(Path(gate_evidence), MAX_GATE_EVIDENCE_BYTES)
        except OSError:
            raise RequestRefused("The gate evidence file could not be read.") from None
    else:
        gate_digest = str(gate_evidence_sha256)
        if not _SHA256.match(gate_digest):
            raise RequestRefused("The gate evidence digest must be 64 lowercase hex characters.")
    plan_path = Path(plan_path)
    plan = _load_plan(plan_path)
    plan_digest = _hash_file(plan_path, 65536)
    if Path(plan.state_dir) != Path(state_dir):
        raise RequestRefused("The plan's state_dir is not this state directory.")
    if plan.source_revision != release:
        raise RequestRefused("The plan's source_revision is not the requested release.")
    if plan.repo_root is None:
        raise RequestRefused("The plan needs repo_root so the request can tell if main moved.")
    head = head_of(Path(plan.repo_root), main_ref)
    if head is None:
        raise RequestRefused("The head of the main ref could not be read.")
    if head != release:
        raise RequestRefused("main_moved: the requested release is not the head of main.")
    with _locked(state_dir) as directory:
        for record in _records(directory):
            if record["status"] not in OPEN:
                continue
            if (
                record["status"] == "pending"
                and record["release"] == release
                and record["plan_sha256"] == plan_digest
                and record["gate_evidence_sha256"] == gate_digest
            ):
                return record
            raise RequestRefused(
                f"Deploy request {record['request_id']} is already open; cancel it first."
            )
        record: dict[str, Any] = {
            "schema": REQUEST_SCHEMA,
            "request_id": "dr-" + uuid.uuid4().hex,
            "release": release,
            "requester": requester,
            "gate_evidence_sha256": gate_digest,
            "gate_evidence_path": None if gate_evidence is None else str(gate_evidence),
            "plan_path": str(plan_path),
            "plan_sha256": plan_digest,
            "main_ref": main_ref,
            "created_at": _stamp(now),
            "expires_at": _stamp(now + timedelta(hours=ttl_hours)),
            "status": "pending",
            "reason": None,
            "blockers": [],
            "checks": 0,
            "last_checked_at": None,
            "fire": None,
            "revision": 0,
        }
        _receipt(
            directory,
            record,
            "requested",
            now,
            requester=requester,
            gate_evidence_sha256=gate_digest,
            plan_sha256=plan_digest,
            main_ref=main_ref,
            expires_at=record["expires_at"],
        )
        _save(directory, record, now)
        return record


def cancel(
    state_dir: Path, request_id: str, *, by: str, now: datetime | None = None
) -> dict[str, Any]:
    """Cancel a pending request; a firing one cannot be cancelled (the upgrade is running)."""
    now = now or _now()
    if not _REQUESTER.match(by):
        raise RequestRefused("The canceller must be a short label (letters, digits, ._@:+-).")
    with _locked(state_dir) as directory:
        record = _load_record(_record_path(directory, request_id))
        if record is None:
            raise RequestRefused("Unknown deploy request id.")
        if record["status"] != "pending":
            raise RequestRefused(f"The request is {record['status']}; only a pending one cancels.")
        record.update(status="cancelled", reason="cancelled")
        _receipt(directory, record, "cancelled", now, by=by)
        _save(directory, record, now)
        return record


def public_view(record: Mapping[str, Any]) -> dict[str, Any]:
    """The record without host-local paths (what the control API and MCP return)."""
    hidden = {"plan_path", "gate_evidence_path"}
    view = {key: value for key, value in record.items() if key not in hidden}
    view["gate_evidence_rechecked"] = record.get("gate_evidence_path") is not None
    return view


def receipts_for(state_dir: Path, request_id: str, limit: int = 100) -> list[dict[str, Any]]:
    directory = requests_dir(state_dir) / RECEIPTS_DIRNAME
    if not _REQUEST_ID.match(request_id) or not directory.is_dir():
        return []
    found = []
    for path in directory.glob(f"{request_id}-*.json"):
        try:
            if path.is_symlink() or path.stat().st_size > MAX_RECORD_BYTES:
                continue
            document = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(document, dict) and document.get("schema") == RECEIPT_SCHEMA:
            found.append(document)
    found.sort(key=lambda item: (_seq(item), str(item.get("receipt_sha256", ""))))
    return found[-limit:]


def _seq(receipt: Mapping[str, Any]) -> int:
    value = receipt.get("seq")
    return value if type(value) is int else 0


def list_requests(state_dir: Path, limit: int = MAX_LISTED) -> list[dict[str, Any]]:
    """Newest first; read-only (creates nothing)."""
    return _records(requests_dir(state_dir))[:limit]


def show(state_dir: Path, request_id: str) -> dict[str, Any]:
    if not _REQUEST_ID.match(request_id):
        raise RequestRefused("Unknown deploy request id.")
    record = _load_record(_record_path(requests_dir(state_dir), request_id))
    if record is None:
        raise RequestRefused("Unknown deploy request id.")
    return record


# -- tick: evaluate and fire once -------------------------------------------------------------


def _default_upgrade(plan: Any) -> dict[str, Any]:
    from . import deploy_upgrade

    document = deploy_upgrade.run_upgrade(plan, execute=True)
    path = deploy_upgrade.write_receipt(plan, document)
    return {**document, "receipt_file": path.name}


def _end(
    directory: Path,
    record: dict[str, Any],
    status: str,
    reason: str,
    now: datetime,
    event: str,
    **detail: Any,
) -> dict[str, Any]:
    record.update(status=status, reason=reason)
    _receipt(directory, record, event, now, reason=reason, **detail)
    _save(directory, record, now)
    return {"outcome": status, "request_id": record["request_id"], "reason": reason}


def tick(
    state_dir: Path,
    *,
    now: datetime | None = None,
    probes: Sequence[BlockerProbe] = DEFAULT_PROBES,
    preflight: Callable[[Any], list[Blocker]] = preflight_blockers,
    head_of: Callable[[Path, str], str | None] = main_head,
    upgrade: Callable[[Any], Mapping[str, Any]] = _default_upgrade,
) -> dict[str, Any]:
    """Evaluate the open request once; fire the upgrade when nothing blocks it.

    Returns ``{"outcome": ...}``: ``idle`` (no open request), ``busy`` (another evaluation
    holds the lock), ``waiting`` (with the blockers), ``fired``, ``failed``, ``refused`` or
    ``expired``."""
    now = now or _now()
    try:
        with _locked(state_dir) as directory:
            return _tick_locked(
                Path(state_dir), directory, now, probes, preflight, head_of, upgrade
            )
    except Busy:
        return {"outcome": "busy"}


def _tick_locked(state_dir, directory, now, probes, preflight, head_of, upgrade):
    open_records = [record for record in _records(directory) if record["status"] in OPEN]
    if not open_records:
        return {"outcome": "idle"}
    # create() admits one open request at a time; if more exist (edited by hand), the oldest
    # is evaluated and the rest wait their turn.
    record = sorted(open_records, key=lambda item: (item["created_at"], item["request_id"]))[0]
    if record["status"] == "firing":
        # The mark was written before the upgrade began and the lock is held for the whole
        # fire, so a firing record seen here belongs to a run that died. Never fire again.
        return _end(directory, record, "failed", "fire_interrupted", now, "fire_failed")
    if now >= _parse(record["expires_at"]):
        return _end(directory, record, "expired", "expired", now, "refused")
    plan_path = Path(record["plan_path"])
    try:
        digest = _hash_file(plan_path, 65536)
    except (OSError, RequestRefused):
        digest = None
    if digest != record["plan_sha256"]:
        return _end(directory, record, "refused", "plan_changed", now, "refused")
    try:
        plan = _load_plan(plan_path)
    except RequestRefused:
        return _end(directory, record, "refused", "plan_invalid", now, "refused")
    if plan.source_revision != record["release"] or Path(plan.state_dir) != state_dir:
        return _end(directory, record, "refused", "release_mismatch", now, "refused")
    if record.get("gate_evidence_path"):
        try:
            gate = _hash_file(Path(record["gate_evidence_path"]), MAX_GATE_EVIDENCE_BYTES)
        except (OSError, RequestRefused):
            gate = None
        if gate != record["gate_evidence_sha256"]:
            return _end(directory, record, "refused", "gate_evidence_changed", now, "refused")

    blockers: list[Blocker] = []
    head = None if plan.repo_root is None else head_of(Path(plan.repo_root), record["main_ref"])
    if head is None:
        blockers.append(Blocker("main.unreadable", "the head of the main ref could not be read"))
    elif head != record["release"]:
        return _end(directory, record, "refused", "main_moved", now, "refused", main_head=head)
    for probe in probes:
        try:
            blockers.extend(probe(state_dir))
        except Exception as error:  # a probe that cannot answer blocks (fail closed)
            failed = type(error).__name__
            blockers.append(Blocker("probe.error", f"a blocker probe failed ({failed})"))
    blockers.extend(preflight(plan))

    record["checks"] = int(record.get("checks", 0)) + 1
    record["last_checked_at"] = _stamp(now)
    if blockers:
        payload = [blocker.payload() for blocker in blockers]
        if [item["id"] for item in payload] != [item["id"] for item in record["blockers"]]:
            # One receipt per change in the blocker set, not per tick.
            record["blockers"] = payload
            _receipt(directory, record, "waiting", now, blockers=payload)
        record["blockers"] = payload
        _save(directory, record, now)
        return {"outcome": "waiting", "request_id": record["request_id"], "blockers": payload}

    # Clear: mark firing on disk first, so this release is upgraded at most once.
    record.update(status="firing", reason=None, blockers=[])
    _receipt(directory, record, "fire", now, main_head=head, plan_sha256=record["plan_sha256"])
    _save(directory, record, now)
    try:
        document = dict(upgrade(plan))
    except Exception as error:
        record["fire"] = {"result": "error", "error": type(error).__name__}
        return _end(
            directory,
            record,
            "failed",
            "upgrade_error",
            _now_after(now),
            "fire_failed",
            error=type(error).__name__,
        )
    summary = {
        "result": document.get("result"),
        "failed_step": document.get("failed_step"),
        "source_revision": document.get("source_revision"),
        "deploy_receipt_file": document.get("receipt_file"),
        "deploy_receipt_sha256": deploy_check.sha256_hex(
            json.dumps(document, sort_keys=True, default=str).encode()
        ),
    }
    record["fire"] = summary
    if document.get("result") == "ok":
        return _end(directory, record, "fired", "deployed", _now_after(now), "fired", **summary)
    return _end(
        directory, record, "failed", "upgrade_failed", _now_after(now), "fire_failed", **summary
    )


def _now_after(started: datetime) -> datetime:
    """Wall-clock time after the upgrade, never before the tick's own time."""
    return max(started, _now())


__all__ = [
    "DEFAULT_PROBES",
    "IN_FLIGHT_ATTEMPT_STATUSES",
    "RECEIPT_SCHEMA",
    "REQUEST_SCHEMA",
    "Blocker",
    "Busy",
    "RequestRefused",
    "cancel",
    "create",
    "in_flight_attempts",
    "list_requests",
    "main_head",
    "public_view",
    "receipts_for",
    "show",
    "tick",
]
