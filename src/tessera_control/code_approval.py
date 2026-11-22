"""Owner-approved private code in a staged input snapshot (ADR 0039, amendment 1).

A staged input snapshot may hold two kinds of file:

* **data**, governed by the snapshot's data class record (``data_class``). Third-party
  compute admits data only when the snapshot is recorded ``public`` with license and
  source evidence. Nothing here changes that rule.
* **approved private code or config**: exact relative paths that a human decision
  actor approved for a remote run. The approval is recorded per file, with its sha256
  and size, in ``snapshot_code_approvals``. Approved code stays private and unlicensed;
  it is never relabelled public, and ``DataClass`` is unchanged.

A third-party target admits a snapshot only when every file is either approved code
or covered by a public data record (``admitted_code_paths``). Missing, malformed or
mismatching records fail closed.

What counts as code is deliberately narrow (``is_code_path``): source and config
suffixes only, at most ``MAX_CODE_FILE_BYTES`` per file and ``MAX_CODE_TOTAL_BYTES`` in
all. Plain ``.txt`` is data unless the name is a requirements or constraints file,
because text is the most common data format. Bytes that the org already holds in a
derived, migrated, unrecorded or undeclared snapshot cannot be approved as code, unless
the same bytes are already approved code elsewhere (empty files are exempt). That check
is a tripwire on sha256, not a guarantee; the human approval is the control.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .data_class import NOT_PUBLIC_SQL, PUBLIC, DataClassRefused, snapshot_data_class

PRIVATE_CODE_APPROVED = "private_code_approved"
NO_CODE = "none"
CONFIRMED_KEY = "structured_config_confirmed"
INVALID_CODE = "invalid"  # an approval row exists but does not match the snapshot
# ``input_data_class`` of a receipt whose every file is approved code: there is no data.
# ``None`` stays "not delivered".
ALL_APPROVED_CODE = "none_all_approved_code"

# Source and config suffixes. Data formats (.csv, .jsonl, .parquet, .bin, ...) and
# notebooks are excluded on purpose.
CODE_SUFFIXES = frozenset({".py", ".sh", ".json", ".toml", ".yaml", ".yml", ".cfg", ".ini"})
_TEXT_CONFIG = re.compile(r"(requirements|constraints)(-[a-z0-9]+)?\.txt\Z")
MAX_CODE_FILES = 256
MAX_CODE_FILE_BYTES = 1024 * 1024
MAX_CODE_TOTAL_BYTES = 8 * 1024 * 1024
# Self-asserted "desktop-owner" automation (owner_automation_unverified) is not accepted.
_HUMAN_CLASSES = ("human_session", "owner_automation_labelled")
# Structured formats can carry data as easily as configuration: approvable only when the
# approver lists the file in ``structured_config_confirmed``, and only when small.
STRUCTURED_SUFFIXES = frozenset({".json", ".yaml", ".yml", ".toml", ".cfg", ".ini"})
MAX_STRUCTURED_BYTES = 64 * 1024
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class CodeApprovalRefused(ValueError):
    """The code approval is malformed or does not fit the snapshot."""


def is_code_path(path: str) -> bool:
    """True for a path whose name has a code or config suffix (case-insensitive)."""
    name = path.rsplit("/", 1)[-1]
    if "." not in name.lstrip("."):
        return False
    suffix = "." + name.rsplit(".", 1)[-1].lower()
    return suffix in CODE_SUFFIXES or bool(_TEXT_CONFIG.fullmatch(name.lower()))


def is_structured_path(path: str) -> bool:
    """True for a structured, data-capable config suffix (case-insensitive)."""
    return "." + path.rsplit("/", 1)[-1].rsplit(".", 1)[-1].lower() in STRUCTURED_SUFFIXES


def _safe_relative(path: str) -> bool:
    return (
        0 < len(path) <= 4096
        and not path.startswith("/")
        and "\\" not in path
        and "\0" not in path
        and all(part not in {"", ".", ".."} for part in path.split("/"))
    )


class CodeApproval(BaseModel):
    """A human's approval of exact relative paths as private code or config."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    paths: list[str] = Field(min_length=1, max_length=MAX_CODE_FILES)
    structured_config_confirmed: list[str] = Field(default_factory=list, max_length=MAX_CODE_FILES)

    @field_validator("paths")
    @classmethod
    def _paths(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("Approved code paths must be unique.")
        for path in value:
            if not _safe_relative(path):
                raise ValueError("Approved code paths must be safe relative paths.")
            if not is_code_path(path):
                raise ValueError(
                    "Only code or config files may be approved as private code "
                    f"({', '.join(sorted(CODE_SUFFIXES))} or requirements/constraints .txt)."
                )
        return sorted(value)

    @field_validator("structured_config_confirmed")
    @classmethod
    def _confirmed(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("Confirmed structured config paths must be unique.")
        for path in value:
            if not is_structured_path(path):
                raise ValueError("Only structured config files need a confirmation.")
        return sorted(value)

    @model_validator(mode="after")
    def _structured_confirmed(self) -> CodeApproval:
        confirmed = set(self.structured_config_confirmed)
        if not confirmed <= set(self.paths):
            raise ValueError("A confirmed structured config path must also be an approved path.")
        for path in self.paths:
            if is_structured_path(path) and path not in confirmed:
                raise ValueError(
                    f"{path!r} is structured and can hold data; confirm it is configuration "
                    "with structured_config_confirmed."
                )
        return self


def parse_code_approval(value: Any) -> CodeApproval | None:
    if value is None or isinstance(value, CodeApproval):
        return value
    try:
        return CodeApproval.model_validate(value)
    except ValidationError as error:
        reason = error.errors()[0].get("msg", "invalid") if error.errors() else "invalid"
        raise CodeApprovalRefused(f"Invalid code approval: {reason}") from error


def approved_files(approval: CodeApproval, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The approved paths' manifest entries, after the membership and size checks."""
    by_path = {entry["relative_path"]: entry for entry in entries}
    files, total = [], 0
    for path in approval.paths:
        entry = by_path.get(path)
        if entry is None:
            raise CodeApprovalRefused(f"Approved code path {path!r} is not a file of the input.")
        if entry["size"] > MAX_CODE_FILE_BYTES:
            raise CodeApprovalRefused(
                f"Approved code file {path!r} exceeds {MAX_CODE_FILE_BYTES} bytes."
            )
        total += entry["size"]
        item = {"relative_path": path, "sha256": entry["sha256"], "size": entry["size"]}
        if path in approval.structured_config_confirmed:
            if entry["size"] > MAX_STRUCTURED_BYTES:
                raise CodeApprovalRefused(
                    f"Structured config file {path!r} exceeds {MAX_STRUCTURED_BYTES} bytes."
                )
            item[CONFIRMED_KEY] = True
        files.append(item)
    if total > MAX_CODE_TOTAL_BYTES:
        raise CodeApprovalRefused(f"Approved code exceeds {MAX_CODE_TOTAL_BYTES} bytes in total.")
    return files


def check_entry_items_approved(runtime_manifest: dict[str, Any], approved: Any) -> None:
    """With a code approval, an input-sourced entry must be one of the approved paths."""
    for item in runtime_manifest.get("fetches", []):
        if (
            isinstance(item, dict)
            and item.get("source") == "input"
            and item.get("kind") == "entry"
            and item.get("path") not in approved
        ):
            raise ValueError(
                f"the manifest's input entry {item.get('path')!r} is not approved private code"
            )


# ---- schema ------------------------------------------------------------------------------


def create_code_approval_schema(connection: sqlite3.Connection) -> None:
    """Idempotent: fresh schemas and the v62->v63 migration share this definition.

    Same immutability guards as ``snapshot_data_classes``: no update, no delete of a row
    whose snapshot exists, no second row for the same id.
    """
    connection.execute(
        f"""CREATE TABLE IF NOT EXISTS snapshot_code_approvals (
        snapshot_id TEXT PRIMARY KEY,
        files_json TEXT NOT NULL,
        actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        decision_class TEXT NOT NULL CHECK(decision_class IN {_HUMAN_CLASSES!r}),
        provenance_json TEXT NOT NULL,
        recorded_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS snapshot_code_approvals_no_update
        BEFORE UPDATE ON snapshot_code_approvals
        BEGIN SELECT RAISE(ABORT, 'snapshot code approval is immutable'); END"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS snapshot_code_approvals_no_delete
        BEFORE DELETE ON snapshot_code_approvals
        WHEN EXISTS (SELECT 1 FROM source_snapshots WHERE id = OLD.snapshot_id)
        BEGIN SELECT RAISE(ABORT, 'snapshot code approval is immutable'); END"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS snapshot_code_approvals_no_replace
        BEFORE INSERT ON snapshot_code_approvals
        WHEN EXISTS (SELECT 1 FROM snapshot_code_approvals
                     WHERE snapshot_id = NEW.snapshot_id)
        BEGIN SELECT RAISE(ABORT, 'snapshot code approval is immutable'); END"""
    )


# ---- recording ---------------------------------------------------------------------------


def _require_approver(live: dict[str, Any]) -> None:
    """A human decision actor only: never an agent, worker or bare root owner."""
    from .approval_trust import decision_class, require_decision_actor
    from .store import Forbidden

    if live.get("role") != "human":
        raise Forbidden("Only a human may approve private code for a remote run.")
    require_decision_actor(live, forbidden=Forbidden)
    if decision_class(live) not in _HUMAN_CLASSES:
        raise Forbidden("Only a human decision may approve private code for a remote run.")


def _approved_elsewhere(
    connection: sqlite3.Connection, org_id: str, snapshot_id: str, digests: set[str]
) -> set[str]:
    """Digests that are already validly approved code in another snapshot of the org."""
    found: set[str] = set()
    if not digests:
        return found
    # Only approvals that list one of these digests are candidates (not every approval
    # row in the org); each candidate is then fully validated.
    rows = connection.execute(
        """SELECT DISTINCT a.snapshot_id FROM snapshot_code_approvals a
        JOIN source_snapshots s ON s.id = a.snapshot_id JOIN projects p ON p.id = s.project_id
        JOIN json_each(CASE WHEN json_valid(a.files_json) THEN a.files_json ELSE '[]' END) f
        WHERE p.org_id = ? AND a.snapshot_id != ?
        AND json_extract(f.value, '$.sha256') IN (SELECT value FROM json_each(?))""",
        (org_id, snapshot_id, json.dumps(sorted(digests))),
    ).fetchall()
    for (other,) in rows:
        record = snapshot_code_approval(connection, other)
        if record is not None and record.get("valid") is True:
            found.update(f["sha256"] for f in record["files"] if f["sha256"] in digests)
    return found


def _refuse_laundered_code(
    connection: sqlite3.Connection, org_id: str, files: list[dict[str, Any]], snapshot_id: str
) -> None:
    """Approved code bytes must not equal a file the org holds as private data.

    Any other snapshot of the org that is not recorded public (derived, migrated,
    undeclared, unrecorded or any future private origin) blocks approval of a file with
    the same sha256, unless the same bytes are already validly approved code in another
    snapshot (the same code may be staged again). Empty files carry no content and are
    exempt.

    This is a tripwire, not a guarantee: a re-serialized file has other bytes and passes.
    The human approval is the control.
    """
    from .store import Conflict

    digests = sorted({f["sha256"] for f in files if f["size"] > 0})
    if not digests:
        return
    hits = connection.execute(
        f"""SELECT DISTINCT json_extract(e.value, '$.sha256')
        FROM source_snapshots s JOIN projects p ON p.id = s.project_id
        JOIN json_each(CASE WHEN json_valid(s.manifest_json) THEN s.manifest_json
                            ELSE '{{}}' END, '$.entries') e
        LEFT JOIN snapshot_data_classes d ON d.snapshot_id = s.id
        WHERE p.org_id = ? AND s.id != ? AND {NOT_PUBLIC_SQL}
        AND json_extract(e.value, '$.sha256') IN (SELECT value FROM json_each(?))""",
        (org_id, snapshot_id, json.dumps(digests)),
    ).fetchall()
    blocked = {sha for (sha,) in hits}
    if blocked:
        blocked -= _approved_elsewhere(connection, org_id, snapshot_id, blocked)
    if blocked:
        raise Conflict(
            "An approved code file has the same bytes as non-public staged data; "
            "it cannot be approved as code."
        )


def _row(connection: sqlite3.Connection, snapshot_id: str) -> sqlite3.Row | None:
    previous = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            "SELECT * FROM snapshot_code_approvals WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
    finally:
        connection.row_factory = previous


def _insert(connection, live, snapshot_id, files, now) -> None:
    from .approval_trust import decision_provenance

    provenance = decision_provenance(live)
    connection.execute(
        """INSERT INTO snapshot_code_approvals
        (snapshot_id, files_json, actor_principal_id, decision_class, provenance_json,
         recorded_at) VALUES (?, ?, ?, ?, ?, ?)""",
        (
            snapshot_id,
            json.dumps(files, sort_keys=True, separators=(",", ":")),
            live["id"],
            provenance["decision_class"],
            json.dumps(provenance, sort_keys=True),
            now,
        ),
    )


def _checked_files(approval: CodeApproval, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from .store import StoreError

    try:
        return approved_files(approval, entries)
    except CodeApprovalRefused as error:
        raise StoreError(str(error)) from None


def declare_pending_code(
    connection: sqlite3.Connection,
    live: dict[str, Any],
    upload_id: str,
    approval: CodeApproval | None,
    entries: list[dict[str, Any]],
    now: str,
) -> dict[str, Any] | None:
    """Record a chunked upload's begin-time code approval under its future snapshot id."""
    if approval is None:
        return None
    _require_approver(live)
    files = _checked_files(approval, entries)
    _refuse_laundered_code(connection, live["org_id"], files, upload_id)
    _insert(connection, live, upload_id, files, now)
    return _record(_row(connection, upload_id))


def discard_pending_code(connection: sqlite3.Connection, upload_id: str) -> None:
    """Drop the approval of an upload that will never become a snapshot."""
    connection.execute(
        """DELETE FROM snapshot_code_approvals WHERE snapshot_id = ? AND NOT EXISTS
        (SELECT 1 FROM source_snapshots WHERE id = ?)""",
        (upload_id, upload_id),
    )


def record_code_approval(
    connection: sqlite3.Connection,
    live: dict[str, Any],
    snapshot_id: str,
    approval: CodeApproval | None,
    *,
    derived: bool = False,
    now: str,
) -> dict[str, Any] | None:
    """Record the code approval of a snapshot just inserted, in the caller's transaction.

    A begin-time approval is kept; a different approval at completion is refused. A
    snapshot built from run outputs can never carry one. The anti-laundering rule is
    re-checked here, as the public declaration's is.
    """
    from .store import Conflict, Forbidden

    if derived and approval is not None:
        raise Forbidden("Snapshots built from run outputs or artifacts cannot carry code.")
    existing = _row(connection, snapshot_id)
    if existing is not None:
        if derived:
            raise Conflict("A derived snapshot cannot carry an earlier code approval.")
        stored = json.loads(existing["files_json"])
        if approval is not None and (
            [f["relative_path"] for f in stored] != list(approval.paths)
            or [f["relative_path"] for f in stored if f.get(CONFIRMED_KEY)]
            != list(approval.structured_config_confirmed)
        ):
            raise Conflict("Code approval differs from the one made at upload begin.")
    elif approval is not None:
        _require_approver(live)
        _insert(
            connection,
            live,
            snapshot_id,
            _checked_files(approval, _entries(connection, snapshot_id)),
            now,
        )
    record = snapshot_code_approval(connection, snapshot_id)
    if existing is not None or approval is not None:
        if record is None or record.get("valid") is not True:
            raise Conflict("Code approval does not match the snapshot's files.")
        _refuse_laundered_code(connection, live["org_id"], record["files"], snapshot_id)
    return record


# ---- reading -----------------------------------------------------------------------------


def _entries(connection: sqlite3.Connection, snapshot_id: str) -> list[dict[str, Any]]:
    row = connection.execute(
        "SELECT manifest_json FROM source_snapshots WHERE id = ?", (snapshot_id,)
    ).fetchone()
    if row is None:
        raise DataClassRefused("the input snapshot is not recorded")
    try:
        entries = json.loads(row[0])["entries"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("manifest entries") from error
    if not isinstance(entries, list) or not all(
        isinstance(e, dict) and isinstance(e.get("relative_path"), str) for e in entries
    ):
        raise ValueError("manifest entries")
    return entries


def _record(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "code": PRIVATE_CODE_APPROVED,
        "files": json.loads(row["files_json"]),
        "actor_principal_id": row["actor_principal_id"],
        "decision_class": row["decision_class"],
        "recorded_at": row["recorded_at"],
    }


def _valid(files: Any, entries: list[dict[str, Any]], row: sqlite3.Row) -> bool:
    """Re-check a stored approval against the snapshot's manifest (fail closed)."""
    if row["decision_class"] not in _HUMAN_CLASSES or not row["actor_principal_id"]:
        return False
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_CODE_FILES:
        return False
    by_path = {e.get("relative_path"): e for e in entries if isinstance(e, dict)}
    seen, total = set(), 0
    for item in files:
        base = {"relative_path", "sha256", "size"}
        if not isinstance(item, dict) or set(item) not in (base, base | {CONFIRMED_KEY}):
            return False
        if item.get(CONFIRMED_KEY, True) is not True:
            return False
        path, sha256, size = item["relative_path"], item["sha256"], item["size"]
        entry = by_path.get(path)
        if (
            not isinstance(path, str)
            or path in seen
            or not _safe_relative(path)
            or not is_code_path(path)
            or not isinstance(sha256, str)
            or not _HEX64.fullmatch(sha256)
            or type(size) is not int
            or not 0 <= size <= MAX_CODE_FILE_BYTES
            or (
                isinstance(path, str)
                and is_structured_path(path)
                and (CONFIRMED_KEY not in item or size > MAX_STRUCTURED_BYTES)
            )
            or (CONFIRMED_KEY in item and not is_structured_path(path))
            or entry is None
            or entry.get("sha256") != sha256
            or entry.get("size") != size
        ):
            return False
        seen.add(path)
        total += size
    return total <= MAX_CODE_TOTAL_BYTES


def snapshot_code_approval(
    connection: sqlite3.Connection, snapshot_id: str
) -> dict[str, Any] | None:
    """The recorded code approval of an existing snapshot, or ``None``.

    ``valid`` is False when the stored row does not match the snapshot's files (for
    example a hand-edited database); enforcement then refuses the snapshot.
    """
    row = connection.execute(
        "SELECT 1 FROM source_snapshots WHERE id = ?", (snapshot_id,)
    ).fetchone()
    approval = _row(connection, snapshot_id) if row is not None else None
    if approval is None:
        return None
    try:
        record = _record(approval)
        record["valid"] = _valid(record["files"], _entries(connection, snapshot_id), approval)
    except (ValueError, TypeError, KeyError, RecursionError):
        record = {"code": PRIVATE_CODE_APPROVED, "files": [], "valid": False}
    return record


# ---- enforcement -------------------------------------------------------------------------


def admitted_code_paths(connection: sqlite3.Connection, snapshot_id: str) -> frozenset[str]:
    """Third-party admission: every file is approved code or covered by a public record.

    Returns the approved code paths (empty without an approval). Raises
    ``DataClassRefused`` otherwise, failing closed on unknown or malformed records.
    """
    approval = snapshot_code_approval(connection, snapshot_id)
    public = snapshot_data_class(connection, snapshot_id)["data_class"] == PUBLIC
    if approval is None:
        if not public:
            raise DataClassRefused(
                "third-party compute admits only a staged snapshot recorded as public "
                "with license and source evidence"
            )
        return frozenset()
    if approval.get("valid") is not True:
        raise DataClassRefused(
            "the snapshot's private code approval does not match its files; third-party "
            "compute admits only approved code and data recorded public"
        )
    approved = frozenset(f["relative_path"] for f in approval["files"])
    try:
        entries = _entries(connection, snapshot_id)
        has_data = any(e["relative_path"] not in approved for e in entries)
    except (ValueError, TypeError, KeyError, RecursionError):
        raise DataClassRefused("the input snapshot manifest is unreadable") from None
    if has_data and not public:
        raise DataClassRefused(
            "third-party compute admits only approved private code plus data recorded "
            "public with license and source evidence"
        )
    return approved


def input_classes(connection: sqlite3.Connection, snapshot_id: str | None) -> dict[str, Any]:
    """Receipt fields: the data class of the non-code files and the approved code.

    ``input_data_class`` is ``none_all_approved_code`` when every file is approved code
    (there is no data); ``None`` is reserved for "no input delivered", which the
    receipts set themselves. ``input_code`` is ``private_code_approved`` with each
    approved file's sha256, or ``none``; ``invalid`` when an approval row exists but does
    not match the snapshot.
    """
    from .data_class import PRIVATE_DERIVED

    if snapshot_id is None:
        return {"input_data_class": PRIVATE_DERIVED, "input_code": NO_CODE, "input_code_files": []}
    data_class = snapshot_data_class(connection, snapshot_id)["data_class"]
    approval = snapshot_code_approval(connection, snapshot_id)
    if approval is None:
        return {"input_data_class": data_class, "input_code": NO_CODE, "input_code_files": []}
    if approval.get("valid") is not True:
        # Tampered or mismatching: never read as "no code".
        return {"input_data_class": data_class, "input_code": INVALID_CODE, "input_code_files": []}
    approved = {f["relative_path"] for f in approval["files"]}
    has_data = any(e["relative_path"] not in approved for e in _entries(connection, snapshot_id))
    return {
        "input_data_class": data_class if has_data else ALL_APPROVED_CODE,
        "input_code": PRIVATE_CODE_APPROVED,
        "input_code_files": [
            {"relative_path": f["relative_path"], "sha256": f["sha256"]} for f in approval["files"]
        ],
    }


def grant_input_classes(connection: sqlite3.Connection, grant_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT snapshot_id FROM execution_grants WHERE id = ?", (grant_id,)
    ).fetchone()
    return input_classes(connection, None if row is None else row[0])


def operation_input_classes(connection: sqlite3.Connection, operation_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT grant_id FROM external_operations WHERE operation_id = ?", (operation_id,)
    ).fetchone()
    if row is None:
        return input_classes(connection, None)
    return grant_input_classes(connection, row[0])


def refuse_local_transfer(connection: sqlite3.Connection, snapshot_id: str) -> None:
    """A public release label never authorizes local transfer of approved private code.

    Approved code is delivered only through the provider input path, which is gated by
    ``admitted_code_paths`` and the provider trust setting. Any recorded approval (valid
    or not) refuses, including code-only ``private_derived`` snapshots: local delivery
    checks the release and lease but neither per-file admission nor provider trust, so it
    is not a path that may carry approved code (ADR 0039, local transfer).
    """
    from .store import Forbidden

    if _row(connection, snapshot_id) is not None:
        raise Forbidden(
            "A snapshot with approved private code is delivered only to approved remote "
            "routes, not through local worker transfer."
        )
