"""Split locks, server-computed leakage audits and pre-dispatch attempt binding.

A lock freezes exact partition membership; the platform computes its digest and
every overlap count from stored members, never from caller claims. A recorded
lock or audit says what membership was frozen and what overlapped. It does not
certify that the split is scientifically valid (`scientific_validity` stays
`not_assessed`).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from .experiments import _bump_admitted_attempt_revision
from .gate_evidence_models import BindAttemptSplit, CreateCrossSetLeakageAudit, CreateSplitLock
from .research import ResearchStore
from .store import Conflict, NotFound, Store, _connect, canonical_digest, utc_now

SAMPLE_LIMIT = 20


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _placeholders(values: list[str]) -> str:
    return ",".join("?" for _ in values)


def lock_audit_status(
    connection: sqlite3.Connection,
    lock_id: str,
    reference_lock_ids: list[str],
    partition: str | None = None,
) -> dict[str, Any]:
    """Pure read for the gate evaluator; applies no tenant filter, so callers must.

    `within_lock` reports the lock's own audit. Locks are immutable, so every
    recorded overlap stays true: a reference is `overlap_detected` if ANY
    cross-set audit against it found overlap, `clean` only when a clean audit
    covers `partition` (any partition when None) AND compared EVERY partition of
    the reference lock, and otherwise `missing`. A later clean audit over fewer
    partitions can never mask an earlier overlap, and a clean audit of only a
    slice of the reference never certifies the whole reference.
    """
    within = connection.execute(
        """SELECT status FROM gate_leakage_audits WHERE lock_id=? AND kind='within_lock'
        ORDER BY created_at DESC, rowid DESC LIMIT 1""",
        (lock_id,),
    ).fetchone()

    def cross(reference: str) -> str:
        rows = connection.execute(
            """SELECT status,eval_partitions_json,reference_partitions_json
            FROM gate_leakage_audits
            WHERE lock_id=? AND kind='cross_set' AND reference_lock_id=?""",
            (lock_id, reference),
        ).fetchall()
        if any(row[0] != "clean" for row in rows):
            return "overlap_detected"
        ref = connection.execute(
            "SELECT partitions_json FROM gate_split_locks WHERE id=?", (reference,)
        ).fetchone()
        if ref is None:
            return "missing"
        needed = {p["name"] for p in json.loads(ref[0])}
        for row in rows:
            if (partition is None or partition in json.loads(row[1])) and needed <= set(
                json.loads(row[2])
            ):
                return "clean"
        return "missing"

    return {
        "within_lock": "missing" if within is None else within[0],
        "cross_set": {ref: cross(ref) for ref in reference_lock_ids},
    }


class SplitLockMixin:
    """Tenant-scoped, human-authored, immutable split locks and leakage audits."""

    lock_audit_status = staticmethod(lock_audit_status)

    @staticmethod
    def _lock(
        connection: sqlite3.Connection, actor: dict[str, Any], lock_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            """SELECT l.* FROM gate_split_locks l JOIN projects p ON p.id=l.project_id
            WHERE l.id=? AND p.org_id=? AND (? IS NULL OR l.project_id=?)""",
            (lock_id, actor["org_id"], actor["project_id"], actor["project_id"]),
        ).fetchone()
        if row is None:
            raise NotFound("Split lock was not found.")
        return dict(row)

    @staticmethod
    def _lock_response(lock: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": lock["id"],
            "project_id": lock["project_id"],
            "name": lock["name"],
            "membership_digest": lock["membership_digest"],
            "partitions": json.loads(lock["partitions_json"]),
            "member_count": lock["member_count"],
            "dataset_snapshot_id": lock["dataset_snapshot_id"],
            "created_at": lock["created_at"],
            "created_by_principal_id": lock["created_by_principal_id"],
        }

    @staticmethod
    def _audit_response(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "lock_id": row["lock_id"],
            "kind": row["kind"],
            "reference_lock_id": row["reference_lock_id"],
            "eval_partitions": json.loads(row["eval_partitions_json"]),
            "reference_partitions": json.loads(row["reference_partitions_json"]),
            "key_overlap_count": row["key_overlap_count"],
            "group_overlap_count": row["group_overlap_count"],
            "overlap_sample": json.loads(row["overlap_sample_json"]),
            "status": row["status"],
            "audit_digest": row["audit_digest"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _insert_audit(
        connection: sqlite3.Connection,
        actor: dict[str, Any],
        lock: dict[str, Any],
        *,
        reference: dict[str, Any] | None,
        eval_partitions: list[str],
        reference_partitions: list[str],
        key_overlap_count: int,
        group_overlap_count: int,
        sample: dict[str, list[str]],
    ) -> dict[str, Any]:
        clean = key_overlap_count == 0 and group_overlap_count == 0
        row = {
            "id": "leakage_audit_" + uuid.uuid4().hex,
            "project_id": lock["project_id"],
            "lock_id": lock["id"],
            "kind": "within_lock" if reference is None else "cross_set",
            "reference_lock_id": None if reference is None else reference["id"],
            "eval_partitions_json": _dumps(eval_partitions),
            "reference_partitions_json": _dumps(reference_partitions),
            "key_overlap_count": key_overlap_count,
            "group_overlap_count": group_overlap_count,
            "overlap_sample_json": _dumps(sample),
            "status": "clean" if clean else "overlap_detected",
            "audit_digest": canonical_digest(
                {
                    "kind": "within_lock" if reference is None else "cross_set",
                    "lock_membership_digest": lock["membership_digest"],
                    "reference_membership_digest": (
                        None if reference is None else reference["membership_digest"]
                    ),
                    "eval_partitions": eval_partitions,
                    "reference_partitions": reference_partitions,
                    "key_overlap_count": key_overlap_count,
                    "group_overlap_count": group_overlap_count,
                }
            ),
            "created_at": utc_now(),
            "created_by_principal_id": actor["id"],
        }
        connection.execute(
            """INSERT INTO gate_leakage_audits
            (id,project_id,lock_id,kind,reference_lock_id,eval_partitions_json,
             reference_partitions_json,key_overlap_count,group_overlap_count,overlap_sample_json,
             status,audit_digest,created_at,created_by_principal_id)
            VALUES (:id,:project_id,:lock_id,:kind,:reference_lock_id,:eval_partitions_json,
             :reference_partitions_json,:key_overlap_count,:group_overlap_count,
             :overlap_sample_json,:status,:audit_digest,:created_at,:created_by_principal_id)""",
            row,
        )
        return row

    def create_split_lock(
        self,
        principal: dict[str, Any],
        project_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | CreateSplitLock,
    ) -> tuple[int, dict[str, Any]]:
        body = ResearchStore._payload(CreateSplitLock, payload)

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            ResearchStore._human(actor, "create a split lock")
            Store._project(connection, actor, project_id)
            snapshot_id = body["dataset_snapshot_id"]
            if (
                snapshot_id is not None
                and connection.execute(
                    """SELECT 1 FROM dataset_snapshots d JOIN research_studies s
                    ON s.id=d.study_id WHERE d.id=? AND s.project_id=?""",
                    (snapshot_id, project_id),
                ).fetchone()
                is None
            ):
                raise NotFound("Dataset snapshot was not found.")
            partition_digests: dict[str, str] = {}
            partitions: list[dict[str, Any]] = []
            members: list[tuple[str, str, str, str | None]] = []
            lock_id = "split_lock_" + uuid.uuid4().hex
            for name in sorted(body["partitions"]):
                part = body["partitions"][name]
                groups = part["groups"]
                pairs = sorted(
                    (k, None if groups is None else groups[i]) for i, k in enumerate(part["keys"])
                )
                partition_digests[name] = canonical_digest(
                    {"partition": name, "members": [[k, g] for k, g in pairs]}
                )
                partitions.append(
                    {
                        "name": name,
                        "member_count": len(pairs),
                        "group_count": len({g for _, g in pairs if g is not None}),
                        "digest": partition_digests[name],
                    }
                )
                members.extend((lock_id, name, k, g) for k, g in pairs)
            membership_digest = canonical_digest({"version": 1, "partitions": partition_digests})
            expected = body["expected_membership_digest"]
            if expected is not None and expected != membership_digest:
                raise Conflict("Expected membership digest does not match the submitted split.")
            if (
                connection.execute(
                    "SELECT 1 FROM gate_split_locks WHERE project_id=? AND membership_digest=?",
                    (project_id, membership_digest),
                ).fetchone()
                is not None
            ):
                raise Conflict("A split lock with this membership already exists in the project.")
            now = utc_now()
            lock = {
                "id": lock_id,
                "project_id": project_id,
                "name": body["name"],
                "dataset_snapshot_id": snapshot_id,
                "membership_digest": membership_digest,
                "partitions_json": _dumps(partitions),
                "member_count": len(members),
                "created_at": now,
                "created_by_principal_id": actor["id"],
            }
            connection.execute(
                """INSERT INTO gate_split_locks
                (id,project_id,name,dataset_snapshot_id,membership_digest,partitions_json,
                 member_count,created_at,created_by_principal_id)
                VALUES (:id,:project_id,:name,:dataset_snapshot_id,:membership_digest,
                 :partitions_json,:member_count,:created_at,:created_by_principal_id)""",
                lock,
            )
            connection.executemany(
                "INSERT INTO gate_split_lock_members (lock_id,partition,member_key,group_key) "
                "VALUES (?,?,?,?)",
                members,
            )
            key_rows = connection.execute(
                """SELECT member_key FROM gate_split_lock_members WHERE lock_id=?
                GROUP BY member_key HAVING COUNT(DISTINCT partition)>1 ORDER BY member_key""",
                (lock_id,),
            ).fetchall()
            group_rows = connection.execute(
                """SELECT group_key FROM gate_split_lock_members
                WHERE lock_id=? AND group_key IS NOT NULL
                GROUP BY group_key HAVING COUNT(DISTINCT partition)>1 ORDER BY group_key""",
                (lock_id,),
            ).fetchall()
            audit = self._insert_audit(
                connection,
                actor,
                lock,
                reference=None,
                eval_partitions=sorted(body["partitions"]),
                reference_partitions=[],
                key_overlap_count=len(key_rows),
                group_overlap_count=len(group_rows),
                sample={
                    "keys": [r[0] for r in key_rows[:SAMPLE_LIMIT]],
                    "groups": [r[0] for r in group_rows[:SAMPLE_LIMIT]],
                },
            )
            Store._event(
                connection,
                actor,
                project_id,
                None,
                "gate.split_lock_created",
                1,
                {
                    "lock_id": lock_id,
                    "membership_digest": membership_digest,
                    "member_count": len(members),
                    "within_lock_status": audit["status"],
                },
            )
            result = self._lock_response(lock)
            result["within_lock_audit"] = self._audit_response(audit)
            result["scientific_validity"] = "not_assessed"
            return 201, result

        return self._mutate(
            principal, key, "POST", f"/projects/{project_id}/split-locks", digest, command
        )

    def get_split_lock(self, principal: dict[str, Any], lock_id: str) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            actor = self._live_principal(connection, principal)
            lock = self._lock(connection, actor, lock_id)
            result = self._lock_response(lock)
            result["leakage_audits"] = [
                self._audit_response(dict(row))
                for row in connection.execute(
                    """SELECT * FROM gate_leakage_audits WHERE lock_id=? OR reference_lock_id=?
                    ORDER BY created_at, id""",
                    (lock_id, lock_id),
                )
            ]
            result["scientific_validity"] = "not_assessed"
            return result
        finally:
            connection.close()

    def create_cross_set_audit(
        self,
        principal: dict[str, Any],
        lock_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | CreateCrossSetLeakageAudit,
    ) -> tuple[int, dict[str, Any]]:
        body = ResearchStore._payload(CreateCrossSetLeakageAudit, payload)

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            ResearchStore._human(actor, "record a leakage audit")
            lock = self._lock(connection, actor, lock_id)
            reference = self._lock(connection, actor, body["reference_lock_id"])
            if reference["id"] == lock["id"]:
                raise Conflict("A split lock cannot be its own leakage reference.")
            if reference["project_id"] != lock["project_id"]:
                raise Conflict("Reference split lock belongs to a different project.")
            eval_parts = sorted(set(body["eval_partitions"]))
            ref_parts = sorted(set(body["reference_partitions"]))
            for owner, names in ((lock, eval_parts), (reference, ref_parts)):
                known = {p["name"] for p in json.loads(owner["partitions_json"])}
                if not set(names) <= known:
                    raise Conflict("Named partition does not exist in the split lock.")
            side = (
                "SELECT DISTINCT {col} AS v FROM gate_split_lock_members "
                "WHERE lock_id=? AND partition IN ({ph}){notnull}"
            )

            def overlap(col: str) -> list[str]:
                notnull = f" AND {col} IS NOT NULL" if col == "group_key" else ""
                sql = (
                    "SELECT r.v FROM ("
                    + side.format(col=col, ph=_placeholders(ref_parts), notnull=notnull)
                    + ") r JOIN ("
                    + side.format(col=col, ph=_placeholders(eval_parts), notnull=notnull)
                    + ") e ON e.v=r.v ORDER BY r.v"
                )
                return [
                    r[0]
                    for r in connection.execute(
                        sql, (reference["id"], *ref_parts, lock["id"], *eval_parts)
                    )
                ]

            keys, groups = overlap("member_key"), overlap("group_key")
            audit = self._insert_audit(
                connection,
                actor,
                lock,
                reference=reference,
                eval_partitions=eval_parts,
                reference_partitions=ref_parts,
                key_overlap_count=len(keys),
                group_overlap_count=len(groups),
                sample={"keys": keys[:SAMPLE_LIMIT], "groups": groups[:SAMPLE_LIMIT]},
            )
            Store._event(
                connection,
                actor,
                lock["project_id"],
                None,
                "gate.leakage_audit_recorded",
                1,
                {
                    "audit_id": audit["id"],
                    "lock_id": lock["id"],
                    "reference_lock_id": reference["id"],
                    "status": audit["status"],
                },
            )
            result = self._audit_response(audit)
            result["scientific_validity"] = "not_assessed"
            return 201, result

        return self._mutate(
            principal, key, "POST", f"/split-locks/{lock_id}/leakage-audits", digest, command
        )

    @staticmethod
    def _admitted_attempt(
        connection: sqlite3.Connection, actor: dict[str, Any], attempt_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            """SELECT a.*,e.project_id FROM research_experiment_admitted_attempts a
            JOIN research_experiments e ON e.id=a.experiment_id JOIN projects p ON p.id=e.project_id
            WHERE a.id=? AND p.org_id=? AND (? IS NULL OR e.project_id=?)""",
            (attempt_id, actor["org_id"], actor["project_id"], actor["project_id"]),
        ).fetchone()
        if row is None:
            raise NotFound("Admitted experiment attempt was not found.")
        return dict(row)

    @staticmethod
    def _binding_response(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "attempt_id": row["attempt_id"],
            "project_id": row["project_id"],
            "split_lock_id": row["lock_id"],
            "seed": row["seed"],
            "metrics": json.loads(row["metrics_json"]) if row.get("metrics_json") else None,
            "membership_digest": row["membership_digest"],
            "attempt_state_at_binding": row["attempt_state_at_binding"],
            "bound_at": row["bound_at"],
            "bound_by_principal_id": row["bound_by_principal_id"],
        }

    def bind_attempt_split(
        self,
        principal: dict[str, Any],
        attempt_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | BindAttemptSplit,
    ) -> tuple[int, dict[str, Any]]:
        body = ResearchStore._payload(BindAttemptSplit, payload)

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            ResearchStore._human(actor, "bind a split to an attempt")
            from .campaign_delegation import assert_gate_binding_matches

            assert_gate_binding_matches(connection, actor, body)
            attempt = self._admitted_attempt(connection, actor, attempt_id)
            lock = self._lock(connection, actor, body["split_lock_id"])
            if attempt["state"] != "admitted":
                raise Conflict("A split can only be bound to an admitted attempt before dispatch.")
            if attempt["revision"] != body["expected_revision"]:
                raise Conflict("Admitted experiment attempt revision is no longer current.")
            if lock["project_id"] != attempt["project_id"]:
                raise Conflict("Split lock belongs to a different project than the attempt.")
            if (
                connection.execute(
                    "SELECT 1 FROM gate_attempt_split_bindings WHERE attempt_id=?", (attempt_id,)
                ).fetchone()
                is not None
            ):
                raise Conflict("Attempt already has a split binding.")
            twin = connection.execute(
                """SELECT 1 FROM gate_attempt_split_bindings b
                JOIN research_experiment_admitted_attempts a ON a.id=b.attempt_id
                WHERE b.lock_id=? AND b.seed=? AND a.variant_id=?""",
                (lock["id"], body["seed"], attempt["variant_id"]),
            ).fetchone()
            if twin is not None:
                raise Conflict("This variant already has an attempt bound to this lock and seed.")
            declared = {p["name"] for p in json.loads(lock["partitions_json"])}
            for metric in body["metrics"]:
                if metric["partition"] not in declared:
                    raise Conflict(
                        f"Declared partition {metric['partition']!r} is not part of the split lock."
                    )
            metrics = sorted(
                ({k: m[k] for k in ("name", "partition", "unit")} for m in body["metrics"]),
                key=lambda m: m["name"],
            )
            row = {
                "attempt_id": attempt_id,
                "project_id": lock["project_id"],
                "lock_id": lock["id"],
                "seed": body["seed"],
                "metrics_json": _dumps(metrics),
                "membership_digest": lock["membership_digest"],
                "attempt_state_at_binding": "admitted",
                "bound_at": utc_now(),
                "bound_by_principal_id": actor["id"],
            }
            connection.execute(
                """INSERT INTO gate_attempt_split_bindings
                (attempt_id,project_id,lock_id,seed,metrics_json,membership_digest,
                 attempt_state_at_binding,bound_at,bound_by_principal_id)
                VALUES (:attempt_id,:project_id,:lock_id,:seed,:metrics_json,:membership_digest,
                 :attempt_state_at_binding,:bound_at,:bound_by_principal_id)""",
                row,
            )
            revision = _bump_admitted_attempt_revision(connection, attempt)
            Store._event(
                connection,
                actor,
                lock["project_id"],
                None,
                "gate.attempt_split_bound",
                revision,
                {
                    "attempt_id": attempt_id,
                    "lock_id": lock["id"],
                    "seed": body["seed"],
                    "metrics": metrics,
                    "membership_digest": lock["membership_digest"],
                },
            )
            result = self._binding_response(row)
            result["attempt_revision"] = revision
            return 201, result

        return self._mutate(
            principal,
            key,
            "POST",
            f"/experiment-attempts/{attempt_id}/split-binding",
            digest,
            command,
        )

    def get_attempt_split_binding(
        self, principal: dict[str, Any], attempt_id: str
    ) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            actor = self._live_principal(connection, principal)
            self._admitted_attempt(connection, actor, attempt_id)
            row = connection.execute(
                "SELECT * FROM gate_attempt_split_bindings WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise NotFound("Attempt split binding was not found.")
            return self._binding_response(dict(row))
        finally:
            connection.close()
