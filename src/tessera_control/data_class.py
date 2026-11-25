"""Data class of staged input snapshots: the one source of truth (ADR 0039).

``DataClass`` is ``"public"`` or ``"private_derived"``. Every ``source_snapshots`` row
has at most one ``snapshot_data_classes`` row; a missing or malformed row reads as
``private_derived`` (fail closed).

* ``public`` is a human assertion made at staging, never inferred. It needs evidence
  (a license identifier and a source URL or reference) and a human actor, and it is
  recorded with that actor's decision provenance (``approval_trust.decision_class``).
* Everything else is ``private_derived``: an undeclared staging, any snapshot built
  from run outputs or artifacts (always, whatever the request says), and every
  snapshot that existed before schema v62.
* There is no reclassification path. A later human reclassification would need its
  own audited endpoint; none exists.

A target's provider receives an input only as the operator's trust setting allows
(ADR 0040 section 4, ``provider_trust``): the default is ``public`` only, at route
admission and again when control issues and serves the provider input ticket. Files a
human approved as private code (``code_approval``, ADR 0039 amendment 1) are a separate
class, ``private_code_approved``; they stay private and are never relabelled public.

This module has no module-level dependency on stores, so other modules (for example
a campaign runner) can import ``DataClass`` and the readers without an import cycle.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError, model_validator

DataClass = Literal["public", "private_derived"]
PUBLIC: DataClass = "public"
PRIVATE_DERIVED: DataClass = "private_derived"
DATA_CLASSES: tuple[DataClass, ...] = (PUBLIC, PRIVATE_DERIVED)

# Why a snapshot has its class.
ORIGIN_DECLARED = "human_declared"  # a human staging declaration (public needs evidence)
ORIGIN_DEFAULT = "default"  # no declaration at staging: private_derived
ORIGIN_DERIVED = "derived"  # built from run outputs or artifacts: always private_derived
ORIGIN_MIGRATED = "migrated"  # staged before schema v62: private_derived, fail closed
ORIGIN_UNRECORDED = "unrecorded"  # read-side only: no row, so private_derived

# Run outputs (and anything built from them) are never public.
OUTPUT_DATA_CLASS: DataClass = PRIVATE_DERIVED

LicenseId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,127}$")]
# A URL or another stable reference: printable ASCII without whitespace.
SourceRef = Annotated[str, StringConstraints(pattern=r"^[\x21-\x7e]{1,2048}$")]
_NOT_A_LICENSE = frozenset({"NOASSERTION", "NONE", "UNKNOWN", "UNLICENSED", "PROPRIETARY"})


class DataClassRefused(ValueError):
    """The recorded data class does not permit this use.

    ``admit_class`` is the provider-trust class whose admission would lift this refusal
    (None when no trust setting could: a mismatched code approval, an unreadable manifest,
    public data without evidence). ``data_class`` is the class the provider was not trusted to
    receive (the snapshot's recorded class, or ``private_code_approved``), None when the
    refusal is not about a class (a mismatched approval, an unreadable manifest)."""

    def __init__(
        self, message: str, admit_class: str | None = None, data_class: str | None = None
    ) -> None:
        super().__init__(message)
        self.admit_class = admit_class
        self.data_class = data_class


class DataClassDeclaration(BaseModel):
    """A human's data-class declaration at staging; ``public`` requires evidence."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    data_class: DataClass
    license: LicenseId | None = None
    source: SourceRef | None = None

    @model_validator(mode="after")
    def _evidence(self) -> DataClassDeclaration:
        if self.data_class == PUBLIC:
            if self.license is None or self.source is None:
                raise ValueError("A public data class requires license and source evidence.")
            if self.license.upper() in _NOT_A_LICENSE:
                raise ValueError("A public data class requires an identified license.")
        return self


def parse_declaration(value: Any) -> DataClassDeclaration | None:
    """``None`` stays ``None``; anything else must be a valid declaration."""
    if value is None or isinstance(value, DataClassDeclaration):
        return value
    try:
        return DataClassDeclaration.model_validate(value)
    except ValidationError as error:
        raise DataClassRefused("Invalid data class declaration.") from error


def create_data_class_schema(connection: sqlite3.Connection) -> None:
    """Idempotent: fresh schemas and the v61->v62 migration share this definition.

    ``snapshot_id`` has no foreign key: a chunked upload records its begin-time
    declaration under the id its snapshot will take. Readers join ``source_snapshots``.
    """
    connection.execute(
        """CREATE TABLE IF NOT EXISTS snapshot_data_classes (
        snapshot_id TEXT PRIMARY KEY,
        data_class TEXT NOT NULL CHECK(data_class IN ('public','private_derived')),
        origin TEXT NOT NULL
            CHECK(origin IN ('human_declared','default','derived','migrated')),
        license TEXT, source TEXT,
        actor_principal_id TEXT REFERENCES principals(id),
        decision_class TEXT, provenance_json TEXT,
        recorded_at TEXT NOT NULL,
        CHECK(data_class='private_derived' OR (origin='human_declared'
            AND license IS NOT NULL AND source IS NOT NULL
            AND actor_principal_id IS NOT NULL AND decision_class IS NOT NULL))
        )"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS snapshot_data_classes_no_update
        BEFORE UPDATE ON snapshot_data_classes
        BEGIN SELECT RAISE(ABORT, 'snapshot data class is immutable'); END"""
    )
    # A recorded snapshot's class cannot be deleted and re-inserted. Only a declaration
    # for an upload that never became a snapshot may be discarded.
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS snapshot_data_classes_no_delete
        BEFORE DELETE ON snapshot_data_classes
        WHEN EXISTS (SELECT 1 FROM source_snapshots WHERE id = OLD.snapshot_id)
        BEGIN SELECT RAISE(ABORT, 'snapshot data class is immutable'); END"""
    )
    # INSERT OR REPLACE deletes without firing delete triggers (recursive_triggers is
    # off), so a second row for the same id is refused before conflict resolution.
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS snapshot_data_classes_no_replace
        BEFORE INSERT ON snapshot_data_classes
        WHEN EXISTS (SELECT 1 FROM snapshot_data_classes WHERE snapshot_id = NEW.snapshot_id)
        BEGIN SELECT RAISE(ABORT, 'snapshot data class is immutable'); END"""
    )


def migrate_existing_snapshots(connection: sqlite3.Connection, now: str) -> int:
    """Every snapshot without a row becomes private_derived (origin ``migrated``)."""
    return connection.execute(
        """INSERT INTO snapshot_data_classes
        (snapshot_id, data_class, origin, recorded_at)
        SELECT id, 'private_derived', 'migrated', ? FROM source_snapshots
        WHERE id NOT IN (SELECT snapshot_id FROM snapshot_data_classes)""",
        (now,),
    ).rowcount


def _public_record(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {"data_class": PRIVATE_DERIVED, "origin": ORIGIN_UNRECORDED}
    record = {
        "data_class": row["data_class"],
        "origin": row["origin"],
        "license": row["license"],
        "source": row["source"],
        "actor_principal_id": row["actor_principal_id"],
        "decision_class": row["decision_class"],
        "recorded_at": row["recorded_at"],
    }
    if record["data_class"] == PUBLIC and not (
        record["origin"] == ORIGIN_DECLARED
        and record["license"]
        and record["source"]
        and record["actor_principal_id"]
        and record["decision_class"]
    ):
        # A row that bypassed the CHECK (for example a hand-edited database) is not public.
        return {"data_class": PRIVATE_DERIVED, "origin": ORIGIN_UNRECORDED}
    if record["data_class"] not in DATA_CLASSES:
        return {"data_class": PRIVATE_DERIVED, "origin": ORIGIN_UNRECORDED}
    return record


def _row(connection: sqlite3.Connection, snapshot_id: str) -> sqlite3.Row | None:
    previous = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            """SELECT d.* FROM snapshot_data_classes d
            JOIN source_snapshots s ON s.id = d.snapshot_id WHERE d.snapshot_id = ?""",
            (snapshot_id,),
        ).fetchone()
    finally:
        connection.row_factory = previous


def snapshot_data_class(connection: sqlite3.Connection, snapshot_id: str) -> dict[str, Any]:
    """The recorded class of an existing snapshot; anything unrecorded is private_derived."""
    return _public_record(_row(connection, snapshot_id))


def _pending(connection: sqlite3.Connection, snapshot_id: str) -> sqlite3.Row | None:
    previous = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            "SELECT * FROM snapshot_data_classes WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
    finally:
        connection.row_factory = previous


def _require_public_actor(live: dict[str, Any]) -> None:
    from .approval_trust import require_decision_actor
    from .store import Forbidden

    if live.get("role") != "human":
        raise Forbidden("Only a human may declare staged data public.")
    require_decision_actor(live, forbidden=Forbidden)


def _insert(
    connection: sqlite3.Connection,
    live: dict[str, Any],
    snapshot_id: str,
    declaration: DataClassDeclaration | None,
    origin: str,
    now: str,
) -> None:
    from .approval_trust import decision_provenance

    data_class = declaration.data_class if origin == ORIGIN_DECLARED else PRIVATE_DERIVED
    declared = origin == ORIGIN_DECLARED
    provenance = decision_provenance(live) if declared else None
    connection.execute(
        """INSERT INTO snapshot_data_classes
        (snapshot_id, data_class, origin, license, source, actor_principal_id,
         decision_class, provenance_json, recorded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            snapshot_id,
            data_class,
            origin,
            declaration.license if declared else None,
            declaration.source if declared else None,
            live["id"] if declared else None,
            provenance["decision_class"] if provenance else None,
            json.dumps(provenance, sort_keys=True) if provenance else None,
            now,
        ),
    )


def _refuse_relabelled_bytes(
    connection: sqlite3.Connection, org_id: str, digest: str, snapshot_id: str
) -> None:
    """A public claim cannot relabel bytes this org already holds as derived or migrated.

    Same digest means same bytes. If any other snapshot of the org with that digest is
    recorded ``derived`` or ``migrated`` (or has no record at all), a fresh staging
    must not turn those bytes public.
    """
    from .store import Conflict

    if connection.execute(
        """SELECT 1 FROM source_snapshots s JOIN projects p ON p.id = s.project_id
        LEFT JOIN snapshot_data_classes d ON d.snapshot_id = s.id
        WHERE p.org_id = ? AND s.digest = ? AND s.id != ?
        AND (d.snapshot_id IS NULL OR d.origin IN ('derived', 'migrated'))
        LIMIT 1""",
        (org_id, digest, snapshot_id),
    ).fetchone():
        raise Conflict(
            "These bytes are already recorded as derived or migrated data; "
            "they cannot be declared public."
        )


# A snapshot is public only with a human declaration; everything else (including any
# future private origin) is non-public. ``d`` is the LEFT JOINed snapshot_data_classes row.
NOT_PUBLIC_SQL = (
    "(d.snapshot_id IS NULL OR NOT (d.data_class = 'public' AND d.origin = 'human_declared' "
    "AND d.license IS NOT NULL AND d.source IS NOT NULL AND d.actor_principal_id IS NOT NULL "
    "AND d.decision_class IS NOT NULL))"
)


def refuse_relabelled_files(
    connection: sqlite3.Connection,
    org_id: str,
    snapshot_id: str,
    *,
    entries: list[dict[str, Any]] | None = None,
    digest: str | None = None,
) -> None:
    """Per file: a public snapshot's non-approved files cannot relabel held private bytes.

    The whole-snapshot check misses a derived file bundled with new approved code, which
    changes the digest. For a snapshot declared public, every file that is not approved
    code must not share its sha256 with a file of another snapshot of the org that is
    recorded derived, migrated or default, or has no record. Staging the identical
    snapshot again (same digest) stays governed by the whole-snapshot rule. Empty files
    are ignored. Call after both the declaration and the code approval are recorded;
    ``entries`` and ``digest`` are needed only before the snapshot row exists (chunked
    begin). A sha256 tripwire, not a guarantee: a re-serialized file has other bytes.
    """
    from .store import Conflict

    declared = _pending(connection, snapshot_id)
    if declared is None or declared["data_class"] != PUBLIC:
        return
    malformed = Conflict(
        "This snapshot's manifest or code approval is malformed; it cannot be declared public."
    )
    try:
        if entries is None or digest is None:
            row = connection.execute(
                "SELECT manifest_json, digest FROM source_snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
            if row is None:
                return
            entries, digest = json.loads(row[0])["entries"], row[1]
        approval = connection.execute(
            "SELECT files_json FROM snapshot_code_approvals WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
        approved = (
            {f["relative_path"] for f in json.loads(approval[0])} if approval is not None else set()
        )
        digests = sorted(
            {
                e["sha256"]
                for e in entries
                if e["relative_path"] not in approved and int(e["size"]) > 0
            }
        )
    except (KeyError, TypeError, ValueError):
        # Fail closed for this snapshot only; another snapshot's bad row never blocks it.
        raise malformed from None
    if not digests:
        return
    # Other snapshots' rows are read only when they are valid JSON (json_valid), so one
    # corrupt manifest or approval in the org cannot block every public staging.
    if connection.execute(
        """SELECT 1 FROM source_snapshots s JOIN projects p ON p.id = s.project_id
        JOIN json_each(CASE WHEN json_valid(s.manifest_json) THEN s.manifest_json
                            ELSE '{}' END, '$.entries') e
        LEFT JOIN snapshot_data_classes d ON d.snapshot_id = s.id
        WHERE p.org_id = ? AND s.id != ? AND s.digest != ?
        AND """
        + NOT_PUBLIC_SQL
        + """
        AND json_extract(e.value, '$.size') > 0
        AND json_extract(e.value, '$.sha256') IN (SELECT value FROM json_each(?))
        LIMIT 1""",
        (org_id, snapshot_id, digest, json.dumps(digests)),
    ).fetchone():
        raise Conflict(
            "A file declared public has the same bytes as a file already held as derived, "
            "migrated or undeclared data; it cannot be declared public."
        )
    # A snapshot's approved code is private code, not public data: its bytes may not
    # reappear unapproved in a different snapshot declared public. Staging the identical
    # snapshot again (same digest) stays governed by the whole-snapshot rule, as above
    # (ADR 0040 ranks identical stagings: public, code-only, private).
    if connection.execute(
        """SELECT 1 FROM snapshot_code_approvals a
        JOIN source_snapshots s ON s.id = a.snapshot_id JOIN projects p ON p.id = s.project_id
        JOIN json_each(CASE WHEN json_valid(a.files_json) THEN a.files_json ELSE '[]' END) f
        WHERE p.org_id = ? AND a.snapshot_id != ? AND s.digest != ?
        AND json_extract(f.value, '$.size') > 0
        AND json_extract(f.value, '$.sha256') IN (SELECT value FROM json_each(?))
        LIMIT 1""",
        (org_id, snapshot_id, digest, json.dumps(digests)),
    ).fetchone():
        raise Conflict(
            "A file declared public has the same bytes as code approved as private in another "
            "snapshot; approve it as code instead of declaring it public data."
        )


def declare_pending(
    connection: sqlite3.Connection,
    live: dict[str, Any],
    upload_id: str,
    declaration: DataClassDeclaration | None,
    now: str,
    *,
    digest: str,
) -> None:
    """Record a chunked upload's begin-time declaration under its future snapshot id."""
    if declaration is None:
        return
    if declaration.data_class == PUBLIC:
        _require_public_actor(live)
        _refuse_relabelled_bytes(connection, live["org_id"], digest, upload_id)
    _insert(connection, live, upload_id, declaration, ORIGIN_DECLARED, now)


def discard_pending(connection: sqlite3.Connection, upload_id: str) -> None:
    """Drop the declaration of an upload that will never become a snapshot."""
    connection.execute(
        """DELETE FROM snapshot_data_classes WHERE snapshot_id = ? AND NOT EXISTS
        (SELECT 1 FROM source_snapshots WHERE id = ?)""",
        (upload_id, upload_id),
    )


def record_snapshot_data_class(
    connection: sqlite3.Connection,
    live: dict[str, Any],
    snapshot_id: str,
    declaration: DataClassDeclaration | None = None,
    *,
    derived: bool = False,
    now: str,
) -> dict[str, Any]:
    """Record the class of a snapshot just inserted, in the caller's transaction.

    The caller puts the returned record in its ``workspace.snapshot_adopted`` event.

    ``derived`` (built from run outputs or artifacts) always records private_derived and
    refuses a public declaration. A row already written at chunked-upload begin is kept;
    a different declaration at completion is refused.
    """
    from .store import Conflict, Forbidden

    if derived and declaration is not None and declaration.data_class != PRIVATE_DERIVED:
        raise Forbidden("Snapshots built from run outputs or artifacts are always private_derived.")
    existing = _pending(connection, snapshot_id)
    if existing is not None:
        if derived:
            raise Conflict("A derived snapshot cannot carry an earlier data class declaration.")
        if declaration is not None and (
            existing["origin"] != ORIGIN_DECLARED
            or (existing["data_class"], existing["license"], existing["source"])
            != (declaration.data_class, declaration.license, declaration.source)
        ):
            raise Conflict("Data class declaration differs from the one made at upload begin.")
    else:
        if declaration is not None and declaration.data_class == PUBLIC:
            _require_public_actor(live)
        origin = (
            ORIGIN_DERIVED
            if derived
            else (ORIGIN_DECLARED if declaration is not None else ORIGIN_DEFAULT)
        )
        _insert(connection, live, snapshot_id, declaration, origin, now)
    record = snapshot_data_class(connection, snapshot_id)
    if record["data_class"] == PUBLIC:
        row = connection.execute(
            "SELECT digest FROM source_snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        _refuse_relabelled_bytes(connection, live["org_id"], row[0], snapshot_id)
    return record


# ---- enforcement -------------------------------------------------------------------------


def target_is_third_party(target: Any) -> bool:
    """Unknown or unregistered targets count as third-party compute (fail closed)."""
    from .execution_targets import get_target

    try:
        return bool(get_target(target).third_party_compute)
    except KeyError:
        return True


def provider_for_target(target: Any) -> str | None:
    """The trust-setting provider id of a route target; None (no route, unregistered)
    receives ``public`` only."""
    from .execution_targets import get_target

    try:
        return get_target(target).provider_id
    except KeyError:
        return None


def admit_provider_input(
    connection: sqlite3.Connection,
    target: Any,
    snapshot_id: str,
    worker_principal_id: str | None = None,
    *,
    narrowed: Any = None,
    approval_digest: str | None = None,
) -> Any:
    """ADR 0040 section 4: the provider trust check for a route target's input.

    Returns the ``provider_trust.AdmittedInput``; raises DataClassRefused naming the
    provider and worker principal. A target without a bound worker principal, an
    unregistered target or ``None`` (no route) receives ``public`` only.
    """
    from .provider_trust import provider_admits

    return provider_admits(
        connection,
        provider_for_target(target),
        worker_principal_id,
        snapshot_id,
        narrowed,
        approval_digest,
    )


def require_provider_input(
    connection: sqlite3.Connection,
    target: Any,
    snapshot_id: str,
    worker_principal_id: str | None = None,
    *,
    narrowed: Any = None,
    approval_digest: str | None = None,
) -> frozenset[str]:
    """Raise DataClassRefused unless the target's provider instance may receive this input.

    Returns the approved code paths (empty without an approval).
    """
    return admit_provider_input(
        connection,
        target,
        snapshot_id,
        worker_principal_id,
        narrowed=narrowed,
        approval_digest=approval_digest,
    ).code_paths


def require_public_input(
    connection: sqlite3.Connection,
    target: Any,
    snapshot_id: str,
    worker_principal_id: str | None = None,
) -> frozenset[str]:
    """ADR 0039 name for ``require_provider_input`` (ADR 0040 section 4)."""
    return require_provider_input(connection, target, snapshot_id, worker_principal_id)


def route_target_for_operation(
    connection: sqlite3.Connection, org_id: str, operation_id: str
) -> str | None:
    """The remote route target bound to an external operation, if it has a route."""
    row = connection.execute(
        """SELECT json_extract(payload_json, '$.proposal.selection.target') FROM events
        WHERE org_id = ? AND event_type = 'remote_execution.state'
        AND json_extract(payload_json, '$.proposal.operation_id') = ?
        ORDER BY cursor DESC LIMIT 1""",
        (org_id, operation_id),
    ).fetchone()
    return None if row is None else row[0]


def grant_input_data_class(connection: sqlite3.Connection, grant_id: str) -> DataClass:
    """Class of the snapshot an execution grant delivers as input."""
    row = connection.execute(
        "SELECT snapshot_id FROM execution_grants WHERE id = ?", (grant_id,)
    ).fetchone()
    if row is None:
        return PRIVATE_DERIVED
    return snapshot_data_class(connection, row[0])["data_class"]


def operation_worker_principal(connection: sqlite3.Connection, operation_id: str) -> str | None:
    """The worker principal that holds the route's grant (the bound provider instance)."""
    row = connection.execute(
        "SELECT worker_principal_id FROM external_operations WHERE operation_id = ?",
        (operation_id,),
    ).fetchone()
    return None if row is None else row[0]


def require_public_provider_input(
    connection: sqlite3.Connection, org_id: str, operation_id: str, snapshot_id: str
) -> Any:
    """Provider input tickets (issue and every pull): the provider of the route's target,
    bound to the operation's worker principal, must be trusted for the input. Route-less
    operations receive ``public`` only. A campaign stage's own ``send_classes`` and its
    licence-label rule apply here exactly as at release. Returns the AdmittedInput plus
    the instance."""
    target = route_target_for_operation(connection, org_id, operation_id)
    worker = operation_worker_principal(connection, operation_id)
    attempts = _campaign_attempts_for_operation(connection, operation_id)
    narrowed, approval_digest = _campaign_narrowing(connection, attempts)
    admitted = admit_provider_input(
        connection, target, snapshot_id, worker, narrowed=narrowed, approval_digest=approval_digest
    )
    provider_id = provider_for_target(target)
    _require_campaign_licence_labels(connection, attempts, snapshot_id, provider_id)
    return admitted, provider_id, worker


def _campaign_attempts_for_operation(
    connection: sqlite3.Connection, operation_id: str
) -> list[sqlite3.Row | tuple[Any, ...]]:
    """The campaign stage attempt(s) whose grant this external operation runs.

    The stage's narrowing is stored durably in the approved campaign spec (immutable and
    bound to the approval by its digest); this finds the attempt that links the operation
    to it. The attempt's ``operation_id`` is recorded only once the runner observes the
    operation, after the worker may already have asked for a ticket, so the link is, in
    order: the attempt's grant (recorded when the runner issues it, before any lease) or
    operation id; else the snapshot release the grant delivers (recorded on the attempt
    before the grant is requested), which covers a crash between issuing the grant and
    recording it. A release shared by several stages of one campaign yields every one of
    them and their narrowings intersect. An operation that no campaign attempt links to
    (a human grant, or anything recorded outside a campaign) returns nothing and keeps
    the deployment trust check alone, exactly as before stage narrowing was re-checked.
    """
    link = connection.execute(
        """SELECT o.grant_id, g.snapshot_release_id FROM external_operations o
        LEFT JOIN execution_grants g ON g.id = o.grant_id WHERE o.operation_id = ?""",
        (operation_id,),
    ).fetchone()
    grant_id, release_id = (None, None) if link is None else (link[0], link[1])
    select = """SELECT a.campaign_id, a.stage_key, a.provider_id, c.spec_json, c.org_id,
        c.project_id FROM campaign_stage_attempts a JOIN campaigns c ON c.id = a.campaign_id
        WHERE c.org_id = ? AND ("""
    # The tenant check is at data access: another organisation's attempts are never read,
    # whatever grant or release id they carry.
    org_id = connection.execute(
        "SELECT org_id FROM external_operations WHERE operation_id = ?", (operation_id,)
    ).fetchone()
    if org_id is None or org_id[0] is None:
        return []
    org = org_id[0]
    rows = connection.execute(
        select + "a.operation_id = ? OR (? IS NOT NULL AND a.grant_id = ?))",
        (org, operation_id, grant_id, grant_id),
    ).fetchall()
    if not rows and release_id is not None:
        rows = connection.execute(select + "a.release_id = ?)", (org, release_id)).fetchall()
    return rows


def _campaign_narrowing(
    connection: sqlite3.Connection, attempts: list[Any]
) -> tuple[frozenset[str] | None, str | None]:
    if not attempts:
        return None, None
    campaign_ids = {row[0] for row in attempts}
    if len(campaign_ids) != 1:  # a release is owned by one campaign; anything else: public
        return frozenset({"public"}), None
    approval = connection.execute(
        """SELECT trust_policy_digest FROM campaign_approvals WHERE campaign_id = ?
        ORDER BY (status='active') DESC, approved_at DESC, id DESC LIMIT 1""",
        (next(iter(campaign_ids)),),
    ).fetchone()
    digest = None if approval is None else approval[0]
    narrowed: frozenset[str] | None = None
    for row in attempts:
        try:
            from .campaign_models import parse_spec_json

            stage = next(item for item in parse_spec_json(row[3]).stages if item.key == row[1])
        except Exception:  # noqa: BLE001 - unreadable campaign context grants nothing beyond public
            return frozenset({"public"}), digest
        classes = getattr(stage, "send_classes", None)
        if classes:
            # Only narrows: the result is intersected with the deployment trust setting.
            narrowed = frozenset(classes) if narrowed is None else narrowed & frozenset(classes)
    return narrowed, digest


def campaign_input_narrowing(
    connection: sqlite3.Connection, operation_id: str
) -> tuple[frozenset[str] | None, str | None]:
    """A campaign stage's own ``send_classes`` and approval-time trust digest.

    Ticket issue and every pull re-apply the narrowing the runner applied at release, so a
    stage that excluded a class never receives it later. Only narrows: an operation outside
    a campaign returns ``(None, None)``. A campaign operation whose stored spec cannot be
    read fails closed to ``public`` only.
    """
    return _campaign_narrowing(
        connection, _campaign_attempts_for_operation(connection, operation_id)
    )


def _spec_stage_providers(row: Any) -> set[str]:
    """Every provider the approved spec lets this attempt's stage run on; empty if unreadable."""
    try:
        from .campaign_models import normalize, parse_spec_json

        stage = next(
            item for item in normalize(parse_spec_json(row[3])).stages if item.key == row[1]
        )
        names = stage.candidate_ids or (stage.provider,)
        return {name for name in names if name}
    except Exception:  # noqa: BLE001 - unreadable context resolves nothing (the caller refuses)
        return set()


def _require_campaign_licence_labels(
    connection: sqlite3.Connection,
    attempts: list[Any],
    snapshot_id: str,
    route_provider_id: str | None,
) -> None:
    """The campaign licence-label rule (``license_labels``) again at ticket issue and pull.

    Labels are add-only, so a label added after release (or a provider restricted since)
    stops later pulls. The labels are the stage's effective labels, computed as at release,
    plus any label recorded on the delivered snapshot itself. Checked against the route's
    provider and the provider the attempt recorded. Outside a campaign nothing changes.
    """
    if not attempts:
        return
    from . import license_labels
    from .provider_trust import current_policy

    policy = current_policy(connection)
    resolved = {p for p in (route_provider_id, *(row[2] for row in attempts)) if p}
    for row in attempts:
        if row[2] or route_provider_id:
            continue
        # A legacy row records no provider and the route names none: take the approved spec's
        # stage provider(s), as the campaign receipt does, so the guard still applies.
        fallback = _spec_stage_providers(row)
        if not fallback:
            raise DataClassRefused(
                "the provider of the campaign stage's attempt cannot be determined, so its "
                "licence-label rule cannot be applied; refused (fail closed)"
            )
        resolved |= fallback
    providers = sorted(resolved)
    restricted = [p for p in providers if policy.allowed_license_labels(p) is not None]
    if not restricted:
        return
    try:
        from .campaign_models import normalize, parse_spec_json

        labels: set[str] = set()
        for row in attempts:
            labels |= license_labels.stage_labels(
                connection,
                normalize(parse_spec_json(row[3])),
                row[1],
                org_id=row[4],
                project_id=row[5],
            )
        explicit = connection.execute(
            "SELECT 1 FROM snapshot_license_labels WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
        if explicit is not None:
            labels |= license_labels.snapshot_labels(connection, snapshot_id)
    except sqlite3.OperationalError:
        raise
    except Exception:  # noqa: BLE001 - undeterminable labels are a refusal, never an admit
        raise DataClassRefused(
            "the licence labels of the campaign stage's input cannot be determined for "
            f"provider(s) {restricted}; refused (fail closed)"
        ) from None
    for provider_id in restricted:
        reason = license_labels.license_refusal(provider_id, labels, policy)
        if reason:
            raise DataClassRefused(reason)


def operation_input_data_class(connection: sqlite3.Connection, operation_id: str) -> DataClass:
    """Class of the input snapshot of an external operation's grant."""
    row = connection.execute(
        "SELECT grant_id FROM external_operations WHERE operation_id = ?", (operation_id,)
    ).fetchone()
    if row is None:
        return PRIVATE_DERIVED
    return grant_input_data_class(connection, row[0])
