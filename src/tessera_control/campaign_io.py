"""Persistent stage I/O index and cross-campaign artifact inputs (ADR 0040 section 3).

``campaign_stage_io`` names every resolved input and output of an attempt: org, project,
campaign, stage, attempt, direction, name, kind, media type, sha256, size, the snapshot or
(manifest, path), the platform's recorded data class and the lineage node. Rows are written
in the runner transaction that marks the input ready or the attempt succeeded, and triggers
refuse update and delete. The bytes stay in snapshots and retained manifests; the index
only names them. No class is ever upgraded: outputs are ``private_derived`` and an
``artifact`` input records the platform's class for the pinned row, never the spec's claim.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .campaign_receipt_read import load_verified_receipt
from .data_class import OUTPUT_DATA_CLASS, PRIVATE_DERIVED
from .store import Conflict

# The shape of ``campaign_models.PinnedImage`` (a built image must satisfy the same rule).
_PINNED_IMAGE = re.compile(r"[a-z0-9][a-z0-9._/:-]{0,254}@sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class IoEntry:
    name: str
    kind: str
    sha256: str
    size_bytes: int
    data_class: str
    media_type: str | None = None
    snapshot_id: str | None = None
    manifest_id: str | None = None
    path: str | None = None
    lineage_node_id: str | None = None


@dataclass(frozen=True)
class ResolvedArtifact:
    """One pinned output of an earlier campaign, ready to compose as a manifest source."""

    name: str
    manifest_id: str
    manifest_digest: str
    path: str
    sha256: str
    size_bytes: int
    data_class: str
    kind: str
    media_type: str | None
    source_campaign_id: str
    source_stage: str
    source_attempt_id: str
    lineage_node_id: str | None


def write_io(
    connection: sqlite3.Connection,
    campaign: Mapping[str, Any],
    stage_key: str,
    attempt_id: str | None,
    direction: str,
    entries: Sequence[IoEntry],
    now: str,
) -> int:
    """Append index rows for one (attempt, direction); a repeat writes nothing.

    Called inside the transaction that advances the attempt, so the rows and the state
    change commit together or not at all.
    """
    existing = connection.execute(
        """SELECT name FROM campaign_stage_io WHERE campaign_id=? AND stage_key=?
        AND attempt_id IS ? AND direction=?""",
        (campaign["id"], stage_key, attempt_id, direction),
    ).fetchall()
    seen = {row["name"] for row in existing}
    written = 0
    for entry in entries:
        if entry.name in seen:
            continue
        connection.execute(
            """INSERT INTO campaign_stage_io (id, org_id, project_id, campaign_id, stage_key,
            attempt_id, direction, name, kind, media_type, sha256, size_bytes, snapshot_id,
            manifest_id, path, data_class, lineage_node_id, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "stageio_" + uuid.uuid4().hex,
                campaign["org_id"],
                campaign["project_id"],
                campaign["id"],
                stage_key,
                attempt_id,
                direction,
                entry.name,
                entry.kind,
                entry.media_type,
                entry.sha256,
                entry.size_bytes,
                entry.snapshot_id,
                entry.manifest_id,
                entry.path,
                entry.data_class,
                entry.lineage_node_id,
                now,
            ),
        )
        written += 1
    if direction == "output" and written:
        # ADR 0113 (amendment 2026-11-23): the outputs keep the labels their stage read, so a
        # later campaign reading them inherits those labels whatever the lineage depth.
        from .license_labels import record_output_labels

        record_output_labels(connection, campaign, stage_key, now)
    return written


def lineage_node(connection: sqlite3.Connection, project_id: str, sha256: str) -> str | None:
    row = connection.execute(
        "SELECT id FROM artifact_lineage_nodes WHERE project_id=? AND sha256=?",
        (project_id, sha256),
    ).fetchone()
    return None if row is None else row["id"]


def output_entries(
    connection: sqlite3.Connection,
    campaign: Mapping[str, Any],
    stage: Any,
    manifest_id: str,
) -> list[IoEntry]:
    """Index entries for the declared outputs a stage's manifest actually contains."""
    row = connection.execute(
        "SELECT artifacts_json FROM artifact_manifests WHERE id=?", (manifest_id,)
    ).fetchone()
    listed = (
        {} if row is None else {a["relative_path"]: a for a in json.loads(row["artifacts_json"])}
    )
    entries = []
    for declared in stage.outputs:
        item = listed.get(declared.path)
        if item is None:
            continue
        entries.append(
            IoEntry(
                name=declared.name,
                kind=declared.kind,
                media_type=declared.media_type,
                sha256=item["sha256"],
                size_bytes=int(item["size"]),
                data_class=OUTPUT_DATA_CLASS,
                manifest_id=manifest_id,
                path=declared.path,
                lineage_node_id=lineage_node(connection, campaign["project_id"], item["sha256"]),
            )
        )
    return entries


def stage_io(
    connection: sqlite3.Connection, campaign_id: str, attempt_id: str | None = None
) -> list[dict[str, Any]]:
    """The index rows of a campaign (or one attempt), oldest first."""
    sql = "SELECT * FROM campaign_stage_io WHERE campaign_id=?"
    params: tuple = (campaign_id,)
    if attempt_id is not None:
        sql += " AND attempt_id=?"
        params = (campaign_id, attempt_id)
    return [dict(r) for r in connection.execute(sql + " ORDER BY created_at, id", params)]


def _refuse(source: Any, why: str) -> Conflict:
    return Conflict(f"Artifact input {source.name!r} does not resolve: {why}.")


def resolve_artifact(
    connection: sqlite3.Connection, campaign: Mapping[str, Any], source: Any
) -> ResolvedArtifact:
    """Resolve a pinned ``artifact`` input or raise Conflict (409).

    It resolves only to an output row of the same org and project, from a succeeded attempt
    that the source campaign's terminal receipt lists, whose manifest is custody-released
    and ``retain_quarantined`` and holds a file with that sha256. Anything else is refused.
    It needs the source receipt's v2 ``io`` list, so a campaign that finished before schema
    v65 (a v1 receipt) can never be an artifact source.
    """
    row = connection.execute(
        """SELECT io.*, a.status AS attempt_status, a.attempt AS attempt_number,
        a.manifest_digest AS attempt_manifest_digest
        FROM campaign_stage_io io JOIN campaign_stage_attempts a ON a.id = io.attempt_id
        WHERE io.org_id=? AND io.project_id=? AND io.campaign_id=? AND io.stage_key=?
        AND io.direction='output' AND io.name=? AND io.sha256=?""",
        (
            campaign["org_id"],
            campaign["project_id"],
            source.campaign_id,
            source.stage,
            source.output,
            source.sha256,
        ),
    ).fetchone()
    if row is None:
        raise _refuse(source, "no such output in this project")
    # Only receipt-output inputs carry resolved_from today; revisit this lineage guard if
    # another input kind gains a resolved_from attribute.
    # The source receipt is used only when its digest verifies (ReceiptDigestMismatch, 409).
    verified = load_verified_receipt(connection, source.campaign_id)
    lineage = getattr(source, "resolved_from", None)
    if lineage is not None:
        if verified is None or verified.digest != lineage.source_receipt_sha256:
            raise _refuse(source, "resolved_from does not match the source campaign receipt")
    if row["attempt_status"] != "succeeded":
        raise _refuse(source, "the producing attempt did not succeed")
    if verified is None:
        raise _refuse(source, "the source campaign has no terminal receipt")
    receipt = verified.receipt
    listed = False
    for stage in receipt.get("stages", []):
        if stage.get("stage_key") != source.stage:
            continue
        for attempt in stage.get("attempts", []):
            if attempt.get("attempt") == row["attempt_number"] and attempt.get("status") == (
                "succeeded"
            ):
                listed = any(
                    item.get("direction") == "output"
                    and item.get("name") == source.output
                    and item.get("sha256") == source.sha256
                    for item in attempt.get("io", [])
                )
    if not listed:
        raise _refuse(source, "the source campaign receipt does not list it")
    manifest = connection.execute(
        "SELECT * FROM artifact_manifests WHERE id=?", (row["manifest_id"],)
    ).fetchone()
    if manifest is None or manifest["project_id"] != campaign["project_id"]:
        raise _refuse(source, "its manifest is not in this project")
    disposition = connection.execute(
        "SELECT * FROM artifact_dispositions WHERE manifest_id=? ORDER BY revision DESC LIMIT 1",
        (manifest["id"],),
    ).fetchone()
    release = connection.execute(
        "SELECT * FROM artifact_custody_releases WHERE manifest_id=? "
        "ORDER BY revision DESC LIMIT 1",
        (manifest["id"],),
    ).fetchone()
    if (
        disposition is None
        or disposition["disposition"] != "retain_quarantined"
        or release is None
        or release["state"] != "released"
        or release["manifest_digest"] != manifest["manifest_digest"]
    ):
        raise _refuse(source, "its manifest is not custody-released and retained")
    files = {a["relative_path"]: a for a in json.loads(manifest["artifacts_json"])}
    item = files.get(row["path"])
    if item is None or item["sha256"] != source.sha256:
        raise _refuse(source, "the manifest holds no file with that sha256")
    return ResolvedArtifact(
        name=source.name,
        manifest_id=manifest["id"],
        manifest_digest=manifest["manifest_digest"],
        path=row["path"],
        sha256=source.sha256,
        size_bytes=int(item["size"]),
        # The platform's recorded class for the pinned row, never relabelled; a spec that
        # declares private_derived can only keep it at least that restrictive.
        data_class=(
            PRIVATE_DERIVED
            if getattr(source, "data_class", None) == PRIVATE_DERIVED
            else row["data_class"]
        ),
        kind=row["kind"],
        media_type=row["media_type"],
        source_campaign_id=source.campaign_id,
        source_stage=source.stage,
        source_attempt_id=row["attempt_id"],
        lineage_node_id=row["lineage_node_id"],
    )


def _receipt_ref_refusal(stage_key: str, ref: Mapping[str, Any], why: str) -> Conflict:
    return Conflict(f"Stage {stage_key!r} receipt_output input {ref['name']!r}: {why}.")


def _source_receipt_output(
    receipt: Mapping[str, Any], stage: str, output: str
) -> tuple[str, int] | None:
    """The sha256 and attempt of the last succeeded attempt's named output, or None."""
    found: tuple[str, int] | None = None
    for item in receipt.get("stages", []):
        if item.get("stage_key") != stage:
            continue
        for attempt in item.get("attempts", []):
            if attempt.get("status") != "succeeded":
                continue
            for io in attempt.get("io", []):
                if io.get("direction") == "output" and io.get("name") == output:
                    found = (io["sha256"], attempt["attempt"])
    return found


def refuse_submitted_lineage(spec_value: Any, *, allow_image_lineage: bool = False) -> None:
    """Refuse any submitted ``resolved_from`` (ADR 0080): only submit's resolver sets it.

    Call this on the raw request spec, before ``resolve_receipt_refs`` adds the lineage. A re-run
    replays the platform's own stored spec, so it may carry the image lineage submit resolved
    (``allow_image_lineage``); an input's ``resolved_from`` is refused as before.
    """
    stages = spec_value.get("stages", []) if isinstance(spec_value, Mapping) else []
    for stage in stages if isinstance(stages, list) else []:
        if (
            not allow_image_lineage
            and isinstance(stage, Mapping)
            and "image_resolved_from" in stage
        ):
            raise Conflict(
                f"Stage {stage.get('key', '?')!r}: image_resolved_from is set by submit only; "
                "remove it or use an image_from reference."
            )
        inputs = stage.get("inputs", []) if isinstance(stage, Mapping) else []
        for item in inputs if isinstance(inputs, list) else []:
            if isinstance(item, Mapping) and "resolved_from" in item:
                raise Conflict(
                    f"Stage {stage.get('key', '?')!r} input {item.get('name', '?')!r}: "
                    "resolved_from is set by submit only; remove it or use a receipt_output "
                    "reference."
                )


def resolve_receipt_refs(
    connection: sqlite3.Connection,
    project: Mapping[str, Any],
    spec_value: Mapping[str, Any],
    predecessors: Mapping[str, str],
) -> dict[str, Any]:
    """Replace every ``receipt_output`` input of a spec (as a JSON mapping) by a pinned one.

    The source campaign must be in this org and project, ``succeeded``, with a terminal
    receipt that lists a succeeded attempt output of that name; the resulting ``artifact``
    input (campaign id, sha256, ``resolved_from`` lineage with the receipt digest) is then
    verified by ``resolve_artifact`` exactly like a hand-written one. Anything else is a
    Conflict that names the stage, the input and the reason. Specs without references are
    returned unchanged.
    """
    stages = spec_value.get("stages", [])
    if not any(
        isinstance(i, dict) and i.get("kind") == "receipt_output"
        for stage in stages
        for i in stage.get("inputs", [])
    ):
        return dict(spec_value)
    resolved = json.loads(json.dumps(spec_value))
    for stage in resolved["stages"]:
        inputs = []
        for item in stage.get("inputs", []):
            if item.get("kind") != "receipt_output":
                inputs.append(item)
                continue
            key = stage.get("key", "?")
            campaign_id = item.get("campaign_id")
            if campaign_id is None:
                campaign_id = predecessors.get(item.get("predecessor"))
                if campaign_id is None:
                    raise _receipt_ref_refusal(
                        key, item, f"predecessor {item.get('predecessor')!r} is not in the submit"
                    )
            row = connection.execute(
                "SELECT status FROM campaigns WHERE id=? AND org_id=? AND project_id=?",
                (campaign_id, project["org_id"], project["project_id"]),
            ).fetchone()
            if row is None:
                raise _receipt_ref_refusal(key, item, "no such campaign in this project")
            if row["status"] != "succeeded":
                raise _receipt_ref_refusal(
                    key, item, f"campaign {campaign_id} is {row['status']}, not succeeded"
                )
            verified = load_verified_receipt(connection, campaign_id)
            if verified is None:
                raise _receipt_ref_refusal(key, item, "the source campaign has no terminal receipt")
            found = _source_receipt_output(verified.receipt, item["stage"], item["output"])
            if found is None:
                raise _receipt_ref_refusal(
                    key,
                    item,
                    f"the receipt of {campaign_id} lists no succeeded output "
                    f"{item['output']!r} of stage {item['stage']!r}",
                )
            lineage: dict[str, Any] = {"source_receipt_sha256": verified.digest}
            if item.get("predecessor") is not None:
                lineage["predecessor"] = item["predecessor"]
            inputs.append(
                {
                    "kind": "artifact",
                    "name": item["name"],
                    "campaign_id": campaign_id,
                    "stage": item["stage"],
                    "output": item["output"],
                    "sha256": found[0],
                    "resolved_from": lineage,
                }
            )
        stage["inputs"] = inputs
    return resolved


def _image_ref_refusal(stage_key: str, why: str) -> Conflict:
    return Conflict(f"Stage {stage_key!r} image_from: {why}.")


def _built_image(receipt: Mapping[str, Any], stage: str) -> str | None:
    """The provider-reported image a succeeded build stage's receipt entry lists, or None.

    Reads the succeeded attempt's ``build`` defensively: any other shape (absent, declared
    only, no digest reference) is None, so a receipt that does not prove the build refuses the
    reference."""
    for item in receipt.get("stages", []):
        if not isinstance(item, Mapping) or item.get("stage_key") != stage:
            continue
        for attempt in item.get("attempts", []):
            if not isinstance(attempt, Mapping) or attempt.get("status") != "succeeded":
                continue
            build = attempt.get("build")
            if not isinstance(build, Mapping) or build.get("evidence") != "provider_reported":
                continue
            image = build.get("produced_image")
            if isinstance(image, str) and _PINNED_IMAGE.fullmatch(image):
                return image
        return None
    return None


def resolve_image_refs(
    connection: sqlite3.Connection,
    project: Mapping[str, Any],
    spec_value: Mapping[str, Any],
    predecessors: Mapping[str, str],
) -> tuple[dict[str, Any], set[str]]:
    """Replace every stage ``image_from`` (ADR 0123) by the literal image a build receipt reports.

    The source campaign must be in this org and project and ``succeeded``; its spec's stage must
    be a build stage; its terminal receipt's entry for it must carry a ``provider_reported``
    ``produced_image`` that is a pinned reference. The literal ``image`` and ``image_resolved_from``
    (campaign, stage, receipt digest) are written into the spec before it is digested. Producing
    an image never makes it runnable: dispatch still checks the resolved image against the
    operator's worker ``--allowed-image`` and Salad profile allowlists. Returns the spec and the
    predecessor names used; specs without references are returned unchanged."""
    stages = spec_value.get("stages", [])
    if not any(isinstance(s, dict) and s.get("image_from") for s in stages):
        return dict(spec_value), set()
    resolved = json.loads(json.dumps(spec_value))
    used: set[str] = set()
    for stage in resolved["stages"]:
        ref = stage.get("image_from")
        if not ref:
            continue
        key = stage.get("key", "?")
        campaign_id = ref.get("campaign_id")
        if campaign_id is None:
            campaign_id = predecessors.get(ref.get("predecessor"))
            if campaign_id is None:
                raise _image_ref_refusal(
                    key, f"predecessor {ref.get('predecessor')!r} is not in the submit"
                )
            used.add(ref["predecessor"])
        row = connection.execute(
            "SELECT status, spec_json FROM campaigns WHERE id=? AND org_id=? AND project_id=?",
            (campaign_id, project["org_id"], project["project_id"]),
        ).fetchone()
        if row is None:
            raise _image_ref_refusal(key, "no such campaign in this project")
        if row["status"] != "succeeded":
            raise _image_ref_refusal(
                key, f"campaign {campaign_id} is {row['status']}, not succeeded"
            )
        source = next(
            (
                s
                for s in json.loads(row["spec_json"]).get("stages", [])
                if s.get("key") == ref["stage"]
            ),
            None,
        )
        if source is None or not source.get("build"):
            raise _image_ref_refusal(
                key, f"stage {ref['stage']!r} of {campaign_id} is not an image-build stage"
            )
        verified = load_verified_receipt(connection, campaign_id)
        if verified is None:
            raise _image_ref_refusal(key, "the source campaign has no terminal receipt")
        image = _built_image(verified.receipt, ref["stage"])
        if image is None:
            raise _image_ref_refusal(
                key,
                f"the receipt of {campaign_id} reports no provider-reported produced image for "
                f"stage {ref['stage']!r}",
            )
        stage.pop("image_from")
        stage["image"] = image
        stage["image_resolved_from"] = {
            "campaign_id": campaign_id,
            "stage": ref["stage"],
            "source_receipt_sha256": verified.digest,
        }
    return resolved, used
