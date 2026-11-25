"""The reasoning admission marker and its readiness check (ADR 0163).

``reasoning.admitted`` stays what the systemd unit checks (``ConditionPathExists``): the unit
only asks whether the file exists. What the file says is new. ``tessera reasoning admit`` has a
person's session record the admission on the control plane's receipts (who admitted, which
project, provider and model, the caps and the expiry; ``tessera_control.reasoning_admissions``)
and then writes the marker holding only a non-secret reference to that record: its id, digest,
project and expiry.

The readiness ``admission`` check reads the marker and, read-only, the record behind it:

* no marker: ``warn`` ``not_admitted``. The unit is skipped until a person admits it; this is not
  a failure of the install (the installer never admits, ADR 0119).
* a bare marker (empty or not a recorded reference, the file an operator creates by hand):
  ``warn`` ``unrecorded``. It still satisfies the unit's condition, exactly as before, but the
  check never reports it as a recorded, delegated admission.
* a recorded marker whose record is missing, altered, revoked, expired, for another project,
  provider or model, below the profile's limits, or out of calls: ``fail``.
* a recorded marker whose record could not be read (no control state directory given, or the
  marker itself is unreadable to the checking user): ``unchecked``, never ``ok``.
* a recorded marker whose record is active and covers the profile: ``ok``.

Nothing here writes anything, reads a secret or prints a profile value other than ids and caps.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MARKER_KIND = "tessera.reasoning_admission"
MARKER_VERSION = 1
MARKER_FIELDS = frozenset(
    {"kind", "version", "admission_id", "record_digest", "project_id", "expires_at"}
)
MAX_MARKER_BYTES = 4096
CHECK = "admission"
ADMIT = (
    "a person runs `tessera reasoning admit` with their own session (it records the admission "
    "on receipts and writes the marker that references it)"
)
_FIXES = {
    "not_admitted": ADMIT + "; until then the unit is skipped",
    "unrecorded": (
        "the marker is a bare file with no admission record behind it; the unit still starts, "
        "but nothing records who admitted it or under which caps: " + ADMIT + " --replace"
    ),
    "marker_unsafe": "remove the marker and " + ADMIT,
    "marker_malformed": "remove the marker and " + ADMIT,
    "marker_unreadable": "run the check as root (the marker is root-owned 0600)",
    "record_unchecked": "pass --control-state-dir so the record can be read",
    "admission_record_missing": ADMIT,
    "admission_record_invalid": "the admission record does not match its digest; " + ADMIT,
    "admission_digest_mismatch": "the marker does not reference this record; " + ADMIT,
    "admission_revoked": "the admission was revoked; " + ADMIT + " again if it should run",
    "admission_expired": "the admission expired; " + ADMIT + " again if it should run",
    "admission_scope_mismatch": (
        "admit the service for the profile's project, provider and model, or fix the profile"
    ),
    "admission_caps_exceeded": (
        "lower the profile's limits to the admitted caps, or " + ADMIT + " with higher caps"
    ),
    "admission_calls_exhausted": "the admitted number of calls is used up; " + ADMIT,
    "control_database_unreadable": (
        "run the check as a user that can read the control database, or check the admission "
        "through GET /projects/{id}/reasoning-admissions/{admission_id}"
    ),
}


def _check(status: str, code: str, detail: str, *, fix: bool = True) -> dict[str, Any]:
    return {
        "name": CHECK,
        "status": status,
        "detail": f"{code}: {detail}",
        "fix": _FIXES.get(code) if fix else None,
    }


# -- the marker ------------------------------------------------------------------------------------


def marker_document(admission: dict[str, Any]) -> dict[str, Any]:
    """The marker's fields, from a ``POST /projects/{id}/reasoning-admissions`` answer."""
    return {
        "kind": MARKER_KIND,
        "version": MARKER_VERSION,
        "admission_id": admission["id"],
        "record_digest": admission["record_digest"],
        "project_id": admission["project_id"],
        "expires_at": admission["expires_at"],
    }


def marker_bytes(admission: dict[str, Any]) -> bytes:
    return (json.dumps(marker_document(admission), sort_keys=True, indent=2) + "\n").encode()


class MarkerMalformed(ValueError):
    """The marker claims to be a recorded reference but is not a valid one."""


def parse_marker(content: bytes) -> dict[str, Any] | None:
    """The reference in a marker, or None for a bare marker (empty, or not a JSON object, the file
    an operator creates by hand). A JSON object that is not exactly a valid v1 reference raises
    ``MarkerMalformed``: a damaged recorded marker is never silently read as a bare one."""
    text = content.strip()
    if not text.startswith(b"{"):
        return None
    try:
        document = json.loads(text)
    except (ValueError, UnicodeDecodeError):
        raise MarkerMalformed("the marker is not valid JSON") from None
    if not isinstance(document, dict) or set(document) != MARKER_FIELDS:
        raise MarkerMalformed("the marker does not have exactly the reference fields")
    if document["kind"] != MARKER_KIND or document["version"] != MARKER_VERSION:
        raise MarkerMalformed("the marker is not a version 1 reasoning admission reference")
    for name in ("admission_id", "record_digest", "project_id", "expires_at"):
        value = document[name]
        if not isinstance(value, str) or not 1 <= len(value) <= 200 or not value.isprintable():
            raise MarkerMalformed(f"the marker's {name} is not a plain string")
    if not all(c in "0123456789abcdef" for c in document["record_digest"]) or (
        len(document["record_digest"]) != 64
    ):
        raise MarkerMalformed("the marker's record_digest is not a sha256 digest")
    try:
        _parse_time(document["expires_at"])
    except ValueError:
        raise MarkerMalformed("the marker's expires_at is not a time with an offset") from None
    return document


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("no offset")
    return parsed.astimezone(UTC)


def read_marker(path: Path) -> tuple[str, bytes | None]:
    """(``absent`` | ``unsafe`` | ``unreadable`` | ``read``, content). Never follows a symlink;
    a marker someone other than its owner may write is ``unsafe``."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "absent", None
    except OSError:
        return "unreadable", None
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
        return "unsafe", None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return "unreadable", None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_mode & 0o022:
            return "unsafe", None
        if opened.st_size > MAX_MARKER_BYTES:
            return "unsafe", None
        return "read", os.read(fd, MAX_MARKER_BYTES + 1)
    finally:
        os.close(fd)


# -- the readiness check ---------------------------------------------------------------------------


def _profile_scope(profile: dict) -> tuple[str | None, str | None]:
    model = profile.get("model")
    if not isinstance(model, dict):
        return None, None
    provider, model_id = model.get("providerID"), model.get("id")
    return (
        provider if isinstance(provider, str) else None,
        model_id if isinstance(model_id, str) else None,
    )


def _profile_exceeds(profile: dict, caps: dict) -> list[str]:
    limits = profile.get("limits")
    if not isinstance(limits, dict):
        return ["limits"]
    over = []
    for name in ("max_cost_cents_per_call", "daily_cost_cap_cents", "daily_token_cap"):
        value, cap = limits.get(name), caps.get(name)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not isinstance(cap, int)
            or value > cap
        ):
            over.append(name)
    return over


def _calls_since(connection, admission: dict, provider: str, model: str) -> int:
    record = admission["record"]
    created = _parse_time(record["created_at"])
    rows = connection.execute(
        """SELECT created_at FROM model_spend_calls WHERE org_id=? AND project_id=?
        AND provider_id=? AND model IN (?, ?) AND status <> 'released'""",
        (admission["org_id"], admission["project_id"], provider, model, f"{provider}/{model}"),
    ).fetchall()
    return sum(1 for (stamp,) in rows if _parse_time(stamp) >= created)


def _open_read_only(control_state_dir: Path):
    database = Path(control_state_dir) / "tessera.sqlite3"
    try:
        os.stat(database)
    except OSError:
        return None
    try:
        return sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def admission_check(
    admitted_path: Path,
    profile: dict | None,
    control_state_dir: Path | None,
    project_id: str | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The readiness ``admission`` check (see the module docstring for every outcome). Statuses:
    ``ok``, ``warn`` (not admitted yet, or admitted by a bare file: the unit's own condition
    decides, this check claims nothing), ``unchecked`` and ``fail``."""
    now = now or datetime.now(UTC)
    state, content = read_marker(Path(admitted_path))
    if state == "absent":
        return _check("warn", "not_admitted", "there is no admission marker")
    if state == "unsafe":
        return _check(
            "fail",
            "marker_unsafe",
            "the marker is not a regular file, is group/world writable or is too large",
        )
    if state == "unreadable":
        return _check(
            "unchecked", "marker_unreadable", "the marker exists but this user cannot read it"
        )
    assert content is not None
    try:
        marker = parse_marker(content)
    except MarkerMalformed as error:
        return _check("fail", "marker_malformed", str(error))
    if marker is None:
        return _check(
            "warn",
            "unrecorded",
            "the marker exists (the unit can start) but references no admission record",
        )
    ident = marker["admission_id"]
    if now >= _parse_time(marker["expires_at"]):
        return _check("fail", "admission_expired", f"admission {ident} expired")
    if profile is None:
        return _check("unchecked", "record_unchecked", "the profile is not valid", fix=False)
    if project_id is not None and marker["project_id"] != project_id:
        return _check(
            "fail",
            "admission_scope_mismatch",
            f"admission {ident} is for another project than the profile's",
        )
    if control_state_dir is None:
        return _check(
            "unchecked",
            "record_unchecked",
            f"admission {ident} is referenced but its record was not read (no "
            "--control-state-dir); not a pass",
        )
    connection = _open_read_only(Path(control_state_dir))
    if connection is None:
        return _check("fail", "control_database_unreadable", "the control database cannot be read")
    try:
        try:
            return _against_record(connection, marker, profile, now)
        except sqlite3.Error:
            return _check(
                "fail", "control_database_unreadable", "the admission record could not be read"
            )
        except (KeyError, TypeError, ValueError, AttributeError):  # fail closed, never crash
            return _check(
                "fail", "admission_record_invalid", "the admission record cannot be compared"
            )
    finally:
        connection.close()


def _against_record(connection, marker: dict, profile: dict, now: datetime) -> dict[str, Any]:
    from tessera_control.reasoning_admissions import admission_state, load_admission

    ident = marker["admission_id"]
    admission = load_admission(connection, marker["project_id"], ident)
    if admission is None:
        return _check("fail", "admission_record_missing", f"no admission record has the id {ident}")
    if not admission["integrity"]:
        return _check(
            "fail", "admission_record_invalid", f"admission {ident}'s record fails its digest"
        )
    record = admission["record"]
    if admission["record_digest"] != marker["record_digest"] or (
        record["expires_at"] != marker["expires_at"]
    ):
        return _check(
            "fail",
            "admission_digest_mismatch",
            f"the marker's digest or expiry is not admission {ident}'s",
        )
    state = admission_state(admission, now)
    if state == "revoked":
        return _check("fail", "admission_revoked", f"admission {ident} was revoked")
    if state == "expired":
        return _check("fail", "admission_expired", f"admission {ident} expired")
    provider, model = _profile_scope(profile)
    if provider != record["provider_id"] or model != record["model"]:
        return _check(
            "fail",
            "admission_scope_mismatch",
            f"admission {ident} is for another provider or model than the profile's",
        )
    caps = record["caps"]
    over = _profile_exceeds(profile, caps)
    if over:
        return _check(
            "fail",
            "admission_caps_exceeded",
            f"the profile's limits exceed admission {ident}'s caps on: " + ", ".join(over),
        )
    if caps.get("max_calls") is not None:
        used = _calls_since(connection, admission, provider, model)
        if used >= caps["max_calls"]:
            return _check(
                "fail",
                "admission_calls_exhausted",
                f"{used} of admission {ident}'s {caps['max_calls']} calls are recorded",
            )
    return {
        "name": CHECK,
        "status": "ok",
        "detail": (
            f"admission {ident} recorded by principal {record['admitted_by_principal_id']} "
            f"({record['decision_class']}) is active until {record['expires_at']} and covers "
            "the profile's limits"
        ),
        "fix": None,
    }


def startup_refusal(content: bytes | None, *, now: datetime | None = None) -> str | None:
    """For the supervisor at start-up, offline (it cannot read the control database): a reason
    code when the admission marker's own reference refuses the start, else None. A bare marker,
    or no marker content at all, is None: the unit's condition already decided, as before. A
    recorded marker that is malformed or past its expiry refuses. Revocation needs the record
    and is the readiness check's (or a control read's) to see."""
    if content is None:
        return None
    try:
        marker = parse_marker(content)
    except MarkerMalformed:
        return "admission_marker_malformed"
    if marker is None:
        return None
    if (now or datetime.now(UTC)) >= _parse_time(marker["expires_at"]):
        return "admission_expired"
    return None


__all__ = [
    "CHECK",
    "MARKER_KIND",
    "MarkerMalformed",
    "admission_check",
    "marker_bytes",
    "marker_document",
    "parse_marker",
    "read_marker",
    "startup_refusal",
]
