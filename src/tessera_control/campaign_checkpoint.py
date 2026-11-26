"""A checkpoint salvaged from an interrupted Salad attempt, handed to the retry.

When an attempt ends ``salad_interrupted`` (or at its route deadline) and custody already
holds a complete checkpoint of it (the stage's declared ``checkpoint`` output, imported through
the existing custody channel), the runner records ``interrupted_checkpointed`` with that file's
sha256 and size. The next attempt of the stage receives it as one more digest-bound stage input
under the same declared name. Nothing else from the interrupted attempt is accepted: its
verdict is never ``ok`` and its other outputs stay unrecorded. A stage with no salvaged
checkpoint starts its retry clean.

The recorded event is the only source the retry reads, scoped to the campaign's project, so a
checkpoint is never inferred and never taken from another stage, campaign or tenant.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from typing import Any

from .late_bound_inputs import UNLABELLED, ResolvedStageInput

# The workload contract's reserved checkpoint output file (ADR 0198:
# ``salad_workload_contract.CHECKPOINT_OUTPUT_NAME``). A stage declares it as the output whose
# path is this name; custody keeps only the newest complete upload of it under one operation, so
# a manifest lists at most one entry for it. Import the constant from the contract once that
# lane is merged.
CHECKPOINT_PATH = "checkpoint.bin"
CHECKPOINT_EVENT = "campaign.interrupted_checkpointed"
# Recorded when a retry's composed input snapshot was built with the checkpoint in it.
COMPOSED_EVENT = "campaign.checkpoint_snapshot_composed"
# A checkpoint is derived from the stage's own inputs: the most restrictive class.
CHECKPOINT_DATA_CLASS = "private_derived"
_SHA256 = re.compile(r"[0-9a-f]{64}")


def salvaged_checkpoint(
    connection: sqlite3.Connection,
    campaign: Mapping[str, Any],
    spec_stage: Any,
    attempt: Mapping[str, Any],
) -> ResolvedStageInput | None:
    """The attempt's custody-held checkpoint, or None. Read-only and fail closed: no declared
    ``checkpoint`` output, no custody manifest bound to this attempt's own grant, lease,
    operation and epoch, a malformed entry, or one over the declared size is None."""
    output = next((o for o in spec_stage.outputs if o.path == CHECKPOINT_PATH), None)
    if output is None:
        return None
    row = connection.execute(
        """SELECT sa.manifest_id, sa.lease_id, sa.operation_id, sa.epoch FROM
        campaign_salad_attempts sa JOIN campaign_stage_attempts a ON a.id = sa.attempt_id
        WHERE sa.attempt_id=? AND a.campaign_id=?""",
        (attempt["id"], campaign["id"]),
    ).fetchone()
    if row is None or row[0] is None or attempt["grant_id"] is None:
        return None
    manifest = connection.execute(
        """SELECT project_id, artifacts_json FROM artifact_manifests WHERE id=? AND grant_id=?
        AND lease_id=? AND operation_id=? AND epoch=?""",
        (row[0], attempt["grant_id"], row[1], row[2], row[3]),
    ).fetchone()
    if manifest is None or manifest[0] != campaign["project_id"]:
        return None
    try:
        listed = next(
            (a for a in json.loads(manifest[1]) if a.get("relative_path") == output.path), None
        )
        digest, size = listed["sha256"], listed["size"]
    except (TypeError, KeyError, ValueError):
        return None
    if (
        not isinstance(digest, str)
        or not _SHA256.fullmatch(digest)
        or type(size) is not int
        or not 0 < size <= output.max_bytes
    ):
        return None
    from .license_labels import derived_snapshot_labels

    labels = tuple(
        sorted(
            derived_snapshot_labels(
                connection, campaign["org_id"], campaign["project_id"], (), (row[0],)
            )
        )
    ) or (UNLABELLED,)
    return ResolvedStageInput(
        name=output.name,
        source_stage=attempt["stage_key"],
        output=output.name,
        digest=digest,
        bytes=size,
        data_class=CHECKPOINT_DATA_CLASS,
        license_labels=labels,
        path=output.path,
        manifest_id=row[0],
    )


def retry_checkpoint(
    connection: sqlite3.Connection, campaign: Mapping[str, Any], stage_key: str
) -> ResolvedStageInput | None:
    """The checkpoint for the next attempt (see ``retry_checkpoint_from``), else None."""
    found = retry_checkpoint_from(connection, campaign, stage_key)
    return None if found is None else found[0]


def retry_checkpoint_from(
    connection: sqlite3.Connection, campaign: Mapping[str, Any], stage_key: str
) -> tuple[ResolvedStageInput, int] | None:
    """The checkpoint recorded for the attempt immediately before the one being (or about to
    be) run, else None.

    The preceding attempt is the highest-numbered one that already ended with a failure class.
    A checkpoint of an earlier attempt is never resumed once a later attempt failed for another
    reason: that later run may have trained past, or away from, it, and a silent resume from a
    stale file is worse than a clean start."""
    last = connection.execute(
        """SELECT max(attempt) FROM campaign_stage_attempts
        WHERE campaign_id=? AND stage_key=? AND failure_class IS NOT NULL""",
        (campaign["id"], stage_key),
    ).fetchone()
    if last is None or last[0] is None:
        return None
    found = connection.execute(
        """SELECT payload_json FROM events WHERE project_id=? AND event_type=?
        AND json_extract(payload_json, '$.campaign_id') = ?
        AND json_extract(payload_json, '$.stage_key') = ?
        AND json_extract(payload_json, '$.attempt') = ?
        ORDER BY rowid DESC LIMIT 1""",
        (campaign["project_id"], CHECKPOINT_EVENT, campaign["id"], stage_key, last[0]),
    ).fetchone()
    if found is None:
        return None
    try:
        return ResolvedStageInput.from_json(json.loads(found[0])["input"]), int(last[0])
    except (KeyError, TypeError, ValueError):
        return None


def recorded_checkpoints(
    connection: sqlite3.Connection, campaign: Mapping[str, Any], stage_key: str
) -> list[dict[str, Any]]:
    """Every checkpoint recorded for the stage (receipt evidence), oldest first: the resolved
    input plus the attempt that produced it. Binding for a retry uses ``retry_checkpoint``."""
    rows = connection.execute(
        """SELECT payload_json FROM events WHERE project_id=? AND event_type=?
        AND json_extract(payload_json, '$.campaign_id') = ?
        AND json_extract(payload_json, '$.stage_key') = ? ORDER BY rowid""",
        (campaign["project_id"], CHECKPOINT_EVENT, campaign["id"], stage_key),
    ).fetchall()
    found: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row[0])
            entry = ResolvedStageInput.from_json(payload["input"]).to_json()
            found.append(
                {**entry, "source": "interrupted_checkpointed", "attempt": payload["attempt"]}
            )
        except (KeyError, TypeError, ValueError):
            continue
    return found


def composed_checkpoint(
    connection: sqlite3.Connection,
    campaign: Mapping[str, Any],
    stage_key: str,
    snapshot_id: str | None,
) -> ResolvedStageInput | None:
    """The checkpoint the stage's CURRENT input snapshot was composed with, else None (the
    snapshot is the base composition). Read from the event recorded when it was composed."""
    if snapshot_id is None:
        return None
    found = connection.execute(
        """SELECT payload_json FROM events WHERE project_id=? AND event_type=?
        AND json_extract(payload_json, '$.campaign_id') = ?
        AND json_extract(payload_json, '$.stage_key') = ?
        AND json_extract(payload_json, '$.snapshot_id') = ?
        ORDER BY rowid DESC LIMIT 1""",
        (campaign["project_id"], COMPOSED_EVENT, campaign["id"], stage_key, snapshot_id),
    ).fetchone()
    if found is None:
        return None
    try:
        return ResolvedStageInput.from_json(json.loads(found[0])["input"])
    except (KeyError, TypeError, ValueError):
        return None


def ever_composed(
    connection: sqlite3.Connection, campaign: Mapping[str, Any], stage_key: str
) -> bool:
    """Whether any attempt of the stage was composed with a checkpoint (so a clean retry must
    restore the base composition)."""
    return (
        connection.execute(
            """SELECT 1 FROM events WHERE project_id=? AND event_type=?
            AND json_extract(payload_json, '$.campaign_id') = ?
            AND json_extract(payload_json, '$.stage_key') = ? LIMIT 1""",
            (campaign["project_id"], COMPOSED_EVENT, campaign["id"], stage_key),
        ).fetchone()
        is not None
    )
