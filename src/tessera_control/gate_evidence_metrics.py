"""Typed per-seed metric records and platform-computed replicate groups.

Values are read from stored measurements, never supplied by callers. A group selects
every record of its series on the server; a failed or cancelled bound attempt is
listed as excluded and is never imputed. Nothing here assesses scientific validity.
"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from typing import Any

from . import gate_evidence_stats as stats
from .gate_evidence_models import CreateReplicateGroup, RecordMetric
from .store import Conflict, NotFound, Store, canonical_digest, utc_now

_TERMINAL = ("succeeded", "failed", "cancelled")
_DUMP = {"sort_keys": True, "separators": (",", ":")}


def series_accounting(
    connection: sqlite3.Connection,
    project_id: str,
    variant_id: str,
    lock_id: str,
    metric: str,
    partition: str,
) -> dict[str, Any]:
    """Account for every attempt of one (variant, lock, metric, partition) series.

    An attempt is in the series if its pre-dispatch binding declared this metric name
    on this partition. A legacy binding (NULL metrics_json) declared nothing, so it is
    conservatively in every series of its variant and lock: it can never be silently
    dropped. Never imputes anything.

    Returns members (attempt_id -> metric record), missing_record (succeeded attempts
    without a record), blocking (every unresolved attempt, including a pre-registered
    seed that was never dispatched: aggregating only the seeds that were run would be
    optional stopping) and excluded (failed and cancelled attempts, with outcome and
    reason, which stay visible in the group). A never-dispatched seed is withdrawn
    only by resolving it `cancelled` with a recorded reason.
    """
    records = {
        r["attempt_id"]: dict(r)
        for r in connection.execute(
            """SELECT * FROM gate_metric_records WHERE variant_id=? AND lock_id=?
            AND metric=? AND partition=?""",
            (variant_id, lock_id, metric, partition),
        )
    }
    members: dict[str, dict[str, Any]] = {}
    missing_record: list[str] = []
    blocking: list[str] = []
    excluded: list[dict[str, Any]] = []
    rows = connection.execute(
        """SELECT a.id,a.state,a.outcome,a.reason,b.metrics_json
        FROM gate_attempt_split_bindings b
        JOIN research_experiment_admitted_attempts a ON a.id=b.attempt_id
        WHERE a.variant_id=? AND b.lock_id=? AND b.project_id=? ORDER BY a.id""",
        (variant_id, lock_id, project_id),
    ).fetchall()
    for attempt in rows:
        if attempt["metrics_json"] is None:
            in_series = True
        else:
            in_series = any(
                m["name"] == metric and m["partition"] == partition
                for m in json.loads(attempt["metrics_json"])
            )
        if not in_series:
            continue
        if attempt["state"] not in _TERMINAL:
            blocking.append(attempt["id"])
        elif attempt["outcome"] == "succeeded":
            if attempt["id"] in records:
                members[attempt["id"]] = records[attempt["id"]]
            else:
                missing_record.append(attempt["id"])
        else:
            excluded.append(
                {
                    "attempt_id": attempt["id"],
                    "outcome": attempt["outcome"] or attempt["state"],
                    "reason": attempt["reason"],
                }
            )
    return {
        "members": members,
        "missing_record": missing_record,
        "blocking": blocking,
        "excluded": excluded,
    }


def _group_view(row: Any, members: list[dict[str, Any]]) -> dict[str, Any]:
    group = dict(row)
    group["sources"] = json.loads(group.pop("sources_json"))
    group["excluded_attempts"] = json.loads(group.pop("excluded_attempts_json"))
    group["members"] = members
    return group


def _members(connection: sqlite3.Connection, group_id: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in connection.execute(
            """SELECT m.* FROM gate_metric_records m
            JOIN gate_replicate_members g ON g.metric_record_id=m.id
            WHERE g.group_id=? ORDER BY m.seed,m.id""",
            (group_id,),
        )
    ]


def load_replicate_group(
    connection: sqlite3.Connection, project_id: str, group_id: str
) -> dict[str, Any] | None:
    """Pure read for the gate evaluator; the caller enforces tenancy via project_id."""
    row = connection.execute(
        "SELECT * FROM gate_replicate_groups WHERE id=? AND project_id=?",
        (group_id, project_id),
    ).fetchone()
    return None if row is None else _group_view(row, _members(connection, group_id))


def _record_view(row: Any) -> dict[str, Any]:
    return {**dict(row), "scientific_validity": "not_assessed"}


class GateMetricMixin:
    """Mixed into GateEvidenceStore (which provides ResearchStore helpers)."""

    @staticmethod
    def _gate_attempt(
        connection: sqlite3.Connection, actor: dict[str, Any], attempt_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            """SELECT a.*,e.project_id FROM research_experiment_admitted_attempts a
            JOIN research_experiments e ON e.id=a.experiment_id
            JOIN projects p ON p.id=e.project_id
            WHERE a.id=? AND p.org_id=? AND (? IS NULL OR e.project_id=?)""",
            (attempt_id, actor["org_id"], actor["project_id"], actor["project_id"]),
        ).fetchone()
        if row is None:
            raise NotFound("Experiment attempt was not found.")
        return dict(row)

    def record_metric(
        self, principal: dict[str, Any], attempt_id: str, key: str, digest: str, payload: Any
    ) -> tuple[int, dict[str, Any]]:
        body = self._payload(RecordMetric, payload)  # type: ignore[attr-defined]

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._human(actor, "record a gate metric")  # type: ignore[attr-defined]
            attempt = self._gate_attempt(connection, actor, attempt_id)
            binding = connection.execute(
                "SELECT * FROM gate_attempt_split_bindings WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if binding is None:
                raise Conflict("The attempt has no pre-registered split lock.")
            if binding["seed"] is None:
                raise Conflict("The binding predates seed pre-registration.")
            if binding["metrics_json"] is None:
                raise Conflict("The binding predates metric pre-registration.")
            declared = {m["name"]: m for m in json.loads(binding["metrics_json"])}
            lock = connection.execute(
                "SELECT partitions_json FROM gate_split_locks WHERE id=?", (binding["lock_id"],)
            ).fetchone()
            partitions = {p["name"]: p for p in json.loads(lock["partitions_json"])}
            measurement = connection.execute(
                "SELECT * FROM research_admitted_attempt_measurements WHERE id=?",
                (body["measurement_id"],),
            ).fetchone()
            if measurement is None or measurement["attempt_id"] != attempt_id:
                raise Conflict("The measurement does not belong to this attempt.")
            metric = measurement["name"]
            if metric not in declared:
                raise Conflict("The measurement name was not pre-registered for this attempt.")
            partition = declared[metric]["partition"]
            if partition not in partitions:
                raise Conflict("The declared partition is not part of the bound split lock.")
            try:
                value_obj = json.loads(measurement["value_json"])
            except ValueError:
                value_obj = None
            number = value_obj.get("number") if isinstance(value_obj, dict) else None
            if type(number) not in (int, float) or not math.isfinite(number):
                raise Conflict('The measurement value is not an object with a finite "number".')
            checks = (
                (
                    "SELECT 1 FROM gate_metric_records WHERE measurement_id=?",
                    (body["measurement_id"],),
                    "The measurement is already recorded as a metric.",
                ),
                (
                    "SELECT 1 FROM gate_metric_records "
                    "WHERE attempt_id=? AND metric=? AND partition=?",
                    (attempt_id, metric, partition),
                    "The attempt already has this metric for this partition.",
                ),
                (
                    "SELECT 1 FROM gate_metric_records WHERE variant_id=? AND lock_id=? "
                    "AND metric=? AND partition=? AND seed=?",
                    (
                        attempt["variant_id"],
                        binding["lock_id"],
                        metric,
                        partition,
                        binding["seed"],
                    ),
                    "This variant already has a record for this seed on this split.",
                ),
            )
            for sql, params, message in checks:
                if connection.execute(sql, params).fetchone() is not None:
                    raise Conflict(message)
            record = {
                "id": "gate_metric_" + uuid.uuid4().hex,
                "project_id": attempt["project_id"],
                "experiment_id": attempt["experiment_id"],
                "variant_id": attempt["variant_id"],
                "attempt_id": attempt_id,
                "measurement_id": body["measurement_id"],
                "measurement_source": measurement["source"],
                "metric": metric,
                "unit": declared[metric]["unit"],
                "lock_id": binding["lock_id"],
                "partition": partition,
                "membership_digest": binding["membership_digest"],
                "sample_count": int(partitions[partition]["member_count"]),
                "seed": binding["seed"],
                "checkpoint_sha": body["checkpoint_sha"],
                "value": float(number),
                "created_at": utc_now(),
                "created_by_principal_id": actor["id"],
            }
            columns = ",".join(record)
            marks = ",".join(f":{k}" for k in record)
            try:
                connection.execute(
                    f"INSERT INTO gate_metric_records ({columns}) VALUES ({marks})", record
                )
            except sqlite3.IntegrityError as error:
                raise Conflict("The metric record conflicts with an existing record.") from error
            Store._event(
                connection,
                actor,
                attempt["project_id"],
                None,
                "gate.metric_recorded",
                1,
                {
                    "metric_record_id": record["id"],
                    "attempt_id": attempt_id,
                    "measurement_id": body["measurement_id"],
                    "metric": metric,
                    "seed": binding["seed"],
                },
            )
            return 201, _record_view(record)

        return self._mutate(  # type: ignore[attr-defined]
            principal,
            key,
            "POST",
            f"/experiment-attempts/{attempt_id}/metric-records",
            digest,
            command,
        )

    def list_metric_records(
        self, principal: dict[str, Any], attempt_id: str
    ) -> list[dict[str, Any]]:
        connection = sqlite3.connect(self.database_path)  # type: ignore[attr-defined]
        connection.row_factory = sqlite3.Row
        try:
            actor = self._live_principal(connection, principal)  # type: ignore[attr-defined]
            self._gate_attempt(connection, actor, attempt_id)
            return [
                _record_view(r)
                for r in connection.execute(
                    "SELECT * FROM gate_metric_records WHERE attempt_id=? ORDER BY created_at,id",
                    (attempt_id,),
                )
            ]
        finally:
            connection.close()

    def create_replicate_group(
        self, principal: dict[str, Any], project_id: str, key: str, digest: str, payload: Any
    ) -> tuple[int, dict[str, Any]]:
        body = self._payload(CreateReplicateGroup, payload)  # type: ignore[attr-defined]

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._human(actor, "create a replicate group")  # type: ignore[attr-defined]
            project = Store._project(connection, actor, project_id)
            variant = connection.execute(
                """SELECT v.id,v.experiment_id FROM research_experiment_variants v
                JOIN research_experiments e ON e.id=v.experiment_id
                WHERE v.id=? AND e.project_id=?""",
                (body["experiment_variant_id"], project["id"]),
            ).fetchone()
            if variant is None:
                raise NotFound("Experiment variant was not found.")
            lock = connection.execute(
                "SELECT * FROM gate_split_locks WHERE id=? AND project_id=?",
                (body["split_lock_id"], project["id"]),
            ).fetchone()
            if lock is None:
                raise NotFound("Split lock was not found.")
            if body["partition"] not in {p["name"] for p in json.loads(lock["partitions_json"])}:
                raise Conflict("The partition is not part of the split lock.")
            series = series_accounting(
                connection,
                project["id"],
                variant["id"],
                lock["id"],
                body["metric"],
                body["partition"],
            )
            if series["blocking"]:
                raise Conflict(
                    f"Bound attempt {series['blocking'][0]} is not resolved; every "
                    "pre-registered seed must finish, or be cancelled with a reason before "
                    "dispatch, before aggregating."
                )
            if series["missing_record"]:
                raise Conflict(
                    f"Succeeded attempt {series['missing_record'][0]} has no metric record; "
                    "record every succeeded seed or resolve it as failed with a reason."
                )
            members = list(series["members"].values())
            excluded = series["excluded"]
            if len({m["unit"] for m in members}) > 1:
                raise Conflict("Metric records for this series use different units.")
            if len({m["seed"] for m in members}) < 2:
                raise Conflict("A replicate group needs at least two distinct seeds.")
            members.sort(key=lambda m: (m["seed"], m["id"]))
            seed_material = canonical_digest(
                {"members": sorted(m["id"] for m in members), "method": body["ci_method"]}
            )
            try:
                result = stats.aggregate(
                    [m["value"] for m in members], body["ci_method"], seed_material=seed_material
                )
            except ValueError as error:
                raise Conflict(f"Aggregation refused: {error}") from error
            sources = sorted({m["measurement_source"] for m in members})
            min_sample = min(m["sample_count"] for m in members)
            result_digest = canonical_digest(
                {
                    "members": [
                        {
                            "id": m["id"],
                            "seed": m["seed"],
                            "value": m["value"],
                            "attempt_id": m["attempt_id"],
                        }
                        for m in members
                    ],
                    "stats": result,
                    "lock_id": lock["id"],
                    "membership_digest": lock["membership_digest"],
                    "variant_id": variant["id"],
                    "metric": body["metric"],
                    "unit": members[0]["unit"],
                    "partition": body["partition"],
                    "excluded": excluded,
                }
            )
            group = {
                "id": "gate_group_" + uuid.uuid4().hex,
                "project_id": project["id"],
                "experiment_id": variant["experiment_id"],
                "variant_id": variant["id"],
                "metric": body["metric"],
                "unit": members[0]["unit"],
                "lock_id": lock["id"],
                "partition": body["partition"],
                "membership_digest": lock["membership_digest"],
                "ci_method": body["ci_method"],
                "statistics_version": result["statistics_version"],
                "n": result["n"],
                "mean": result["mean"],
                "std": result["std"],
                "ci_low": result["ci_low"],
                "ci_high": result["ci_high"],
                "min_sample_count": min_sample,
                "sources_json": json.dumps(sources, **_DUMP),
                "excluded_attempts_json": json.dumps(excluded, **_DUMP),
                "result_digest": result_digest,
                "created_at": utc_now(),
                "created_by_principal_id": actor["id"],
            }
            columns = ",".join(group)
            marks = ",".join(f":{k}" for k in group)
            connection.execute(
                f"INSERT INTO gate_replicate_groups ({columns}) VALUES ({marks})", group
            )
            connection.executemany(
                "INSERT INTO gate_replicate_members (group_id,metric_record_id) VALUES (?,?)",
                [(group["id"], m["id"]) for m in members],
            )
            Store._event(
                connection,
                actor,
                project["id"],
                None,
                "gate.replicate_group_created",
                1,
                {
                    "group_id": group["id"],
                    "variant_id": variant["id"],
                    "metric": body["metric"],
                    "n": result["n"],
                    "result_digest": result_digest,
                },
            )
            view = _group_view(group, members)
            view["scientific_validity"] = "not_assessed"
            view["imputation"] = "none"
            return 201, view

        return self._mutate(  # type: ignore[attr-defined]
            principal, key, "POST", f"/projects/{project_id}/replicate-groups", digest, command
        )

    def get_replicate_group(self, principal: dict[str, Any], group_id: str) -> dict[str, Any]:
        connection = sqlite3.connect(self.database_path)  # type: ignore[attr-defined]
        connection.row_factory = sqlite3.Row
        try:
            actor = self._live_principal(connection, principal)  # type: ignore[attr-defined]
            row = connection.execute(
                """SELECT g.project_id FROM gate_replicate_groups g
                JOIN projects p ON p.id=g.project_id
                WHERE g.id=? AND p.org_id=? AND (? IS NULL OR g.project_id=?)""",
                (group_id, actor["org_id"], actor["project_id"], actor["project_id"]),
            ).fetchone()
            group = (
                None
                if row is None
                else load_replicate_group(connection, row["project_id"], group_id)
            )
            if group is None:
                raise NotFound("Replicate group was not found.")
            group["scientific_validity"] = "not_assessed"
            group["imputation"] = "none"
            return group
        finally:
            connection.close()
