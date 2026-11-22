"""Paired-delta replicate groups (ADR 0045): the per-seed difference between two arms.

Generic and platform-computed. The two arms are experiment variants; their per-seed typed
metric records (``gate_metric_records``) on one split lock, metric and partition are paired
by seed and the delta ``arm_a - arm_b`` is aggregated with the platform statistics. The
caller names records' series; it never supplies a number. Anything incomplete (an
unresolved or unrecorded attempt, a seed present in only one arm, mixed units, fewer than
two pairs) is refused rather than dropped, so a verdict can never rest on a convenient subset.
"""

from __future__ import annotations

import json
import operator
import sqlite3
import uuid
from typing import Any

from . import gate_evidence_stats as stats
from .gate_evidence_ingest import ingested_series
from .gate_evidence_metrics import series_accounting
from .store import Conflict, NotFound, Store, canonical_digest, utc_now

_DUMP = {"sort_keys": True, "separators": (",", ":")}
COMPARISONS = {"ge": operator.ge, "gt": operator.gt, "le": operator.le, "lt": operator.lt}


class PairedRefused(Conflict):
    """The paired delta cannot be computed honestly; ``code`` is a stable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def compute_paired_delta(
    connection: sqlite3.Connection,
    project_id: str,
    *,
    arm_a_variant_id: str,
    arm_b_variant_id: str,
    lock_id: str,
    metric: str,
    partition: str,
    ci_method: str = "t_interval_95",
    campaign_id: str | None = None,
    allow_ingested: bool = True,
) -> dict[str, Any]:
    """Pure read in the caller's transaction; raises PairedRefused / NotFound.

    With ``campaign_id`` every member attempt of both arms must have been admitted by that
    campaign's stages (an attempt from anywhere else cannot support a gate on it).
    """
    if arm_a_variant_id == arm_b_variant_id:
        raise PairedRefused("same_arm", "The two arms must be different experiment variants.")
    for variant in (arm_a_variant_id, arm_b_variant_id):
        found = connection.execute(
            """SELECT 1 FROM research_experiment_variants v
            JOIN research_experiments e ON e.id=v.experiment_id
            WHERE v.id=? AND e.project_id=?""",
            (variant, project_id),
        ).fetchone()
        if found is None:
            raise NotFound("Experiment variant was not found.")
    lock = connection.execute(
        "SELECT * FROM gate_split_locks WHERE id=? AND project_id=?", (lock_id, project_id)
    ).fetchone()
    if lock is None:
        raise NotFound("Split lock was not found.")
    if partition not in {p["name"] for p in json.loads(lock["partitions_json"])}:
        raise PairedRefused("partition_not_in_lock", "The partition is not part of the split lock.")
    arms: dict[str, dict[str, Any]] = {}
    for label, variant in (("a", arm_a_variant_id), ("b", arm_b_variant_id)):
        series = series_accounting(connection, project_id, variant, lock_id, metric, partition)
        if series["blocking"]:
            raise PairedRefused(
                "attempt_unresolved",
                f"Arm {label} attempt {series['blocking'][0]} is not resolved; every "
                "pre-registered seed must finish before pairing.",
            )
        if series["missing_record"]:
            raise PairedRefused(
                "metric_record_missing",
                f"Arm {label} succeeded attempt {series['missing_record'][0]} has no "
                "metric record.",
            )
        if series["excluded"]:
            # A failed or cancelled seed is never silently dropped from a pairing: cancelling
            # the same bad seed in both arms must not be able to manufacture a verdict.
            raise PairedRefused(
                "attempt_excluded",
                f"Arm {label} has an excluded attempt ({series['excluded'][0]['attempt_id']}); "
                "a gate needs every pre-registered seed to have succeeded.",
            )
        by_seed: dict[int, dict[str, Any]] = {}
        for record in series["members"].values():
            by_seed[record["seed"]] = record
        # Rows a campaign stage emitted and the platform ingested itself (ADR 0052).
        ingested = ingested_series(
            connection, project_id, variant, lock_id, metric, partition, campaign_id
        )
        if ingested and not allow_ingested:
            raise PairedRefused(
                "ingested_rows_not_storable",
                "Ingested evidence rows support a campaign unlock evaluation, not a stored group.",
            )
        for record in ingested:
            if record["seed"] in by_seed:
                raise PairedRefused(
                    "seed_duplicated",
                    f"Arm {label} has two records for seed {record['seed']}.",
                )
            by_seed[record["seed"]] = record
        arms[label] = {"by_seed": by_seed, "excluded": series["excluded"]}
    seeds_a, seeds_b = set(arms["a"]["by_seed"]), set(arms["b"]["by_seed"])
    if seeds_a != seeds_b:
        raise PairedRefused(
            "seeds_unpaired",
            "The arms' recorded seeds differ (arm a only: "
            f"{sorted(seeds_a - seeds_b)[:10]}, arm b only: {sorted(seeds_b - seeds_a)[:10]}); "
            "pairs are never formed from a subset.",
        )
    if len(seeds_a) < 2:
        raise PairedRefused("too_few_pairs", "A paired delta needs at least two paired seeds.")
    members = [
        (seed, arms["a"]["by_seed"][seed], arms["b"]["by_seed"][seed]) for seed in sorted(seeds_a)
    ]
    units = {m[1]["unit"] for m in members} | {m[2]["unit"] for m in members}
    if len(units) != 1:
        raise PairedRefused("unit_mismatch", "The metric records use different units.")
    if campaign_id is not None:
        attempts = {m[1]["attempt_id"] for m in members} | {m[2]["attempt_id"] for m in members}
        owned = {
            row["admitted_attempt_id"]
            for row in connection.execute(
                "SELECT admitted_attempt_id FROM campaign_stage_attempts "
                "WHERE campaign_id=? AND admitted_attempt_id IS NOT NULL",
                (campaign_id,),
            )
        }
        if not attempts <= owned:
            raise PairedRefused(
                "attempts_not_from_campaign",
                "Every paired attempt must have been admitted by the named campaign.",
            )
    deltas = [float(a["value"]) - float(b["value"]) for _, a, b in members]
    seed_material = canonical_digest(
        {"pairs": [[a["id"], b["id"]] for _, a, b in members], "method": ci_method}
    )
    try:
        result = stats.aggregate(deltas, ci_method, seed_material=seed_material)
    except ValueError as error:
        raise PairedRefused("aggregation_refused", f"Aggregation refused: {error}") from error
    sources = sorted(
        {m[1]["measurement_source"] for m in members}
        | {m[2]["measurement_source"] for m in members}
    )
    excluded = {"arm_a": arms["a"]["excluded"], "arm_b": arms["b"]["excluded"]}
    pairs = [
        {
            "seed": seed,
            "record_a_id": a["id"],
            "record_b_id": b["id"],
            "value_a": a["value"],
            "value_b": b["value"],
            "delta": delta,
        }
        for (seed, a, b), delta in zip(members, deltas, strict=True)
    ]
    view = {
        "arm_a_variant_id": arm_a_variant_id,
        "arm_b_variant_id": arm_b_variant_id,
        "metric": metric,
        "unit": units.pop(),
        "lock_id": lock_id,
        "partition": partition,
        "membership_digest": lock["membership_digest"],
        "ci_method": ci_method,
        "statistics_version": result["statistics_version"],
        "n": result["n"],
        "mean": result["mean"],
        "std": result["std"],
        "ci_low": result["ci_low"],
        "ci_high": result["ci_high"],
        "min_sample_count": min(min(a["sample_count"], b["sample_count"]) for _, a, b in members),
        "sources": sources,
        "excluded_attempts": excluded,
        "pairs": pairs,
    }
    view["result_digest"] = canonical_digest(view)
    return view


def paired_outcome(
    result: dict[str, Any],
    *,
    statistic: str,
    comparison: str,
    threshold: float,
    minimum_pairs: int,
    minimum_samples: int,
    allowed_sources: list[str],
) -> tuple[str | None, str | None]:
    """(outcome, None) with outcome pass|fail, or (None, reason) when it cannot be decided."""
    if any(source not in allowed_sources for source in result["sources"]):
        return None, "disallowed_measurement_source"
    if result["n"] < minimum_pairs:
        return None, "insufficient_pairs"
    if result["min_sample_count"] < minimum_samples:
        return None, "insufficient_samples"
    value = result[statistic]
    return ("pass" if COMPARISONS[comparison](value, threshold) else "fail"), None


class PairedGroupMixin:
    """Mixed into GateEvidenceStore next to GateMetricMixin."""

    def create_paired_group(
        self, principal: dict[str, Any], project_id: str, key: str, digest: str, payload: Any
    ) -> tuple[int, dict[str, Any]]:
        from .gate_evidence_models import CreatePairedReplicateGroup

        body = self._payload(CreatePairedReplicateGroup, payload)  # type: ignore[attr-defined]

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._human(actor, "create a paired replicate group")  # type: ignore[attr-defined]
            project = Store._project(connection, actor, project_id)
            result = compute_paired_delta(
                connection,
                project["id"],
                arm_a_variant_id=body["arm_a_variant_id"],
                arm_b_variant_id=body["arm_b_variant_id"],
                lock_id=body["split_lock_id"],
                metric=body["metric"],
                partition=body["partition"],
                ci_method=body["ci_method"],
                allow_ingested=False,
            )
            group_id = "gate_paired_" + uuid.uuid4().hex
            connection.execute(
                """INSERT INTO gate_paired_groups (id,project_id,arm_a_variant_id,
                arm_b_variant_id,metric,unit,lock_id,partition,membership_digest,ci_method,
                statistics_version,n,mean,std,ci_low,ci_high,min_sample_count,sources_json,
                excluded_attempts_json,result_digest,created_at,created_by_principal_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    group_id,
                    project["id"],
                    result["arm_a_variant_id"],
                    result["arm_b_variant_id"],
                    result["metric"],
                    result["unit"],
                    result["lock_id"],
                    result["partition"],
                    result["membership_digest"],
                    result["ci_method"],
                    result["statistics_version"],
                    result["n"],
                    result["mean"],
                    result["std"],
                    result["ci_low"],
                    result["ci_high"],
                    result["min_sample_count"],
                    json.dumps(result["sources"], **_DUMP),
                    json.dumps(result["excluded_attempts"], **_DUMP),
                    result["result_digest"],
                    utc_now(),
                    actor["id"],
                ),
            )
            connection.executemany(
                "INSERT INTO gate_paired_members (group_id,seed,record_a_id,record_b_id,delta) "
                "VALUES (?,?,?,?,?)",
                [
                    (group_id, p["seed"], p["record_a_id"], p["record_b_id"], p["delta"])
                    for p in result["pairs"]
                ],
            )
            Store._event(
                connection,
                actor,
                project["id"],
                None,
                "gate.paired_group_created",
                1,
                {
                    "group_id": group_id,
                    "arm_a_variant_id": result["arm_a_variant_id"],
                    "arm_b_variant_id": result["arm_b_variant_id"],
                    "metric": result["metric"],
                    "n": result["n"],
                    "result_digest": result["result_digest"],
                },
            )
            return 201, {
                **result,
                "id": group_id,
                "scientific_validity": "not_assessed",
                "imputation": "none",
            }

        return self._mutate(  # type: ignore[attr-defined]
            principal,
            key,
            "POST",
            f"/projects/{project_id}/paired-replicate-groups",
            digest,
            command,
        )

    def get_paired_group(self, principal: dict[str, Any], group_id: str) -> dict[str, Any]:
        connection = sqlite3.connect(self.database_path)  # type: ignore[attr-defined]
        connection.row_factory = sqlite3.Row
        try:
            actor = self._live_principal(connection, principal)  # type: ignore[attr-defined]
            row = connection.execute(
                """SELECT g.* FROM gate_paired_groups g JOIN projects p ON p.id=g.project_id
                WHERE g.id=? AND p.org_id=? AND (? IS NULL OR g.project_id=?)""",
                (group_id, actor["org_id"], actor["project_id"], actor["project_id"]),
            ).fetchone()
            if row is None:
                raise NotFound("Paired replicate group was not found.")
            group = dict(row)
            group["sources"] = json.loads(group.pop("sources_json"))
            group["excluded_attempts"] = json.loads(group.pop("excluded_attempts_json"))
            group["pairs"] = [
                dict(r)
                for r in connection.execute(
                    "SELECT seed,record_a_id,record_b_id,delta FROM gate_paired_members "
                    "WHERE group_id=? ORDER BY seed",
                    (group_id,),
                )
            ]
            group["scientific_validity"] = "not_assessed"
            group["imputation"] = "none"
            return group
        finally:
            connection.close()
