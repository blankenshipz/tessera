"""Record a provider-run stage's public fetch from its retained output (ADR 0097).

A rented runtime (``gpu_runtime_v1`` on Salad) fetches a stage's pinned ``public_artifact``
files on the provider and writes what it fetched into its ``output.json`` under ``fetched``
(rows ``{url, sha256, bytes, seconds, cache}``). The files never reach the control plane. After
the attempt succeeded and its ``output.json`` is retained in custody, the runner asks the
platform, under the campaign's approval, to read those rows itself. Control (never the worker)
checks them against the frozen pins of the approved stage and records the same
``execution.public_fetch_recorded`` event a local worker's report produces, once per operation.

A row that contradicts the pins (a missing or extra file, another digest or URL, more bytes than
the pin allows, a cache outcome the spec never asked for) records nothing: the receipt keeps the
pins as ``declared_only``. Nothing is inferred and nothing is fabricated.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .approval_trust import DELEGATED_ACTOR_KEY
from .public_fetch_models import public_fetch_host
from .store import Conflict, Forbidden, Store, StoreError

STEP = "record_provider_fetch"
# The runtime adds ``fetched`` to this output only (gpu_runtime_v1 ``read_output``).
OUTPUT_NAME = "output.json"
MAX_SECONDS = 7 * 86400
_CACHE_OUTCOMES = frozenset({"hit", "miss", "unavailable", "off"})
_ROW_KEYS = frozenset({"url", "sha256", "bytes", "seconds", "cache"})
_REQUIRED_KEYS = frozenset({"url", "sha256", "bytes", "seconds"})


class RecordProviderFetch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    artifact_id: Annotated[str, StringConstraints(min_length=1, max_length=200)]


def stage_pins(spec_stage: Any) -> list[dict[str, Any]]:
    """The approved stage's pinned public files, in spec order (empty for a stage with none)."""
    pins: list[dict[str, Any]] = []
    for source in getattr(spec_stage, "inputs", ()):
        if getattr(source, "kind", None) != "public_artifact":
            continue
        cache = bool(getattr(source, "cache", False))
        for name, url, sha256, max_bytes in source.public_files:
            pins.append(
                {
                    "input": source.name,
                    "name": name,
                    "url": url,
                    "sha256": sha256,
                    "max_bytes": max_bytes,
                    "cache": cache,
                }
            )
    return pins


def stage_hosts(spec_stage: Any, pins: list[dict[str, Any]]) -> list[str]:
    hosts = {public_fetch_host(pin["url"]) for pin in pins}
    for source in getattr(spec_stage, "inputs", ()):
        if getattr(source, "kind", None) == "public_artifact":
            hosts |= set(source.redirect_hosts)
    return sorted(h for h in hosts if h)


def _refuse(reason: str) -> Conflict:
    return Conflict(f"Provider fetch rows contradict the frozen pins: {reason}.")


def _seconds(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _refuse("seconds is not a number")
    if not math.isfinite(value) or value < 0 or value > MAX_SECONDS:
        raise _refuse("seconds is out of range")
    return value


def check_fetched_rows(pins: list[dict[str, Any]], fetched: Any) -> list[dict[str, Any]]:
    """The runtime's ``fetched`` rows as recorded files, or Conflict.

    Every pinned file exactly once (matched by its exact pinned URL and digest), at most its
    pinned ``max_bytes``, from the pinned host, with a cache outcome consistent with what the
    approved spec asked for. Anything else is refused whole."""
    if not pins:
        raise Forbidden("The stage declares no pinned public files.")
    if not isinstance(fetched, list):
        raise _refuse("the output carries no fetched list")
    if len(fetched) != len(pins):
        raise _refuse("the rows do not cover exactly the pinned files")
    waiting: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for pin in pins:
        waiting.setdefault((pin["url"], pin["sha256"]), []).append(pin)
    files = []
    for row in fetched:
        if not isinstance(row, dict) or not _REQUIRED_KEYS <= set(row) <= _ROW_KEYS:
            raise _refuse("a row is not a fetch record")
        url, sha256 = row["url"], row["sha256"]
        if not isinstance(url, str) or not isinstance(sha256, str):
            raise _refuse("a row is not a fetch record")
        candidates = waiting.get((url, sha256))
        if not candidates:
            raise _refuse("a row is not a pinned file or repeats one")
        pin = candidates.pop(0)
        size = row["bytes"]
        if type(size) is not int or size < 0 or size > pin["max_bytes"]:
            raise _refuse("a row's bytes exceed the pin")
        host = public_fetch_host(url)
        if host is None or host != public_fetch_host(pin["url"]):
            raise _refuse("a row's host is not the pinned host")
        cache = row.get("cache")
        if cache is not None and (cache not in _CACHE_OUTCOMES or (cache == "off") == pin["cache"]):
            raise _refuse("a row's cache outcome is not what the spec asked for")
        files.append(
            {
                "input": pin["input"],
                "name": pin["name"],
                "source_host": host,
                "sha256": pin["sha256"],
                "bytes": size,
                "seconds": _seconds(row["seconds"]),
                # The local-worker field (a boolean) where the outcome is one; the runtime's own
                # outcome is kept beside it, so ``unavailable``/``off`` are never read as a miss.
                "cached": {"hit": True, "miss": False}.get(cache),
                "cache": cache,
            }
        )
    if any(waiting.values()):
        raise _refuse("a pinned file has no row")
    return files


def _unique_object(pairs):
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate JSON key")
        result[name] = value
    return result


def _fetched_of(content: bytes) -> Any:
    try:
        document = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")),
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise _refuse("the output is not a JSON object") from error
    if not isinstance(document, dict):
        raise _refuse("the output is not a JSON object")
    return document.get("fetched")


class ProviderFetchRecordMixin:
    """Mixed into ExperimentStore (needs _evidence_attempt, _read_retained_output, _mutate)."""

    def record_provider_fetch(
        self, principal: dict[str, Any], attempt_id: str, key: str, digest: str, payload: Any
    ) -> tuple[int, dict[str, Any]]:
        try:
            body = RecordProviderFetch.model_validate(payload).model_dump(mode="json")
        except ValidationError as error:
            raise StoreError("Invalid provider fetch request.") from error

        def command(connection: sqlite3.Connection, actor: dict[str, Any]):
            from .campaign_delegation import _normal_spec, _stage_provider
            from .campaign_models import stage_on_candidate
            from .gate_evidence_ingest import spec_stage_key
            from .public_fetch_report import EVENT_TYPE, public_fetch_for_operation

            delegated = actor.get(DELEGATED_ACTOR_KEY)
            if not delegated:
                # No human or worker route: only a campaign's approved stage asks, and the
                # rows come from the retained output, never from the request.
                raise Forbidden("Provider fetch rows are recorded only by an approved stage.")
            self._human(actor)  # type: ignore[attr-defined]
            attempt = self._evidence_attempt(connection, actor, attempt_id)  # type: ignore[attr-defined]
            if attempt["attempt_kind"] != "admitted" or attempt["outcome"] != "succeeded":
                raise Conflict("Provider fetch rows need a succeeded admitted attempt.")
            if attempt["revision"] != body["expected_revision"]:
                raise Conflict("Experiment attempt revision is no longer current.")
            project_id = attempt["project_id"]
            campaign = connection.execute(
                "SELECT * FROM campaigns WHERE id=? AND org_id=? AND project_id=?",
                (delegated["campaign_id"], actor["org_id"], project_id),
            ).fetchone()
            stage_key = spec_stage_key(connection, delegated)
            owned = connection.execute(
                """SELECT operation_id, manifest_id FROM campaign_stage_attempts
                WHERE campaign_id=? AND stage_key=? AND admitted_attempt_id=?""",
                (delegated["campaign_id"], stage_key, attempt_id),
            ).fetchone()
            if campaign is None or owned is None:
                raise Forbidden("The attempt does not belong to this campaign stage.")
            operation_id = attempt["operation_id"]
            if operation_id is None or owned["operation_id"] != operation_id:
                raise Conflict("The attempt has no provider operation.")
            operation = connection.execute(
                """SELECT task_id, epoch FROM external_operations
                WHERE operation_id=? AND org_id=? AND project_id=?""",
                (operation_id, actor["org_id"], project_id),
            ).fetchone()
            if operation is None:
                raise Conflict("The attempt has no provider operation.")
            try:
                spec_stage = _normal_spec(campaign).stage(stage_key)
                placed = _stage_provider(connection, campaign, stage_key)
                if placed:
                    spec_stage = stage_on_candidate(spec_stage, placed)
            except (StopIteration, ValueError, TypeError, KeyError):
                raise Forbidden("The stage is not part of the approved spec.") from None
            pins = stage_pins(spec_stage)
            if not pins:
                raise Forbidden("The stage declares no pinned public files.")
            manifest = connection.execute(
                "SELECT artifacts_json FROM artifact_manifests WHERE id=? AND project_id=?",
                (owned["manifest_id"], project_id),
            ).fetchone()
            artifacts = json.loads(manifest["artifacts_json"]) if manifest else []
            if not any(
                item["id"] == body["artifact_id"] and item["relative_path"] == OUTPUT_NAME
                for item in artifacts
            ):
                raise Conflict("The artifact is not this attempt's retained output.json.")
            existing = public_fetch_for_operation(connection, operation_id)
            if existing is not None:
                # At most one record per operation; a repeat ask returns it unchanged.
                return 200, existing | {"recorded": False}
            content, sha256 = self._retained_output_bytes(
                connection, actor, attempt, attempt_id, body["artifact_id"]
            )
            files = check_fetched_rows(pins, _fetched_of(content))
            event = {
                "operation_id": operation_id,
                "files": files,
                "hosts": stage_hosts(spec_stage, pins),
                "via_control": False,
                "provider_id": spec_stage.provider,
                "source": "retained_output",
                "artifact_id": body["artifact_id"],
                "artifact_sha256": sha256,
            }
            Store._event(
                connection,
                actor,
                project_id,
                operation["task_id"],
                EVENT_TYPE,
                operation["epoch"],
                event,
            )
            return 201, event | {"recorded": True}

        return self._mutate(  # type: ignore[attr-defined]
            principal,
            key,
            "POST",
            f"/experiment-attempts/{attempt_id}/provider-fetch",
            digest,
            command,
        )

    def _retained_output_bytes(
        self,
        connection: sqlite3.Connection,
        actor: dict[str, Any],
        attempt: dict[str, Any],
        attempt_id: str,
        artifact_id: str,
    ) -> tuple[bytes, str]:
        """The attempt's retained output bytes and their sha256, integrity-checked by control.

        A project-placed output is read through the custody reader. A provider import that
        keeps its output in quarantine custody only (a Salad output) is read from that row:
        linked to this attempt and its operation, quarantined and unclassified, not late, at
        most 1 MiB, and its bytes must hash to the recorded digest."""
        placed = connection.execute(
            "SELECT 1 FROM project_artifact_placements WHERE artifact_id=? AND project_id=?",
            (artifact_id, attempt["project_id"]),
        ).fetchone()
        if placed is not None:
            return self._read_retained_output(  # type: ignore[attr-defined]
                connection, actor, attempt, attempt_id, artifact_id
            )
        from .project_artifact_retention import validate_experiment_artifact_evidence_reference

        if connection.execute(
            """SELECT 1 FROM research_admitted_attempt_artifacts
            WHERE attempt_id=? AND artifact_id=?""",
            (attempt_id, artifact_id),
        ).fetchone() is None or not self._artifact_matches_attempt_provenance(  # type: ignore[attr-defined]
            connection, actor, attempt, artifact_id
        ):
            raise Conflict("Output artifact is not linked to this attempt provenance.")
        validate_experiment_artifact_evidence_reference(
            connection, attempt["project_id"], artifact_id
        )
        row = connection.execute(
            """SELECT a.sha256,a.size,a.status,a.classification,a.late,a.content,a.operation_id
            FROM artifacts a JOIN projects p ON p.id=a.project_id
            WHERE a.id=? AND a.project_id=? AND p.org_id=?""",
            (artifact_id, attempt["project_id"], actor["org_id"]),
        ).fetchone()
        if (
            row is None
            or row["status"] != "quarantined"
            or row["classification"] != "unclassified"
            or row["late"]
            or row["operation_id"] != attempt["operation_id"]
            or type(row["content"]) is not bytes
            or row["size"] > 1024 * 1024
            or len(row["content"]) != row["size"]
            or hashlib.sha256(row["content"]).hexdigest() != row["sha256"]
        ):
            raise Conflict("Output has no eligible retained custody copy.")
        return row["content"], row["sha256"]
