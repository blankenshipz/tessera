"""Deploy-check state and provider-file checks (ADR 0098).

Pure checks that return ``deploy_check.Finding`` objects. They open the control database
read-only, never read or print a secret, and never contact a provider. The one writer,
``apply_migrate``, runs only after the operator confirms a backup exists.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from collections import Counter
from pathlib import Path
from typing import Any

from . import provider_registration as registration
from .deploy_check import Finding, Fix, require_writable_directory, write_receipt
from .migrations import LATEST_SCHEMA_VERSION
from .recovery import MAX_CONFIGURABLE_BACKUP_BYTES, backup_byte_limit
from .store import DATABASE_NAME, StoreError, migrate_state

MAX_WALK_ENTRIES = 100000
MAX_CONFIG_BYTES = 65536
OBJECTS_DIRECTORY = "project-artifacts"  # what a backup copies into its ``objects`` directory
_GIB = 1 << 30


# -- schema ----------------------------------------------------------------------------------


def _read_schema_version(state_dir: Path) -> int:
    """The stored schema version, read through a read-only connection. Raises ValueError."""
    database = state_dir / DATABASE_NAME
    try:
        status = os.lstat(database)
    except OSError:
        raise ValueError("The control database does not exist or is not readable.") from None
    if not stat.S_ISREG(status.st_mode):
        raise ValueError("The control database is not a regular file.")
    connection = None
    try:
        connection = sqlite3.connect(database.absolute().as_uri() + "?mode=ro", uri=True, timeout=1)
        row = connection.execute(
            "SELECT schema_version FROM schema_meta WHERE singleton = 1"
        ).fetchone()
    except sqlite3.Error:
        raise ValueError("The control database has no readable schema version.") from None
    finally:
        if connection is not None:
            connection.close()
    if row is None or type(row[0]) is not int:
        raise ValueError("The control database has no readable schema version.")
    return row[0]


def check_schema(state_dir: Path) -> Finding:
    try:
        current = _read_schema_version(state_dir)
    except ValueError as error:
        return Finding("state.schema", "migrations", "fail", str(error))
    latest = LATEST_SCHEMA_VERSION
    detail = {"current": current, "latest": latest}
    if current == latest:
        return Finding(
            "state.schema", "migrations", "ok", f"Schema is current (v{latest}).", detail
        )
    if current > latest:
        return Finding(
            "state.schema",
            "migrations",
            "fail",
            f"The database schema (v{current}) is newer than this code (v{latest}); "
            "run a newer release.",
            detail,
        )
    pending = latest - current
    return Finding(
        "state.schema",
        "migrations",
        "fail",
        f"{pending} schema migration(s) pending (v{current} to v{latest}).",
        {**detail, "pending": pending},
        Fix(
            "migrate",
            f"apply {pending} pending migrations",
            automatic=True,
            command=f"tessera migrate --state-dir {state_dir}",
        ),
    )


def apply_migrate(
    state_dir: Path, receipt_dir: Path, *, backup_confirmed: bool = False
) -> dict[str, Any] | None:
    """Migrate once the operator confirms a backup; ``None`` (and no write) if none is pending."""
    before = _read_schema_version(state_dir)
    if before == LATEST_SCHEMA_VERSION:
        return None
    if before > LATEST_SCHEMA_VERSION:
        raise ValueError("The database schema is newer than this code; it cannot be migrated.")
    if not backup_confirmed:
        raise ValueError("Take a restorable backup and confirm it before migrating.")
    # Before the schema changes: an applied migration always has its receipt.
    require_writable_directory(receipt_dir, "receipt directory")
    after = migrate_state(state_dir)
    return write_receipt(
        receipt_dir,
        "migrate",
        {"schema_before": before, "schema_after": after, "latest": LATEST_SCHEMA_VERSION},
    )


# -- backup cap ------------------------------------------------------------------------------


def _estimate_backup_bytes(state_dir: Path) -> int:
    database = state_dir / DATABASE_NAME
    status = os.lstat(database)
    if not stat.S_ISREG(status.st_mode):
        raise ValueError("The control database is not a regular file.")
    total = status.st_size
    root = state_dir / OBJECTS_DIRECTORY
    try:
        if not stat.S_ISDIR(os.lstat(root).st_mode):
            return total
    except OSError:
        return total
    seen = 0
    pending = [root]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                seen += 1
                if seen > MAX_WALK_ENTRIES:
                    raise ValueError("The object store has too many entries to estimate.")
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
    return total


def _suggest_cap(estimate: int) -> int | None:
    needed = -(-estimate * 5 // 4)
    size = _GIB
    while size < needed:
        size *= 2
    return size if size <= MAX_CONFIGURABLE_BACKUP_BYTES else None


def check_backup_cap(state_dir: Path, max_backup_bytes: int | None) -> Finding:
    try:
        cap = backup_byte_limit(max_backup_bytes)
    except StoreError as error:
        return Finding("backup.cap", "backup", "fail", str(error))
    try:
        estimate = _estimate_backup_bytes(state_dir)
    except (OSError, ValueError) as error:
        message = str(error) if isinstance(error, ValueError) else "State could not be measured."
        return Finding("backup.cap", "backup", "fail", message)
    detail: dict[str, Any] = {"estimated_bytes": estimate, "cap_bytes": cap}
    if estimate * 5 <= cap * 4:
        return Finding(
            "backup.cap", "backup", "ok", "The estimated backup fits the size cap.", detail
        )
    if estimate <= cap:
        return Finding(
            "backup.cap",
            "backup",
            "warn",
            "The estimated backup is over 80% of the size cap.",
            detail,
        )
    suggestion = _suggest_cap(estimate)
    if suggestion is None:
        return Finding(
            "backup.cap",
            "backup",
            "fail",
            "The estimated backup exceeds the largest configurable cap (64 GiB).",
            detail,
        )
    return Finding(
        "backup.cap",
        "backup",
        "fail",
        "The estimated backup exceeds the size cap; backup and restore would refuse.",
        {**detail, "suggested_max_backup_bytes": suggestion},
        Fix(
            "set_backup_cap",
            "pass --max-backup-bytes N to tessera backup and restore",
            automatic=False,
            command=f"tessera backup --max-backup-bytes {suggestion} ...",
        ),
    )


# -- providers file --------------------------------------------------------------------------


def check_providers(
    providers_file: Path,
    trust_file: Path | None = None,
    guardian_status_file: Path | None = None,
) -> list[Finding]:
    path = Path(providers_file)
    try:
        if not path.is_absolute():
            raise registration.RegistrationRefused("The providers file path must be absolute.")
        if not os.path.lexists(path):
            raise registration.RegistrationRefused("The providers file does not exist.")
        document, _digest, _existed = registration.read_document(path)
        rows = registration.list_document(document)
    except (registration.RegistrationRefused, ValueError, OSError) as error:
        return [Finding("providers.file", "providers", "fail", str(error) or "Unreadable file.")]
    counts = dict(sorted(Counter(row["section"] for row in rows).items()))
    findings = [
        Finding(
            "providers.file",
            "providers",
            "ok",
            f"The providers file is valid ({len(rows)} entries).",
            {"entries": len(rows), "sections": counts},
        )
    ]
    if trust_file is not None:
        findings.append(_check_trust(Path(trust_file)))
    findings.append(check_frame_verdict_read(document, guardian_status_file))
    audit = Path(str(path) + registration.AUDIT_SUFFIX)
    if not os.path.lexists(audit):
        findings.append(
            Finding("providers.audit", "providers", "skipped", "No audit file is present.", {})
        )
        return findings
    try:
        result = registration.verify_audit(path)
    except (registration.RegistrationRefused, ValueError, OSError) as error:
        findings.append(
            Finding("providers.audit", "providers", "fail", str(error) or "Unreadable audit file.")
        )
        return findings
    detail = {"records": result["records"], "broken_at": result["broken_at"]}
    if result["ok"]:
        findings.append(
            Finding("providers.audit", "providers", "ok", "The audit chain verifies.", detail)
        )
    else:
        findings.append(
            Finding(
                "providers.audit",
                "providers",
                "fail",
                f"The audit chain is broken at record {result['broken_at']}.",
                detail,
            )
        )
    return findings


def check_frame_verdict_read(document: Any, guardian_status_file: Path | None = None) -> Finding:
    """Guardian-bounded frame verdicts are read by control (ADR 0041 stage 2). Reading is enabled
    when the providers file configures Salad profiles; it then needs ``salad_guardian_read`` and a
    guardian whose ``status`` carries ``frame_record``. Either missing silently turns auto-settle
    off, so it fails here. ``guardian_status_file`` is a saved copy of the guardian's status reply
    (this check never contacts the guardian); without it the guardian version is not checked."""
    check_id = "providers.frame_verdict_read"
    if not isinstance(document, dict) or not document.get("salad_profiles"):
        return Finding(
            check_id, "providers", "skipped", "No Salad profiles: frame verdict reading is off."
        )
    reason = "ok"
    summary = ""
    path = document.get("salad_guardian_read")
    if path is None:
        reason = "config_missing"
        summary = "Salad profiles are configured but salad_guardian_read is absent."
    else:
        from .guardian_record_reader import load_reader_config

        try:
            if not isinstance(path, str) or not path.startswith("/"):
                raise ValueError("salad_guardian_read must be an absolute path")
            load_reader_config(path)
        except ValueError as error:
            reason = "config_invalid"
            summary = f"salad_guardian_read is unusable: {error}."
    if reason == "ok" and guardian_status_file is not None:
        reason = _guardian_frame_record_reason(Path(guardian_status_file))
        if reason != "ok":
            summary = (
                "The guardian status reply has no valid frame_record: upgrade the guardian "
                "before enabling salad_guardian_read."
                if reason == "guardian_version_missing"
                else "The guardian status file is unreadable."
            )
    detail = {"reason": reason, "guardian_version_checked": guardian_status_file is not None}
    if reason == "ok":
        return Finding(
            check_id,
            "providers",
            "ok",
            "Frame verdict reading is configured"
            + ("." if guardian_status_file is not None else "; guardian version not checked."),
            detail,
        )
    return Finding(
        check_id,
        "providers",
        "fail",
        summary + " Salad frames will not auto-settle and holds wait for a human charge.",
        detail,
        Fix(
            "configure_frame_verdict_read",
            "Upgrade the guardian so status returns frame_record, then set salad_guardian_read "
            "(docs/deploy-check.md).",
            command="tessera serve --salad-guardian-read FILE",
        ),
    )


def _guardian_frame_record_reason(status_file: Path) -> str:
    from .salad_workload_jwt import strict_json

    try:
        reply = strict_json(status_file.read_bytes()[:MAX_CONFIG_BYTES])
    except (OSError, ValueError):
        return "guardian_status_unreadable"
    from .guardian_record_reader import FRAME_RECORD_KEYS, VERDICTS

    record = reply.get("frame_record") if isinstance(reply, dict) else None
    if (
        isinstance(record, dict)
        and set(record) == FRAME_RECORD_KEYS
        and (record.get("verdict") in VERDICTS)
    ):
        return "ok"
    return "guardian_version_missing"


def _check_trust(trust_file: Path) -> Finding:
    from .provider_trust import load_policy_file

    try:
        load_policy_file(trust_file)
    except (ValueError, OSError) as error:
        return Finding(
            "providers.trust",
            "providers",
            "fail",
            "The provider trust file is invalid: " + (str(error) or "unreadable") + ".",
        )
    return Finding("providers.trust", "providers", "ok", "The provider trust file is valid.")


# -- executor profiles -----------------------------------------------------------------------


def _config_names(executor_config: Path) -> tuple[str, list[str]]:
    """``(profiles_file, names)`` from the config; nothing else in it is opened."""
    from .salad_workload_jwt import strict_json

    path = Path(executor_config)
    if not path.is_absolute():
        raise ValueError("The executor config path must be absolute.")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise ValueError("The executor config is missing, a symlink or unreadable.") from None
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or not 0 < status.st_size <= MAX_CONFIG_BYTES:
            raise ValueError("The executor config must be a regular file of at most 64 KiB.")
        raw = os.read(descriptor, MAX_CONFIG_BYTES + 1)
    finally:
        os.close(descriptor)
    try:
        document = strict_json(raw)
    except ValueError:
        raise ValueError("The executor config is not strict JSON.") from None
    if not isinstance(document, dict):
        raise ValueError("The executor config must be a JSON object.")
    profiles_file = document.get("profiles_file")
    one, many = document.get("profile"), document.get("profiles")
    if not isinstance(profiles_file, str) or not profiles_file:
        raise ValueError("The executor config names no profiles_file.")
    if (one is None) == (many is None):
        raise ValueError("The executor config must name profile or profiles, not both.")
    names = [one] if one is not None else many
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        raise ValueError("The executor config profile names are malformed.")
    return profiles_file, names


def check_executor_profiles(executor_config: Path) -> list[Finding]:
    from .salad_campaign_profiles import load_profiles

    check_id = "providers.executor_profiles"
    try:
        profiles_file, names = _config_names(executor_config)
    except ValueError as error:
        return [Finding(check_id, "providers", "fail", str(error))]
    try:
        loaded = load_profiles(profiles_file)
    except (ValueError, OSError):
        return [
            Finding(
                check_id,
                "providers",
                "fail",
                f"Profile {name!r} cannot be checked: the profiles file is not an "
                "owner-only (0600) absolute JSON file holding a valid profiles document.",
                {"profile": name},
            )
            for name in names
        ]
    findings = []
    for name in names:
        if loaded.get(name) is None:
            findings.append(
                Finding(
                    check_id,
                    "providers",
                    "fail",
                    f"Profile {name!r} is not defined in the profiles file.",
                    {"profile": name},
                )
            )
    if not findings:
        findings.append(
            Finding(
                check_id,
                "providers",
                "ok",
                f"All {len(names)} named profile(s) validate.",
                {"profiles": list(names)},
            )
        )
    return findings
