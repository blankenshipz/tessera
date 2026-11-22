"""Operator data-class trust setting per compute provider (ADR 0040 section 4).

The setting says which of ``public``, ``private_code_approved`` and ``private_derived``
each provider *instance* may receive. An instance is a provider id plus a bound worker
principal: a class reaches a worker only when that principal is listed under exactly that
provider. Everything else, including the built-in default (which binds no principal),
receives ``public`` only. Specs and agents can only narrow it; there is no write API.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .campaign_providers import TRUST_CLASSES, TrustClass

SCHEMA = "tessera.provider-trust.v1"
# Fail-closed: binds no worker principal, so every provider instance gets public only
# (ADR 0040 section 4, Default). There is no verifiable "control host's own worker".
BUILTIN_DEFAULT: dict[str, Any] = {"schema": SCHEMA, "providers": {}}


# Owner-set modes (ADR 0047). ``listed_classes`` (default, omitted from the canonical form so
# existing digests are unchanged): a listed principal receives its provider entry's classes.
# ``all_registered_providers``: a principal listed under a provider that is registered on
# this platform receives every class, and agents and specs choose among those providers.
# In every mode a principal listed nowhere, or under an unregistered provider id, receives
# public only, and nothing here relaxes the egress refusals for non-public inputs.
MODE_LISTED = "listed_classes"
MODE_ALL_REGISTERED = "all_registered_providers"
MODES = (MODE_LISTED, MODE_ALL_REGISTERED)


class TrustPolicyRefused(ValueError):
    """The trust file is unreadable, too permissive or malformed (serve exits 2)."""


PUBLIC_ONLY: frozenset[TrustClass] = frozenset({"public"})
MAX_FILE_BYTES = 256 * 1024
_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_PRINCIPAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_PROVIDER_KEYS = frozenset({"data_classes", "worker_principal_ids"})
POLICY_CHANGED = "provider_trust.policy_changed"
RELOAD_REFUSED = "provider_trust.reload_refused"
DEPLOYMENT_START = "deployment_start"


def _digest(document: dict[str, Any]) -> str:
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> dict[str, Any]:
    """Validate a trust document and return its canonical form; raises TrustPolicyRefused."""
    if not isinstance(value, dict) or not {"schema", "providers"} <= set(value) <= {
        "schema",
        "providers",
        "mode",
    }:
        raise TrustPolicyRefused(
            "trust file must have the keys schema and providers, and optionally mode"
        )
    mode = value.get("mode", MODE_LISTED)
    if mode not in MODES:
        raise TrustPolicyRefused(f"trust file mode must be one of {list(MODES)}")
    if value["schema"] != SCHEMA:
        raise TrustPolicyRefused(f"trust file schema must be {SCHEMA}")
    providers = value["providers"]
    if not isinstance(providers, dict) or len(providers) > 64:
        raise TrustPolicyRefused("trust file providers must be an object of at most 64 entries")
    canonical: dict[str, Any] = {}
    owner: dict[str, str] = {}
    for provider_id, entry in sorted(providers.items()):
        if not isinstance(provider_id, str) or not _PROVIDER_ID.fullmatch(provider_id):
            raise TrustPolicyRefused("trust file provider id is invalid")
        if not isinstance(entry, dict) or set(entry) != _PROVIDER_KEYS:
            raise TrustPolicyRefused(
                f"provider {provider_id!r} must have exactly data_classes and worker_principal_ids"
            )
        classes, principals = entry["data_classes"], entry["worker_principal_ids"]
        if (
            not isinstance(classes, list)
            or not all(isinstance(c, str) and c in TRUST_CLASSES for c in classes)
            or len(set(classes)) != len(classes)
        ):
            raise TrustPolicyRefused(
                f"provider {provider_id!r} data_classes must be unique values of "
                f"{list(TRUST_CLASSES)}"
            )
        if (
            not isinstance(principals, list)
            or len(principals) > 256
            or not all(isinstance(p, str) and _PRINCIPAL_ID.fullmatch(p) for p in principals)
            or len(set(principals)) != len(principals)
        ):
            raise TrustPolicyRefused(
                f"provider {provider_id!r} worker_principal_ids must be unique principal ids"
            )
        for principal in principals:
            if principal in owner:
                raise TrustPolicyRefused(
                    f"worker principal {principal!r} is listed under both "
                    f"{owner[principal]!r} and {provider_id!r}; a principal binds one provider"
                )
            owner[principal] = provider_id
        canonical[provider_id] = {
            "data_classes": [c for c in TRUST_CLASSES if c in classes],
            "worker_principal_ids": sorted(principals),
        }
    document: dict[str, Any] = {"schema": SCHEMA, "providers": canonical}
    if mode != MODE_LISTED:
        document["mode"] = mode
    return document


def _registered(provider_id: str) -> bool:
    """Is the id a compute provider registered on this platform (built in or configured)?"""
    from .campaign_providers import load_builtin_providers, provider_ids

    # Built-in modules register themselves on import (idempotent); a configured plugin or
    # self-hosted instance is registered only by the serve process that read its config,
    # so elsewhere it stays unregistered and therefore public-only (fail closed).
    load_builtin_providers()
    return provider_id in provider_ids()


def _allowed_in(
    document: dict[str, Any], provider_id: str | None, worker_principal_id: str | None
) -> frozenset[TrustClass]:
    if not isinstance(provider_id, str) or not isinstance(worker_principal_id, str):
        return PUBLIC_ONLY
    entry = document.get("providers", {}).get(provider_id)
    if entry is None or worker_principal_id not in entry["worker_principal_ids"]:
        return PUBLIC_ONLY
    if document.get("mode") == MODE_ALL_REGISTERED:
        # Configured (listed) and registered: every class. An id no provider registered
        # (a typo, a removed plugin) stays public only.
        return frozenset(TRUST_CLASSES) if _registered(provider_id) else PUBLIC_ONLY
    return PUBLIC_ONLY | frozenset(entry["data_classes"])


@dataclass(frozen=True)
class TrustPolicy:
    digest: str
    document: dict[str, Any]  # canonical
    source: str  # "builtin" | "file"

    def allowed(
        self, provider_id: str | None, worker_principal_id: str | None
    ) -> frozenset[TrustClass]:
        """Classes for the instance; {"public"} unless the principal is listed under
        exactly that provider."""
        return _allowed_in(self.document, provider_id, worker_principal_id)

    def provider_for_worker(self, worker_principal_id: str) -> str | None:
        """None means the principal is unbound."""
        for provider_id, entry in self.document["providers"].items():
            if worker_principal_id in entry["worker_principal_ids"]:
                return provider_id
        return None


def policy_from_document(value: Any, source: str) -> TrustPolicy:
    document = _canonical(value)
    return TrustPolicy(_digest(document), document, source)


@dataclass(frozen=True)
class AdmittedInput:
    code_paths: frozenset[str]
    input_data_class: str  # public | private_derived | none_all_approved_code
    input_code: str  # private_code_approved | none
    basis: str  # public_only | provider_trust_policy
    policy_digest: str


def create_provider_trust_schema(connection: sqlite3.Connection) -> None:
    """Idempotent: fresh schemas and the v64->v65 migration share this definition."""
    connection.execute(
        """CREATE TABLE IF NOT EXISTS provider_trust_policies (
        digest TEXT PRIMARY KEY, document_json TEXT NOT NULL,
        source TEXT NOT NULL CHECK(source IN ('builtin','file')),
        first_loaded_at TEXT NOT NULL)"""
    )
    for event in ("update", "delete"):
        connection.execute(
            f"""CREATE TRIGGER IF NOT EXISTS provider_trust_policies_no_{event}
            BEFORE {event.upper()} ON provider_trust_policies
            BEGIN SELECT RAISE(ABORT, 'provider trust policies are immutable'); END"""
        )
    # The append-only activation log: its newest row names the effective policy for every
    # process that shares the database (serve records at start and on reload).
    connection.execute(
        """CREATE TABLE IF NOT EXISTS provider_trust_activations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        digest TEXT NOT NULL REFERENCES provider_trust_policies(digest),
        actor TEXT NOT NULL, activated_at TEXT NOT NULL)"""
    )
    for event in ("update", "delete"):
        connection.execute(
            f"""CREATE TRIGGER IF NOT EXISTS provider_trust_activations_no_{event}
            BEFORE {event.upper()} ON provider_trust_activations
            BEGIN SELECT RAISE(ABORT, 'provider trust activations are append-only'); END"""
        )


# ---- loading -----------------------------------------------------------------------------


def load_policy_file(path: Path) -> TrustPolicy:
    """Check permissions and parse; raises TrustPolicyRefused.

    Refuses a symlink, a file that is not a regular file owned by the service user, a
    group- or world-writable file, an oversized or malformed file, a wrong schema,
    unknown keys or classes, and a worker principal listed under two providers.
    """
    path = Path(path)
    try:
        info = os.lstat(path)
    except OSError:
        raise TrustPolicyRefused("trust file cannot be read") from None
    if stat.S_ISLNK(info.st_mode):
        raise TrustPolicyRefused("trust file must not be a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise TrustPolicyRefused("trust file must be a regular file")
    if info.st_uid != os.geteuid():
        raise TrustPolicyRefused("trust file must be owned by the service user")
    if info.st_mode & 0o022:
        raise TrustPolicyRefused("trust file must not be group- or world-writable")
    try:
        # O_NOFOLLOW closes the window between lstat and open.
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise TrustPolicyRefused("trust file cannot be read") from None
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise TrustPolicyRefused("trust file changed while it was read")
        if opened.st_size > MAX_FILE_BYTES:
            raise TrustPolicyRefused("trust file is too large")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    finally:
        os.close(descriptor)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise TrustPolicyRefused("trust file is not valid JSON") from None
    return policy_from_document(value, "file")


_lock = threading.Lock()
_current: TrustPolicy | None = None
_loaded_at: str | None = None


def builtin_policy() -> TrustPolicy:
    return policy_from_document(BUILTIN_DEFAULT, "builtin")


def _process_policy() -> TrustPolicy:
    global _current
    with _lock:
        if _current is None:
            _current = builtin_policy()
        return _current


_cache: dict[str, TrustPolicy] = {}


def recorded_policy(connection: sqlite3.Connection) -> TrustPolicy | None:
    """The newest recorded policy (the shared source of truth), or None when none is
    recorded or the table is absent. Cheap: one indexed read, the parse is cached by
    digest (policies are immutable). An unreadable recorded document fails closed to the
    builtin default."""
    try:
        row = connection.execute(
            """SELECT a.digest, p.document_json, p.source FROM provider_trust_activations a
            JOIN provider_trust_policies p ON p.digest = a.digest
            ORDER BY a.id DESC LIMIT 1"""
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    digest, document_json, source = row[0], row[1], row[2]
    cached = _cache.get(digest)
    if cached is not None:
        return cached
    try:
        policy = policy_from_document(json.loads(document_json), source)
        if policy.digest != digest:
            raise TrustPolicyRefused("recorded digest mismatch")
    except (ValueError, RecursionError):
        return builtin_policy()
    if len(_cache) > 64:
        _cache.clear()
    _cache[digest] = policy
    return policy


def current_policy(connection: sqlite3.Connection | None = None) -> TrustPolicy:
    """The effective policy. With a connection the latest recorded policy wins, so every
    process sharing the database enforces the same setting and a reload reaches all of
    them. Without a recorded row, or without a connection, the process policy applies
    (the builtin default until one is installed)."""
    if connection is not None:
        recorded = recorded_policy(connection)
        if recorded is not None:
            return recorded
    return _process_policy()


def install_policy(policy: TrustPolicy, loaded_at: str | None = None) -> None:
    global _current, _loaded_at
    if not isinstance(policy, TrustPolicy):
        raise TypeError("a TrustPolicy is required")
    from .store import utc_now

    with _lock:
        _current = policy
        _loaded_at = loaded_at or utc_now()


def reset_policy() -> None:
    """Back to the builtin default (tests and shutdown)."""
    global _current, _loaded_at
    with _lock:
        _current = None
        _loaded_at = None


def loaded_at() -> str | None:
    """When the process installed the current policy; None for the untouched default."""
    with _lock:
        return _loaded_at


# ---- recording and audit -----------------------------------------------------------------


def _diff(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    before = (old or {}).get("providers", {})
    after = new["providers"]
    result: dict[str, Any] = {}
    for provider_id in sorted(set(before) | set(after)):
        a = before.get(provider_id, {"data_classes": [], "worker_principal_ids": []})
        b = after.get(provider_id, {"data_classes": [], "worker_principal_ids": []})
        change: dict[str, Any] = {}
        for key in ("data_classes", "worker_principal_ids"):
            added = sorted(set(b[key]) - set(a[key]))
            removed = sorted(set(a[key]) - set(b[key]))
            if added or removed:
                change[key] = {"added": added, "removed": removed}
        if provider_id not in before:
            change["provider"] = "added"
        elif provider_id not in after:
            change["provider"] = "removed"
        if change:
            result[provider_id] = change
    before_mode = (old or {}).get("mode", MODE_LISTED)
    after_mode = new.get("mode", MODE_LISTED)
    if before_mode != after_mode:
        # Provider ids never start with "_", so this key cannot collide with one.
        result["_mode"] = {"from": before_mode, "to": after_mode}
    return result


def _audit(
    connection: sqlite3.Connection,
    event_type: str,
    decision: str,
    actor: str,
    payload: dict[str, Any],
    now: str,
) -> int:
    """Append one operator audit event per organization (the setting is deployment-wide)."""
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'operator_audit_events'"
        ).fetchone()
        is None
    ):
        return 0
    principal = connection.execute("SELECT id FROM principals WHERE id = ?", (actor,)).fetchone()
    body = json.dumps({**payload, "actor": actor}, separators=(",", ":"), sort_keys=True)
    orgs = [row[0] for row in connection.execute("SELECT id FROM organizations ORDER BY id")]
    for org_id in orgs:
        connection.execute(
            """INSERT INTO operator_audit_events
            (org_id, project_id, event_type, decision, actor_principal_id, created_at,
             payload_json) VALUES (?, NULL, ?, ?, ?, ?, ?)""",
            (org_id, event_type, decision, None if principal is None else actor, now, body),
        )
    return len(orgs)


def _last_recorded_digest(connection: sqlite3.Connection) -> str | None:
    row = connection.execute(
        "SELECT digest FROM provider_trust_activations ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is not None:
        return row[0]
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'operator_audit_events'"
        ).fetchone()
        is None
    ):
        return None
    row = connection.execute(
        """SELECT payload_json FROM operator_audit_events WHERE event_type = ?
        ORDER BY cursor DESC LIMIT 1""",
        (POLICY_CHANGED,),
    ).fetchone()
    return None if row is None else json.loads(row[0]).get("new_digest")


def _stored_document(connection: sqlite3.Connection, digest: str | None) -> dict[str, Any] | None:
    if digest is None:
        return None
    row = connection.execute(
        "SELECT document_json FROM provider_trust_policies WHERE digest = ?", (digest,)
    ).fetchone()
    return None if row is None else json.loads(row[0])


def record_policy(
    connection: sqlite3.Connection, policy: TrustPolicy, actor: str, now: str
) -> bool:
    """Store the policy (immutable, keyed by digest) in the caller's transaction.

    Appends ``provider_trust.policy_changed`` (old and new digest, per-provider
    difference, actor) only when the digest differs from the last recorded one; True
    when an event was appended. An identical load appends nothing.
    """
    connection.execute(
        """INSERT OR IGNORE INTO provider_trust_policies
        (digest, document_json, source, first_loaded_at) VALUES (?, ?, ?, ?)""",
        (policy.digest, json.dumps(policy.document, sort_keys=True), policy.source, now),
    )
    old_digest = _last_recorded_digest(connection)
    if old_digest == policy.digest:
        return False
    connection.execute(
        "INSERT INTO provider_trust_activations (digest, actor, activated_at) VALUES (?, ?, ?)",
        (policy.digest, actor, now),
    )
    return (
        _audit(
            connection,
            POLICY_CHANGED,
            "allowed",
            actor,
            {
                "old_digest": old_digest,
                "new_digest": policy.digest,
                "source": policy.source,
                "providers_changed": _diff(
                    _stored_document(connection, old_digest), policy.document
                ),
            },
            now,
        )
        > 0
    )


def record_reload_refused(
    connection: sqlite3.Connection, actor: str, now: str, reason: str
) -> None:
    """A refused load keeps the current policy; the refusal is audited."""
    _audit(
        connection,
        RELOAD_REFUSED,
        "denied",
        actor,
        {"kept_digest": current_policy(connection).digest, "reason": reason[:300]},
        now,
    )


def record_at_start(
    database_path: Path, policy: TrustPolicy, actor: str = DEPLOYMENT_START
) -> bool:
    """Record ``policy`` in its own transaction (serve start-up)."""
    from .store import _connect, utc_now

    connection = _connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        changed = record_policy(connection, policy, actor, utc_now())
        connection.execute("COMMIT")
        return changed
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def show(policy: TrustPolicy | None = None) -> dict[str, Any]:
    """The readable setting: document, digest and source (the load time is the caller's)."""
    policy = policy or current_policy()
    return {
        "digest": policy.digest,
        "source": policy.source,
        "loaded_at": loaded_at(),
        "policy": policy.document,
    }


# ---- checks ------------------------------------------------------------------------------


def _effective(
    connection: sqlite3.Connection | None,
    provider_id: str | None,
    worker_principal_id: str | None,
    narrowed: frozenset[TrustClass] | None,
    approval_digest: str | None,
) -> tuple[frozenset[TrustClass], TrustPolicy]:
    """current setting, intersected with the approval-time setting and the spec's narrowing."""
    policy = current_policy(connection)
    allowed = policy.allowed(provider_id, worker_principal_id)
    if approval_digest is not None and approval_digest != policy.digest:
        stored = _stored_document(connection, approval_digest) if connection is not None else None
        # An unknown approval-time setting grants nothing beyond public.
        allowed &= (
            PUBLIC_ONLY if stored is None else _allowed_in(stored, provider_id, worker_principal_id)
        )
    if narrowed is not None:
        allowed &= frozenset(narrowed)
    return allowed, policy


def _instance(provider_id: str | None, worker_principal_id: str | None) -> str:
    return (
        f"provider {provider_id or 'none'!r} with worker principal "
        f"{worker_principal_id or 'none'!r}"
    )


def provider_admits(
    connection: sqlite3.Connection,
    provider_id: str | None,
    worker_principal_id: str | None,
    snapshot_id: str,
    narrowed: frozenset[TrustClass] | None = None,
    approval_digest: str | None = None,
) -> AdmittedInput:
    """ADR 0040 section 4 check; raises ``DataClassRefused`` naming provider and principal."""
    from .code_approval import PRIVATE_CODE_APPROVED, snapshot_code_approval
    from .code_approval import _entries as manifest_entries
    from .data_class import DataClassRefused, snapshot_data_class

    allowed, policy = _effective(
        connection, provider_id, worker_principal_id, narrowed, approval_digest
    )
    who = _instance(provider_id, worker_principal_id)
    approval = snapshot_code_approval(connection, snapshot_id)
    if approval is not None and approval.get("valid") is not True:
        raise DataClassRefused(
            "the snapshot's private code approval does not match its files; refused for "
            f"{who} under any trust setting"
        )
    try:
        entries = manifest_entries(connection, snapshot_id)
    except (ValueError, TypeError, KeyError, RecursionError) as error:
        if isinstance(error, DataClassRefused):
            raise
        raise DataClassRefused("the input snapshot manifest is unreadable") from None
    recorded = snapshot_data_class(connection, snapshot_id)["data_class"]
    approved: frozenset[str] = frozenset()
    if approval is not None:
        # Approved code is never counted as data, whatever the snapshot's recorded class.
        if PRIVATE_CODE_APPROVED not in allowed:
            raise DataClassRefused(
                f"the snapshot carries approved private code, which {who} is not "
                "trusted to receive (private_code_approved); without a provider trust "
                "setting only data recorded public is admitted",
                admit_class=PRIVATE_CODE_APPROVED,
            )
        approved = frozenset(f["relative_path"] for f in approval["files"])
    has_data = approval is None or any(e["relative_path"] not in approved for e in entries)
    if has_data and recorded not in allowed:
        raise DataClassRefused(
            f"the snapshot data is recorded {recorded}, which {who} is not trusted to receive "
            "(without a provider trust setting only data recorded public with license and "
            "source evidence is admitted)",
            admit_class=recorded if recorded == "private_derived" else None,
        )
    return AdmittedInput(
        code_paths=approved,
        input_data_class=recorded if has_data else "none_all_approved_code",
        input_code=PRIVATE_CODE_APPROVED if approval is not None else "none",
        basis="public_only"
        if approval is None and recorded == "public"
        else "provider_trust_policy",
        policy_digest=policy.digest,
    )


def provider_admits_class(
    provider_id: str | None,
    worker_principal_id: str | None,
    cls: TrustClass,
    narrowed: frozenset[TrustClass] | None = None,
    approval_digest: str | None = None,
    connection: sqlite3.Connection | None = None,
) -> bool:
    allowed, _ = _effective(connection, provider_id, worker_principal_id, narrowed, approval_digest)
    return cls in allowed


def admission_basis(connection: sqlite3.Connection, snapshot_id: str | None) -> str:
    """``public_only`` for an unapproved snapshot recorded public, else the trust policy."""
    from .code_approval import snapshot_code_approval
    from .data_class import snapshot_data_class

    if snapshot_id is None:
        return "provider_trust_policy"
    if snapshot_code_approval(connection, snapshot_id) is None and (
        snapshot_data_class(connection, snapshot_id)["data_class"] == "public"
    ):
        return "public_only"
    return "provider_trust_policy"


def receipt_trust_fields(
    connection: sqlite3.Connection, worker_principal_id: str | None, snapshot_id: str | None
) -> dict[str, Any]:
    """Receipt fields for a run: the provider the worker is bound to, the setting digest
    in force and how the input was admitted. Records; never enforces."""
    policy = current_policy(connection)
    return {
        "trust_provider_id": (
            None if worker_principal_id is None else policy.provider_for_worker(worker_principal_id)
        ),
        "trust_policy_digest": policy.digest,
        "admission_basis": admission_basis(connection, snapshot_id),
    }


def worker_admits_class(
    worker_principal_id: str | None,
    cls: TrustClass,
    connection: sqlite3.Connection | None = None,
) -> bool:
    """Does the provider instance this worker principal is bound to admit ``cls``?

    An unbound worker (listed under no provider) receives ``public`` only.
    """
    if not isinstance(worker_principal_id, str):
        return cls == "public"
    provider_id = current_policy(connection).provider_for_worker(worker_principal_id)
    return provider_admits_class(provider_id, worker_principal_id, cls, connection=connection)


def check_narrowing(
    provider_id: str,
    send_classes: list[str] | None,
    worker_principal_id: str | None = None,
    connection: sqlite3.Connection | None = None,
) -> None:
    """Raises ValueError when a spec narrows beyond what the provider may receive.

    Without a worker principal the provider's own listed classes are the bound; with one
    the instance's classes are. A class outside the setting is refused, never dropped.
    """
    if send_classes is None:
        return
    unknown = sorted(set(send_classes) - set(TRUST_CLASSES))
    if unknown:
        raise ValueError(f"send_classes has unknown classes {unknown}")
    policy = current_policy(connection)
    if worker_principal_id is not None:
        allowed = policy.allowed(provider_id, worker_principal_id)
    else:
        entry = policy.document["providers"].get(provider_id)
        if entry is not None and policy.document.get("mode") == MODE_ALL_REGISTERED:
            allowed = frozenset(TRUST_CLASSES) if _registered(provider_id) else PUBLIC_ONLY
        else:
            allowed = PUBLIC_ONLY | frozenset(() if entry is None else entry["data_classes"])
    outside = sorted(set(send_classes) - allowed)
    if outside:
        raise ValueError(
            f"send_classes {outside} exceed what the provider trust setting allows "
            f"provider {provider_id!r} to receive"
        )


def audit_operator_event(
    connection: sqlite3.Connection,
    event_type: str,
    decision: str,
    actor: str,
    payload: dict[str, Any],
    now: str,
) -> int:
    """Append one operator audit event (per organization) for a deployment-wide setting change.

    The public form of the provider setting's own audit writer, used by provider registration
    (ADR 0081). The payload must never hold secret or entry values."""
    return _audit(connection, event_type, decision, actor, payload, now)
