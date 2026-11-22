"""Stable, owner-enrolled executor identity for one compute provider instance.

The provider trust setting (ADR 0040 section 4, docs/provider-trust.md) lists worker
principals per provider. A setup that mints a fresh worker principal for every route can
therefore never receive a private class. An executor enrollment is the one long-lived
worker principal of a provider instance: its own project, a worker credential and a
worker registration, kept in a private 0700 directory. Route setups reuse it (for Salad,
``salad_live_setup.py --executor-dir``) instead of minting a principal.

Lifetime is bounded twice. Control issues and renews worker credentials at most 24 hours
ahead (owner only, same principal and token), and this module never renews past
``not_after``, at most seven days after enrollment. Rotation is a new enrollment, a
trust-file edit and a human reload, then revocation of the old enrollment. Nothing here
prints or returns the token; it lives only in the directory's 0600 ``worker-token``.

By default an enrollment creates a project of its own. ``existing_project_id`` enrolls the
executor into a project that already exists instead (ADR 0056), so one campaign, which has
exactly one project, can mix stages on a CPU worker and a GPU executor whose data,
snapshots and receipts live together. Control authorizes that exactly as it does any
credential in a project: the local owner only, and an unknown or foreign project is
refused before a credential is issued.

The module is provider-agnostic. ``http`` is the setup's journaled owner client:
``http(step, method, path, body=None) -> dict``, raising on any refusal.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .store import MAX_WORKER_CREDENTIAL_LIFETIME as MAX_LIFETIME  # one bound, control's

SCHEMA = "tessera.executor-enrollment.v1"
RECORD_NAME = "executor.json"
TOKEN_NAME = "worker-token"
# Control refuses credential expiry beyond now + 24 h; stay inside it across clock skew.
RENEW_STEP = timedelta(hours=24) - timedelta(minutes=5)
_PROVIDER = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_ID = re.compile(r"[A-Za-z0-9_:-]{1,200}\Z")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PROJECT_ID = re.compile(r"project_[0-9a-f]{32}\Z")
_FIELDS = (
    "schema",
    "provider",
    "project_id",
    "worker_principal_id",
    "credential_id",
    "registration_id",
    "approved_build_identifier",
    "approved_build_digest",
    "trust_class",
    "region",
    "enrolled_at",
    "not_after",
    "credential_expires_at",
    "revoked_at",
)
# Written by an enrollment into an existing project; a record without it is a new project.
_OPTIONAL_FIELDS = ("project_existing",)

Http = Callable[..., dict[str, Any]]


class ExecutorError(ValueError):
    """The enrollment is missing, unsafe, expired, revoked or for another provider."""


@dataclass(frozen=True)
class Executor:
    schema: str
    provider: str
    project_id: str
    worker_principal_id: str
    credential_id: str
    registration_id: str
    approved_build_identifier: str
    approved_build_digest: str
    trust_class: str
    region: str
    enrolled_at: str
    not_after: str
    credential_expires_at: str
    revoked_at: str | None = None
    project_existing: bool = False

    def public(self) -> dict[str, Any]:
        """Non-secret summary (identifiers and times only)."""
        return asdict(self)


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ExecutorError("enrollment times require an offset")
    return parsed.astimezone(UTC)


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(UTC)


def _directory(path: Path) -> int:
    """Open an existing owner-only 0700 directory without following symlinks."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise ExecutorError("executor directory is missing or not a directory") from error
    info = os.fstat(fd)
    if stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.geteuid():
        os.close(fd)
        raise ExecutorError("executor directory must be owner-only 0700")
    return fd


def _private_file(directory: int, name: str, cap: int) -> bytes:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    except OSError as error:
        raise ExecutorError(f"executor {name} is missing") from error
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or not 0 < info.st_size <= cap
        ):
            raise ExecutorError(f"executor {name} must be a private 0600 file")
        return os.read(fd, cap + 1)
    finally:
        os.close(fd)


def _write_record(directory: int, executor: Executor) -> None:
    """Replace executor.json atomically (0600, same directory, fsynced)."""
    temporary = RECORD_NAME + ".new"
    try:
        os.unlink(temporary, dir_fd=directory)
    except FileNotFoundError:
        pass
    fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
    )
    with os.fdopen(fd, "wb") as stream:
        stream.write(json.dumps(executor.public(), sort_keys=True, indent=2).encode() + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, RECORD_NAME, src_dir_fd=directory, dst_dir_fd=directory)
    os.fsync(directory)


def _validate(record: Any) -> Executor:
    if not isinstance(record, dict) or not (
        set(_FIELDS) <= set(record) <= set(_FIELDS) | set(_OPTIONAL_FIELDS)
    ):
        raise ExecutorError("executor record has unexpected fields")
    if not isinstance(record.get("project_existing", False), bool):
        raise ExecutorError("executor project_existing is invalid")
    if record["schema"] != SCHEMA:
        raise ExecutorError(f"executor record schema must be {SCHEMA}")
    if not isinstance(record["provider"], str) or not _PROVIDER.match(record["provider"]):
        raise ExecutorError("executor provider id is invalid")
    for key in ("project_id", "worker_principal_id", "credential_id", "registration_id"):
        if not isinstance(record[key], str) or not _ID.match(record[key]):
            raise ExecutorError(f"executor {key} is invalid")
    for key in ("approved_build_identifier", "trust_class", "region"):
        if not isinstance(record[key], str) or not _LABEL.match(record[key]):
            raise ExecutorError(f"executor {key} is invalid")
    if not isinstance(record["approved_build_digest"], str) or not _DIGEST.match(
        record["approved_build_digest"]
    ):
        raise ExecutorError("executor approved_build_digest is invalid")
    for key in ("enrolled_at", "not_after", "credential_expires_at"):
        if not isinstance(record[key], str):
            raise ExecutorError(f"executor {key} is invalid")
        _parse(record[key])
    if record["revoked_at"] is not None:
        if not isinstance(record["revoked_at"], str):
            raise ExecutorError("executor revoked_at is invalid")
        _parse(record["revoked_at"])
    enrolled, not_after = _parse(record["enrolled_at"]), _parse(record["not_after"])
    if not enrolled < not_after <= enrolled + MAX_LIFETIME:
        raise ExecutorError("executor lifetime exceeds the seven-day bound")
    if _parse(record["credential_expires_at"]) > not_after:
        raise ExecutorError("executor credential outlives the enrollment")
    return Executor(**record)


def read(path: Path) -> Executor:
    """Read and validate an enrollment record (no liveness check)."""
    directory = _directory(path)
    try:
        record = json.loads(_private_file(directory, RECORD_NAME, 16384))
        return _validate(record)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ExecutorError("executor record is not valid JSON") from error
    finally:
        os.close(directory)


def load(
    path: Path, *, provider: str, needed_until: datetime, now: datetime | None = None
) -> tuple[Executor, str]:
    """Return a live enrollment for ``provider`` and its worker token.

    Refuses a revoked or expired enrollment, another provider's enrollment, and one whose
    credential would lapse before ``needed_until`` (the route's authority window): renew
    or rotate it first. The token is returned for in-process use only.
    """
    executor = read(path)
    current = _now(now)
    if executor.provider != provider:
        raise ExecutorError(f"executor is enrolled for {executor.provider}, not {provider}")
    if executor.revoked_at is not None:
        raise ExecutorError("executor enrollment is revoked; enroll a new executor")
    if current >= _parse(executor.not_after):
        raise ExecutorError("executor enrollment passed its seven-day bound; rotate it")
    if _parse(executor.credential_expires_at) < needed_until.astimezone(UTC):
        raise ExecutorError(
            "executor credential expires before this route's authority window ends; renew it"
        )
    directory = _directory(path)
    try:
        token = _private_file(directory, TOKEN_NAME, 512).decode("ascii", "strict").strip()
    except UnicodeDecodeError as error:
        raise ExecutorError("executor worker-token is not ASCII") from error
    finally:
        os.close(directory)
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,256}", token):
        raise ExecutorError("executor worker-token is malformed")
    return executor, token


def enroll(
    http: Http,
    path: Path,
    token: str,
    *,
    provider: str,
    project_name: str,
    approved_build_identifier: str,
    approved_build_digest: str,
    trust_class: str,
    region: str,
    lifetime: timedelta = MAX_LIFETIME,
    now: datetime | None = None,
    existing_project_id: str | None = None,
) -> Executor:
    """Create the project, worker credential and registration, then write the record.

    With ``existing_project_id`` no project is created: the project is looked up first (an
    unknown or unavailable one is refused before anything is issued) and the credential and
    registration are made in it.

    ``path`` must already be an owner-only directory holding the 0600 ``worker-token``
    (written before any request so a lost reply can be reconciled from the journal).
    """
    if not _PROVIDER.match(provider):
        raise ExecutorError("provider id is invalid")
    if not timedelta(hours=1) <= lifetime <= MAX_LIFETIME:
        raise ExecutorError("executor lifetime must be between one hour and seven days")
    current = _now(now)
    not_after = current + lifetime
    expires = min(current + RENEW_STEP, not_after)
    directory = _directory(path)
    try:
        try:
            stored = _private_file(directory, TOKEN_NAME, 512).decode("ascii").strip()
        except UnicodeDecodeError as error:
            raise ExecutorError("worker-token file is not ASCII") from error
        if stored != token:
            raise ExecutorError("worker-token file does not hold the enrollment token")
        if existing_project_id is None:
            project = http("executor-project", "POST", "/projects", {"name": project_name})
            project_id = project["id"]
        else:
            project_id = _existing_project(http, existing_project_id)
        credential = http(
            "executor-credential",
            "POST",
            "/credentials",
            dict(
                token=token,
                kind="worker",
                audience="worker",
                project_id=project_id,
                expires_at=_utc(expires),
            ),
        )
        principal = credential["principal_id"]
        registration = http(
            "executor-registration",
            "POST",
            f"/projects/{project_id}/workers",
            dict(
                worker_principal_id=principal,
                approved_build_identifier=approved_build_identifier,
                approved_build_digest=approved_build_digest,
                trust_class=trust_class,
                region=region,
            ),
        )
        if registration.get("worker_principal_id") != principal:
            raise ExecutorError("worker registration identity mismatch")
        executor = _validate(
            dict(
                schema=SCHEMA,
                provider=provider,
                project_id=project_id,
                worker_principal_id=principal,
                credential_id=credential["credential_id"],
                registration_id=registration["id"],
                approved_build_identifier=approved_build_identifier,
                approved_build_digest=approved_build_digest,
                trust_class=trust_class,
                region=region,
                enrolled_at=_utc(current),
                not_after=_utc(not_after),
                credential_expires_at=_utc(expires),
                revoked_at=None,
                **({"project_existing": True} if existing_project_id is not None else {}),
            )
        )
        _write_record(directory, executor)
        return executor
    finally:
        os.close(directory)


def _existing_project(http: Http, project_id: str) -> str:
    """Confirm, through the owner's own authority, that the project exists and is usable."""
    if not _PROJECT_ID.match(project_id):
        raise ExecutorError("existing project id is invalid")
    try:
        project = http("executor-project-existing", "GET", f"/projects/{project_id}")
    except Exception:  # the client names the refused step; control's detail stays out
        raise ExecutorError(
            "the existing project was not found or is not available to this owner"
        ) from None
    if project.get("id") != project_id:
        raise ExecutorError("control returned a different project than requested")
    return project_id


def same_enrollment(
    executor: Executor,
    *,
    provider: str,
    project_id: str,
    region: str | None = None,
    lifetime: timedelta | None = None,
) -> bool:
    """Whether a live record already enrolls ``provider`` into ``project_id`` (idempotency).

    ``region`` and ``lifetime`` (``not_after`` minus ``enrolled_at``, exactly the lifetime the
    enrollment was made with) are compared when given, so a rerun that asks for a different
    region or number of days is refused instead of silently returning the old record.
    """
    return (
        executor.revoked_at is None
        and executor.provider == provider
        and executor.project_id == project_id
        and executor.project_existing
        and (region is None or executor.region == region)
        and (
            lifetime is None
            or _parse(executor.not_after) - _parse(executor.enrolled_at) == lifetime
        )
    )


def renew(http: Http, path: Path, *, now: datetime | None = None) -> Executor:
    """Extend the same principal's credential by up to 24 h, never past ``not_after``."""
    executor = read(path)
    current = _now(now)
    if executor.revoked_at is not None:
        raise ExecutorError("executor enrollment is revoked; enroll a new executor")
    not_after = _parse(executor.not_after)
    if current >= not_after:
        raise ExecutorError("executor enrollment passed its seven-day bound; rotate it")
    expires = min(current + RENEW_STEP, not_after)
    if expires <= _parse(executor.credential_expires_at):
        raise ExecutorError("executor credential already reaches its seven-day bound; rotate it")
    response = http(
        "executor-renew",
        "POST",
        f"/credentials/{executor.credential_id}/renew",
        {"expires_at": _utc(expires)},
    )
    if response.get("principal_id") != executor.worker_principal_id:
        raise ExecutorError("renewal response does not name the enrolled worker principal")
    renewed = Executor(**{**executor.public(), "credential_expires_at": _utc(expires)})
    directory = _directory(path)
    try:
        _write_record(directory, renewed)
    finally:
        os.close(directory)
    return renewed


def revoke(http: Http, path: Path, *, now: datetime | None = None) -> Executor:
    """Revoke the worker credential at control, then mark the record revoked."""
    executor = read(path)
    if executor.revoked_at is not None:
        return executor
    http("executor-revoke", "POST", f"/credentials/{executor.credential_id}/revoke")
    revoked = Executor(**{**executor.public(), "revoked_at": _utc(_now(now))})
    directory = _directory(path)
    try:
        _write_record(directory, revoked)
    finally:
        os.close(directory)
    return revoked
