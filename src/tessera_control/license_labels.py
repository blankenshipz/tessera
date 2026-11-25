"""Licence labels on data, inherited through lineage, and the provider rule that uses them.

A snapshot may carry licence labels (lowercase slugs such as ``cc-by`` or ``non-commercial``)
that a human adds and nobody can remove, so a derived copy cannot launder one. A snapshot with
no label row is ``unlabelled``. Data derived from labelled inputs inherits their labels: the
effective labels of a stage's inputs are computed from the spec's DAG (a stage input carries the
labels of everything the upstream stage read; an artifact input carries those of its source
stage, computed the same way from the source campaign's stored spec). An input whose lineage
cannot be resolved carries ``unknown-lineage`` as well as ``unlabelled``.

The operator's provider trust file says what a provider may receive with
``allowed_license_labels``. A stage goes to a provider only if every effective label of every
input is in that list (fail closed: unlabelled data needs ``unlabelled`` in the list); a
third-party (or unregistered or unlisted) provider without the list receives only ``unlabelled``
data, while an operator-owned provider listed without it is unrestricted. A recorded stage
output keeps the labels its inputs carried (``campaign_output_license_labels``), so later
campaigns that read it inherit them.
Labels carry no experiment, dataset or vendor meaning here; the vocabulary is the operator's.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

LABEL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
UNLABELLED = "unlabelled"
UNKNOWN_LINEAGE = "unknown-lineage"
RESERVED = frozenset({UNLABELLED, UNKNOWN_LINEAGE})
MAX_LABELS = 8
MAX_DEPTH = 8
_UNRESOLVED = frozenset({UNLABELLED, UNKNOWN_LINEAGE})


def create_license_schema(connection: sqlite3.Connection) -> None:
    """Shared by the migration and fresh databases so both converge. Additive, idempotent."""
    for statement in (
        """CREATE TABLE IF NOT EXISTS snapshot_license_labels (
        snapshot_id TEXT PRIMARY KEY REFERENCES source_snapshots(id),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        labels_json TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision >= 1),
        updated_by_principal_id TEXT NOT NULL,
        updated_at TEXT NOT NULL)""",
        """CREATE TRIGGER IF NOT EXISTS snapshot_license_labels_no_delete
        BEFORE DELETE ON snapshot_license_labels
        BEGIN SELECT RAISE(ABORT, 'licence labels are permanent'); END""",
        """CREATE TRIGGER IF NOT EXISTS snapshot_license_labels_add_only
        BEFORE UPDATE ON snapshot_license_labels
        WHEN NEW.snapshot_id != OLD.snapshot_id OR NEW.org_id != OLD.org_id
            OR NEW.project_id != OLD.project_id
            OR EXISTS (
                SELECT 1 FROM json_each(OLD.labels_json)
                WHERE value NOT IN (SELECT value FROM json_each(NEW.labels_json)))
        BEGIN SELECT RAISE(ABORT, 'licence labels can only be added'); END""",
    ):
        connection.execute(statement)


def valid_label(value: Any) -> bool:
    return isinstance(value, str) and LABEL_RE.fullmatch(value) is not None


def validate_labels(labels: Iterable[Any]) -> list[str]:
    """The labels as a sorted unique list, or ValueError naming what is wrong (never a value
    longer than the slug limit)."""
    found = list(labels)
    if not found:
        raise ValueError("at least one licence label is required")
    for label in found:
        if not valid_label(label):
            raise ValueError("a licence label is a lowercase slug (a-z, 0-9, _ and -, at most 32)")
        if label in RESERVED:
            raise ValueError(f"the label {label!r} is reserved and cannot be set")
    unique = sorted(set(found))
    if len(unique) > MAX_LABELS:
        raise ValueError(f"at most {MAX_LABELS} licence labels per snapshot")
    return unique


def snapshot_labels(connection: sqlite3.Connection, snapshot_id: str) -> frozenset[str]:
    """The snapshot's labels; ``unlabelled`` when it has none."""
    row = connection.execute(
        "SELECT labels_json FROM snapshot_license_labels WHERE snapshot_id=?", (snapshot_id,)
    ).fetchone()
    if row is None:
        return frozenset({UNLABELLED})
    labels = frozenset(json.loads(row[0]))
    return labels or frozenset({UNLABELLED})


def add_snapshot_labels(
    connection: sqlite3.Connection,
    principal: Mapping[str, Any],
    snapshot_id: str,
    labels: Iterable[str],
    now: str,
) -> dict[str, Any]:
    """Add labels (never remove): ``{labels, added, revision}``. Idempotent when nothing is new.

    The snapshot must belong to the principal's organization (another organization's snapshot
    reads as not found) and, for a project-bound principal, to its project. The caller writes
    the event, in the same transaction."""
    from .store import NotFound

    requested = validate_labels(labels)
    snapshot = connection.execute(
        """SELECT s.project_id, p.org_id FROM source_snapshots s
        JOIN projects p ON p.id = s.project_id WHERE s.id=?""",
        (snapshot_id,),
    ).fetchone()
    if (
        snapshot is None
        or snapshot[1] != principal["org_id"]
        or principal.get("project_id") not in (None, snapshot[0])
    ):
        raise NotFound("Snapshot was not found.")
    row = connection.execute(
        "SELECT labels_json, revision FROM snapshot_license_labels WHERE snapshot_id=?",
        (snapshot_id,),
    ).fetchone()
    current = sorted(json.loads(row[0])) if row else []
    merged = sorted(set(current) | set(requested))
    if len(merged) > MAX_LABELS:
        raise ValueError(f"at most {MAX_LABELS} licence labels per snapshot")
    added = [label for label in merged if label not in current]
    if row is not None and not added:
        return {"labels": merged, "added": [], "revision": row[1]}
    if row is None:
        connection.execute(
            "INSERT INTO snapshot_license_labels VALUES (?,?,?,?,?,?,?)",
            (snapshot_id, snapshot[1], snapshot[0], json.dumps(merged), 1, principal["id"], now),
        )
        return {"labels": merged, "added": added, "revision": 1}
    connection.execute(
        """UPDATE snapshot_license_labels SET labels_json=?, revision=revision+1,
        updated_by_principal_id=?, updated_at=? WHERE snapshot_id=?""",
        (json.dumps(merged), principal["id"], now, snapshot_id),
    )
    return {"labels": merged, "added": added, "revision": row[1] + 1}


def _output_labels(
    connection: sqlite3.Connection, manifest_id: str, org_id: str, project_id: str
) -> frozenset[str]:
    """Explicit labels carried by a released campaign output (a custody manifest).

    The producing campaign stage is found through the attempt that collected the manifest; the
    output carries what that stage read. A manifest no campaign attempt produced adds nothing
    (it entered from outside the labelled lineage, like an unlabelled snapshot). A producing
    attempt whose campaign cannot be read or resolved adds ``unknown-lineage``."""
    from .campaign_models import normalize, parse_spec_json

    attempt = connection.execute(
        """SELECT campaign_id, stage_key FROM campaign_stage_attempts WHERE manifest_id=?
        ORDER BY created_at LIMIT 1""",
        (manifest_id,),
    ).fetchone()
    if attempt is None:
        return frozenset()
    row = connection.execute(
        "SELECT spec_json FROM campaigns WHERE id=? AND org_id=? AND project_id=?",
        (attempt[0], org_id, project_id),
    ).fetchone()
    if row is None:
        return frozenset({UNKNOWN_LINEAGE})
    try:
        spec = normalize(parse_spec_json(row[0]))
        labels = stage_labels(connection, spec, attempt[1], org_id=org_id, project_id=project_id)
    except (ValueError, TypeError, KeyError):
        return frozenset({UNKNOWN_LINEAGE})
    return labels - {UNLABELLED}


def derived_snapshot_labels(
    connection: sqlite3.Connection,
    org_id: str,
    project_id: str,
    snapshot_ids: Iterable[str],
    manifest_ids: Iterable[str],
) -> frozenset[str]:
    """The explicit labels a snapshot built from these sources inherits (their union).

    Unlabelled sources contribute nothing, because ``unlabelled`` is implicit; so a derivative
    of unlabelled data stays unlabelled and no row is written."""
    found: set[str] = set()
    for snapshot_id in snapshot_ids:
        found |= snapshot_labels(connection, snapshot_id) - {UNLABELLED}
    for manifest_id in manifest_ids:
        found |= _output_labels(connection, manifest_id, org_id, project_id)
    return frozenset(found)


def inherit_snapshot_labels(
    connection: sqlite3.Connection,
    principal: Mapping[str, Any],
    new_snapshot_id: str,
    project_id: str,
    snapshot_ids: Iterable[str],
    manifest_ids: Iterable[str],
    now: str,
) -> dict[str, Any] | None:
    """Give a new derived snapshot its sources' labels, in the caller's transaction.

    Returns the ``snapshot.license_labels_added`` event payload to record, or None when the
    sources carry no explicit label (nothing is written). The label rules for humans do not
    apply here (the computed ``unknown-lineage`` marker may be stored), but the table's add-only
    triggers do: the row is new, so only an insert happens."""
    snapshot_ids, manifest_ids = list(snapshot_ids), list(manifest_ids)
    labels = derived_snapshot_labels(
        connection, principal["org_id"], project_id, snapshot_ids, manifest_ids
    )
    if not labels:
        return None
    merged = sorted(labels)
    if len(merged) > MAX_LABELS:
        # Too many to store: keep the lineage marker so a restricted provider still refuses.
        merged = sorted(set(merged[: MAX_LABELS - 1]) | {UNKNOWN_LINEAGE})
    connection.execute(
        "INSERT INTO snapshot_license_labels VALUES (?,?,?,?,?,?,?)",
        (
            new_snapshot_id,
            principal["org_id"],
            project_id,
            json.dumps(merged),
            1,
            principal["id"],
            now,
        ),
    )
    return {
        "snapshot_id": new_snapshot_id,
        "added": merged,
        "labels": merged,
        "revision": 1,
        "inherited_from": sorted([*snapshot_ids, *manifest_ids]),
    }


def create_output_license_schema(connection: sqlite3.Connection) -> None:
    """The labels a recorded campaign stage output carries (ADR 0113 amendment 2026-11-23).

    One add-only row per campaign stage, written when the stage's outputs are first indexed.
    Shared by the migration and fresh databases. Additive, idempotent."""
    for statement in (
        """CREATE TABLE IF NOT EXISTS campaign_output_license_labels (
        campaign_id TEXT NOT NULL REFERENCES campaigns(id),
        stage_key TEXT NOT NULL,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        labels_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (campaign_id, stage_key))""",
        """CREATE TRIGGER IF NOT EXISTS campaign_output_license_labels_no_delete
        BEFORE DELETE ON campaign_output_license_labels
        BEGIN SELECT RAISE(ABORT, 'recorded output labels are permanent'); END""",
        """CREATE TRIGGER IF NOT EXISTS campaign_output_license_labels_add_only
        BEFORE UPDATE ON campaign_output_license_labels
        WHEN NEW.campaign_id != OLD.campaign_id OR NEW.stage_key != OLD.stage_key
            OR NEW.org_id != OLD.org_id OR NEW.project_id != OLD.project_id
            OR EXISTS (
                SELECT 1 FROM json_each(OLD.labels_json)
                WHERE value NOT IN (SELECT value FROM json_each(NEW.labels_json)))
        BEGIN SELECT RAISE(ABORT, 'recorded output labels can only be added'); END""",
    ):
        connection.execute(statement)


def recorded_output_labels(
    connection: sqlite3.Connection,
    campaign_id: str,
    stage_key: str,
    *,
    org_id: str,
    project_id: str,
) -> frozenset[str] | None:
    """The labels recorded for a campaign stage's outputs, or None when none were recorded."""
    row = connection.execute(
        """SELECT labels_json FROM campaign_output_license_labels
        WHERE campaign_id=? AND stage_key=? AND org_id=? AND project_id=?""",
        (campaign_id, stage_key, org_id, project_id),
    ).fetchone()
    if row is None:
        return None
    return frozenset(json.loads(row[0])) or _UNRESOLVED


def record_output_labels(
    connection: sqlite3.Connection, campaign: Mapping[str, Any], stage_key: str, now: str
) -> list[str]:
    """Record (add-only) the labels a stage's outputs carry, in the caller's transaction.

    The labels are those of every input of the stage, computed from the campaign's stored
    spec. A lineage that cannot be computed is recorded as ``unknown-lineage`` (fail closed);
    a busy database propagates for a retry. Returns the recorded labels."""
    from .campaign_models import normalize, parse_spec_json

    org_id, project_id = campaign["org_id"], campaign["project_id"]
    try:
        row = connection.execute(
            "SELECT spec_json FROM campaigns WHERE id=? AND org_id=? AND project_id=?",
            (campaign["id"], org_id, project_id),
        ).fetchone()
        if row is None:
            raise ValueError("campaign not found")
        spec = normalize(parse_spec_json(row[0]))
        labels = set(
            stage_labels(connection, spec, stage_key, org_id=org_id, project_id=project_id)
        )
    except sqlite3.OperationalError:
        raise
    except Exception:  # noqa: BLE001 - an unreadable lineage is recorded as unknown, never clean
        labels = set(_UNRESOLVED)
    current = recorded_output_labels(
        connection, campaign["id"], stage_key, org_id=org_id, project_id=project_id
    )
    if current is None:
        merged = sorted(labels)
        connection.execute(
            "INSERT INTO campaign_output_license_labels VALUES (?,?,?,?,?,?)",
            (campaign["id"], stage_key, org_id, project_id, json.dumps(merged), now),
        )
        return merged
    merged = sorted(set(current) | labels)
    if merged != sorted(current):
        connection.execute(
            """UPDATE campaign_output_license_labels SET labels_json=?, updated_at=?
            WHERE campaign_id=? AND stage_key=?""",
            (json.dumps(merged), now, campaign["id"], stage_key),
        )
    return merged


def _stage_union(
    connection: sqlite3.Connection,
    spec: Any,
    stage_key: str,
    org_id: str,
    project_id: str,
    depth: int,
    path: frozenset[tuple[Any, str]],
    spec_id: Any,
    memo: dict[tuple[Any, str], dict[str, frozenset[str]]],
) -> dict[str, frozenset[str]]:
    """Input name -> effective labels for one stage of ``spec``.

    Stage inputs follow the spec's own (acyclic, bounded) stage graph without a depth limit;
    ``depth`` counts campaign hops (artifact inputs) only. ``path`` refuses a cycle."""
    marker = (spec_id, stage_key)
    if marker in memo:
        return memo[marker]
    try:
        stage = spec.stage(stage_key)
    except StopIteration:
        return {}
    result: dict[str, frozenset[str]] = {}
    for item in stage.inputs:
        if marker in path or depth > MAX_DEPTH:
            result[item.name] = _UNRESOLVED
            continue
        inner = path | {marker}
        if item.kind == "snapshot":
            result[item.name] = snapshot_labels(connection, item.snapshot_id)
        elif item.kind == "public_artifact":
            # Pinned public files are fetched by the provider (ADR 0097) and carry no operator
            # label: the implicit ``unlabelled``, never an unresolved lineage.
            result[item.name] = frozenset({UNLABELLED})
        elif item.kind == "stage":
            if not any(s.key == item.stage for s in spec.stages):
                result[item.name] = _UNRESOLVED
            else:
                upstream = _stage_union(
                    connection, spec, item.stage, org_id, project_id, depth, inner, spec_id, memo
                )
                result[item.name] = _flatten(upstream)
        elif item.kind == "artifact":  # another campaign's output
            result[item.name] = _artifact_labels(
                connection, item, org_id, project_id, depth, inner, memo
            )
        else:  # an input kind this guard does not know: fail closed
            result[item.name] = _UNRESOLVED
    if marker not in path:
        memo[marker] = result
    return result


def _flatten(by_input: Mapping[str, frozenset[str]]) -> frozenset[str]:
    out: set[str] = set()
    for labels in by_input.values():
        out |= labels
    return frozenset(out) or _UNRESOLVED


def _artifact_labels(
    connection: sqlite3.Connection,
    item: Any,
    org_id: str,
    project_id: str,
    depth: int,
    path: frozenset[tuple[Any, str]],
    memo: dict[tuple[Any, str], dict[str, frozenset[str]]],
) -> frozenset[str]:
    """The labels of an earlier campaign's output: recomputed from its stored spec, united with
    the labels recorded when the output was indexed. Where recomputation cannot finish (the
    campaign-hop limit) the recorded labels stand in; without them it is ``unknown-lineage``."""
    from .campaign_models import normalize, parse_spec_json

    recorded = recorded_output_labels(
        connection, item.campaign_id, item.stage, org_id=org_id, project_id=project_id
    )
    if depth + 1 > MAX_DEPTH:
        return _UNRESOLVED if recorded is None else recorded
    row = connection.execute(
        "SELECT spec_json FROM campaigns WHERE id=? AND org_id=? AND project_id=?",
        (item.campaign_id, org_id, project_id),
    ).fetchone()
    if row is None:
        return _UNRESOLVED
    try:
        source = normalize(parse_spec_json(row[0]))
        if not any(s.key == item.stage for s in source.stages):
            return _UNRESOLVED
        marker_id = item.campaign_id
        if (marker_id, item.stage) in path:
            return _UNRESOLVED
        inputs = _stage_union(
            connection,
            source,
            item.stage,
            org_id,
            project_id,
            depth + 1,
            path,
            marker_id,
            memo,
        )
    except (ValueError, TypeError, KeyError):
        return _UNRESOLVED if recorded is None else recorded
    computed = _flatten(inputs)
    if recorded is None:
        return computed
    # The recorded row is the lineage of record; recomputation adds labels given since.
    return recorded | (computed - {UNKNOWN_LINEAGE})


def effective_labels(
    connection: sqlite3.Connection, spec: Any, stage_key: str, *, org_id: str, project_id: str
) -> dict[str, frozenset[str]]:
    """Input name -> the labels that input carries, through lineage (``spec`` is a NormalSpec)."""
    return _stage_union(
        connection, spec, stage_key, org_id, project_id, 0, frozenset(), id(spec), {}
    )


def stage_labels(
    connection: sqlite3.Connection, spec: Any, stage_key: str, *, org_id: str, project_id: str
) -> frozenset[str]:
    """Every label any input of the stage carries."""
    by_input = effective_labels(connection, spec, stage_key, org_id=org_id, project_id=project_id)
    return _flatten(by_input)


def license_refusal(provider_id: str, labels: Iterable[str], policy: Any) -> str | None:
    """None when ``provider_id`` may receive data carrying ``labels``, else a clear message."""
    if policy is None:
        return None
    allowed = policy.allowed_license_labels(provider_id)
    if allowed is None:
        return None
    carried = frozenset(labels)
    bad = sorted(carried - allowed)
    if not bad:
        return None
    return (
        f"provider {provider_id!r} may receive only licence labels {sorted(allowed)}; this "
        f"stage's inputs carry {sorted(carried)} (not allowed: {bad}). Unlabelled data needs "
        f"{UNLABELLED!r} in the provider's allowed_license_labels; data whose lineage cannot "
        f"be resolved carries {UNKNOWN_LINEAGE!r}."
    )
