"""Late binding of in-campaign stage-output inputs (ADR 0140).

A stage input of kind ``stage`` names an output of an earlier stage of the same campaign. Its
content digest does not exist when the spec is submitted, so it is bound when the dependent
stage starts: each referenced output is resolved to the digest and size the upstream stage's
succeeded, settled attempt recorded in the stage I/O index, checked against the upstream
custody manifest, and pinned for the dependent stage. Anything else fails closed with one of
the closed refusal codes in ``REFUSAL_CODES``.

Data-class and licence routing is re-checked after resolution from the resolved artifacts'
own recorded class and inherited licence labels (``check_resolved_routing``), so a provider
the trust setting does not admit for that data never receives it.

Providers receive only resolved digests: the runner records the bound tuple on the
``campaign.stage_inputs_bound`` event under ``LAUNCH_INPUTS_KEY`` and exposes it through
``RunnerServices.bound_inputs``; ``bound_inputs`` here reads it back for a stage. No vendor
is named in this module.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

# The key under which a stage's launch inputs carry the resolved tuple (event payload,
# receipt ``input`` section, provider launch inputs).
LAUNCH_INPUTS_KEY = "late_bound_inputs"
BOUND_EVENT = "campaign.stage_inputs_bound"
REFUSED_EVENT = "campaign.stage_input_bind_refused"
UNLABELLED = "unlabelled"

REFUSAL_CODES = frozenset(
    {
        "campaign_not_found",  # the campaign is not in the caller's org and project
        "upstream_missing",  # the producing stage is not part of the campaign
        "upstream_not_succeeded",  # the producing stage ended without success
        "upstream_not_settled",  # the producing stage has not finished and settled yet
        "output_missing",  # the succeeded attempt did not record the declared output
        "digest_mismatch",  # recorded digests disagree, or differ from an earlier binding
        "data_class_refused",  # the provider instance may not receive the resolved class
        "license_refused",  # the provider may not receive the resolved licence labels
    }
)

_RUNNING = frozenset({"pending", "preparing", "running"})


class LateBindRefused(Exception):  # noqa: N818 - a refusal, named like LicenseRefused
    """A stage-output input could not be bound; ``code`` is one of ``REFUSAL_CODES``."""

    def __init__(self, code: str, detail: str = "", *, input_name: str | None = None) -> None:
        if code not in REFUSAL_CODES:
            raise ValueError(f"unknown late-bind refusal code {code!r}")
        self.code = code
        self.detail = detail[:300]
        self.input_name = input_name
        super().__init__(f"{code}: {self.detail}" if self.detail else code)


@dataclass(frozen=True)
class ResolvedStageInput:
    """One upstream output bound for a dependent stage.

    ``name`` is the dependent stage's input name, ``source_stage`` and ``output`` the producing
    stage and its declared output name, ``digest`` and ``bytes`` the recorded sha256 and size,
    ``data_class`` the recorded class of the output row and ``license_labels`` the labels it
    inherits (``("unlabelled",)`` when none). ``path`` is the output's path inside the input
    directory and ``manifest_id`` the custody manifest holding the bytes."""

    name: str
    source_stage: str
    output: str
    digest: str
    bytes: int
    data_class: str
    license_labels: tuple[str, ...]
    path: str = ""
    manifest_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source_stage": self.source_stage,
            "output": self.output,
            "digest": self.digest,
            "bytes": self.bytes,
            "data_class": self.data_class,
            "license_labels": list(self.license_labels),
            "path": self.path,
            "manifest_id": self.manifest_id,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> ResolvedStageInput:
        return cls(
            name=str(value["name"]),
            source_stage=str(value["source_stage"]),
            output=str(value["output"]),
            digest=str(value["digest"]),
            bytes=int(value["bytes"]),
            data_class=str(value["data_class"]),
            license_labels=tuple(value["license_labels"]),
            path=str(value.get("path") or ""),
            manifest_id=value.get("manifest_id"),
        )


def launch_payload(resolved: Iterable[ResolvedStageInput]) -> dict[str, Any]:
    """The launch-input mapping a provider receives: ``{LAUNCH_INPUTS_KEY: [...]}``."""
    return {LAUNCH_INPUTS_KEY: [item.to_json() for item in resolved]}


def _refuse(code: str, item: Any, why: str) -> LateBindRefused:
    return LateBindRefused(
        code, f"input {item.name!r} from stage {item.stage!r}: {why}", input_name=item.name
    )


def resolve_stage_inputs(
    connection: sqlite3.Connection,
    campaign_id: str,
    stage: Any,
    *,
    org_id: str,
    project_id: str,
    expected: Mapping[tuple[str, str], str] | None = None,
) -> tuple[ResolvedStageInput, ...]:
    """Resolve every ``stage`` input of ``stage`` (a ``NormalStage``) or raise LateBindRefused.

    Reads only rows of ``campaign_id`` in ``org_id``/``project_id`` (the tenant check is in
    every query), inside the caller's transaction. The producing stage must have succeeded and
    its last succeeded attempt must be settled; each referenced output must be in that
    attempt's output index and its custody manifest with the same sha256 and size, and must
    equal ``expected[(input name, output name)]`` when given (a retry keeps the first binding).
    Returns the bindings sorted by input name, then output."""
    from .campaign_models import normalize, parse_spec_json
    from .license_labels import derived_snapshot_labels

    row = connection.execute(
        "SELECT spec_json FROM campaigns WHERE id=? AND org_id=? AND project_id=?",
        (campaign_id, org_id, project_id),
    ).fetchone()
    if row is None:
        raise LateBindRefused("campaign_not_found", f"campaign {campaign_id!r} is not in scope")
    spec = normalize(parse_spec_json(row[0]))
    expected = expected or {}
    resolved: list[ResolvedStageInput] = []
    for item in sorted(stage.inputs, key=lambda i: i.name):
        if item.kind != "stage":
            continue
        upstream = next((s for s in spec.stages if s.key == item.stage), None)
        if upstream is None:
            raise _refuse("upstream_missing", item, "no such stage in this campaign")
        stage_row = connection.execute(
            """SELECT s.status FROM campaign_stages s JOIN campaigns c ON c.id=s.campaign_id
            WHERE s.campaign_id=? AND s.stage_key=? AND c.org_id=? AND c.project_id=?""",
            (campaign_id, item.stage, org_id, project_id),
        ).fetchone()
        if stage_row is None:
            raise _refuse("upstream_missing", item, "the stage has no record")
        if stage_row[0] in _RUNNING:
            raise _refuse("upstream_not_settled", item, f"the stage is {stage_row[0]}")
        if stage_row[0] != "succeeded":
            raise _refuse("upstream_not_succeeded", item, f"the stage is {stage_row[0]}")
        attempt = connection.execute(
            """SELECT id, manifest_id, manifest_digest, settled_cents, settlement_basis
            FROM campaign_stage_attempts WHERE campaign_id=? AND stage_key=?
            AND status='succeeded' ORDER BY attempt DESC LIMIT 1""",
            (campaign_id, item.stage),
        ).fetchone()
        if attempt is None:
            raise _refuse("upstream_not_succeeded", item, "no attempt succeeded")
        if attempt[3] is None or attempt[4] is None:
            raise _refuse("upstream_not_settled", item, "the succeeded attempt is not settled")
        if attempt[1] is None:
            raise _refuse("output_missing", item, "the succeeded attempt collected no manifest")
        manifest = connection.execute(
            "SELECT project_id, manifest_digest, artifacts_json FROM artifact_manifests WHERE id=?",
            (attempt[1],),
        ).fetchone()
        if manifest is None or manifest[0] != project_id:
            raise _refuse("output_missing", item, "the custody manifest is not in this project")
        if attempt[2] is not None and attempt[2] != manifest[1]:
            raise _refuse("digest_mismatch", item, "the manifest digest differs from the attempt's")
        files = {a["relative_path"]: a for a in json.loads(manifest[2])}
        names = {output.path: output.name for output in upstream.outputs}
        labels = tuple(
            sorted(derived_snapshot_labels(connection, org_id, project_id, (), (attempt[1],)))
        ) or (UNLABELLED,)
        for path in item.paths:
            output = names.get(path)
            if output is None:
                raise _refuse("output_missing", item, f"{path!r} is not a declared output")
            io = connection.execute(
                """SELECT sha256, size_bytes, data_class FROM campaign_stage_io
                WHERE org_id=? AND project_id=? AND campaign_id=? AND stage_key=?
                AND attempt_id=? AND direction='output' AND name=?""",
                (org_id, project_id, campaign_id, item.stage, attempt[0], output),
            ).fetchone()
            listed = files.get(path)
            if io is None or listed is None:
                raise _refuse("output_missing", item, f"output {output!r} was not recorded")
            if listed["sha256"] != io[0] or int(listed["size"]) != int(io[1]):
                raise _refuse(
                    "digest_mismatch", item, f"output {output!r} differs from its manifest entry"
                )
            pinned = expected.get((item.name, output))
            if pinned is not None and pinned != io[0]:
                raise _refuse(
                    "digest_mismatch", item, f"output {output!r} differs from its earlier binding"
                )
            resolved.append(
                ResolvedStageInput(
                    name=item.name,
                    source_stage=item.stage,
                    output=output,
                    digest=io[0],
                    bytes=int(io[1]),
                    data_class=io[2],
                    license_labels=labels,
                    path=path,
                    manifest_id=attempt[1],
                )
            )
    return tuple(resolved)


def check_resolved_routing(
    connection: sqlite3.Connection,
    stage: Any,
    resolved: Iterable[ResolvedStageInput],
    *,
    worker: str | None,
    approval_digest: str | None = None,
) -> frozenset[str]:
    """ADR 0040 / 0113 after resolution: the stage's provider instance must admit each bound
    input's recorded data class and its licence labels, under the same trust setting the
    reservation check uses. Raises LateBindRefused (``data_class_refused`` or
    ``license_refused``); returns the labels checked."""
    from . import provider_trust
    from .license_labels import license_refusal

    narrowed = frozenset(stage.send_classes) if getattr(stage, "send_classes", None) else None
    carried: set[str] = set()
    for item in resolved:
        if not provider_trust.provider_admits_class(
            stage.provider,
            worker,
            item.data_class,
            narrowed,
            approval_digest,
            connection=connection,
        ):
            raise LateBindRefused(
                "data_class_refused",
                f"input {item.name!r} output {item.output!r} of stage {item.source_stage!r} is "
                f"{item.data_class} and provider {stage.provider!r} with worker principal "
                f"{worker!r} is not trusted to receive it",
                input_name=item.name,
            )
        carried |= set(item.license_labels)
    reason = license_refusal(stage.provider, carried, provider_trust.current_policy(connection))
    if reason:
        raise LateBindRefused("license_refused", reason)
    return frozenset(carried)


def bound_inputs(
    connection: sqlite3.Connection, campaign: Mapping[str, Any], stage_key: str
) -> tuple[ResolvedStageInput, ...]:
    """The bindings recorded for a stage (empty when it has no stage-output input or has not
    started). Scoped to the campaign's project like every event read."""
    row = connection.execute(
        """SELECT payload_json FROM events WHERE project_id=? AND event_type=?
        AND json_extract(payload_json, '$.campaign_id') = ?
        AND json_extract(payload_json, '$.stage_key') = ?
        ORDER BY rowid DESC LIMIT 1""",
        (campaign["project_id"], BOUND_EVENT, campaign["id"], stage_key),
    ).fetchone()
    if row is None:
        return ()
    payload = json.loads(row[0])
    return tuple(ResolvedStageInput.from_json(item) for item in payload[LAUNCH_INPUTS_KEY])
