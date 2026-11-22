"""Tenant-scoped artifact lineage: identified nodes and typed, acyclic edges.

A lineage node names exactly one set of bytes by sha256 within one project and
may bind it to existing Tessera records (a quarantined artifact, a committed
result checkpoint, the admitted attempt that produced it).  Edges record that
one node was derived from, trained from, labelled from or evaluated on another.
Lineage is attributed bookkeeping; it does not establish that bytes exist,
validate content, or make any scientific claim.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .store import (
    Conflict,
    Forbidden,
    NotFound,
    Store,
    StoreError,
    _connect,
    _parse_utc,
    canonical_digest,
    utc_now,
)

_TIMESTAMPED_TABLES = frozenset({"artifact_lineage_nodes", "artifact_lineage_edges"})

# Creation time normalized to seconds plus exactly six fractional digits, so ordering by it
# is ordering by time even for rows written without microseconds ("...:05Z", which sorts
# after "...:05.000001Z" as text) next to rows written with them.
_ORDER_KEY = (
    "substr(created_at, 1, 19) || '.' || substr("
    "CASE WHEN substr(created_at, 20, 1) = '.' "
    "THEN substr(created_at, 21, length(created_at) - 21) ELSE '' END || '000000', 1, 6)"
)
_ORDER = f"ORDER BY {_ORDER_KEY}, id"


def _created_at(connection: sqlite3.Connection, table: str, project_id: str) -> str:
    """A creation time strictly later than every row already in ``table`` for the project.

    Listings and exports order by creation time and then by ``id``, and ids are random. Two
    rows written in the same clock tick (or after the clock stepped back) would tie and be
    listed in a random order instead of the order they were created. Making each time
    strictly later than the previous row's removes the tie. Times are always written with
    microseconds; rows written earlier without them still compare correctly because the
    comparison here is on parsed times and ordering uses ``_ORDER_KEY``.

    The caller runs this inside the write transaction (``BEGIN IMMEDIATE``) that also
    inserts the row, so concurrent writers are serialized and cannot pick the same time.
    The reads use the ``(project_id, created_at, id)`` index: the text-greatest row fixes the
    latest second (seconds always compare correctly as text), and every row from that second
    on is parsed to find the true latest time.
    """
    if table not in _TIMESTAMPED_TABLES:  # the name is interpolated into SQL below
        raise ValueError("unknown lineage table")
    if not connection.in_transaction:
        # The read below and the caller's insert must be one write transaction (the store
        # opens BEGIN IMMEDIATE); outside one, two writers could pick the same time.
        raise RuntimeError("creation times must be chosen inside the write transaction")
    moment = _parse_utc(utc_now())
    top = connection.execute(
        f"SELECT created_at FROM {table} WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    if top is not None:
        latest = max(
            _parse_utc(row[0])
            for row in connection.execute(
                f"SELECT created_at FROM {table} WHERE project_id=? AND created_at >= ?",
                (project_id, top[0][:19]),
            )
        )
        if moment <= latest:
            moment = latest + timedelta(microseconds=1)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


LINEAGE_RELATIONS = ("derived_from", "trained_from", "labels_from", "evaluated_on")
LINEAGE_NODE_KINDS = ("model_checkpoint", "dataset", "labels", "evaluation", "other")


def create_artifact_lineage_schema(connection: sqlite3.Connection) -> None:
    """Install empty, append-only lineage records without inferring history."""
    connection.execute(
        """CREATE TABLE IF NOT EXISTS artifact_lineage_nodes (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        kind TEXT NOT NULL CHECK (
            kind IN ('model_checkpoint', 'dataset', 'labels', 'evaluation', 'other')
        ),
        sha256 TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
        label TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 200),
        origin TEXT NOT NULL CHECK (origin IN ('tessera_attempt', 'external_import')),
        artifact_id TEXT REFERENCES artifacts(id),
        result_checkpoint_id TEXT REFERENCES execution_result_checkpoints(id),
        producing_attempt_id TEXT REFERENCES research_experiment_admitted_attempts(id),
        operation_id TEXT REFERENCES external_operations(operation_id),
        framework TEXT CHECK (framework IS NULL OR length(framework) BETWEEN 1 AND 100),
        format TEXT CHECK (format IS NULL OR length(format) BETWEEN 1 AND 100),
        source_uri TEXT CHECK (source_uri IS NULL OR length(source_uri) BETWEEN 1 AND 2000),
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        created_at TEXT NOT NULL,
        CHECK (origin <> 'tessera_attempt' OR producing_attempt_id IS NOT NULL),
        CHECK (origin <> 'external_import' OR producing_attempt_id IS NULL),
        CHECK (kind <> 'model_checkpoint' OR (framework IS NOT NULL AND format IS NOT NULL)),
        UNIQUE(project_id, sha256)
        )"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS artifact_lineage_nodes_by_project
        ON artifact_lineage_nodes(project_id, created_at, id)"""
    )
    # Serves the listing order (normalized creation time, then id) without a sort. Databases
    # created before this index existed still return the same order; they sort per query.
    connection.execute(
        f"""CREATE INDEX IF NOT EXISTS artifact_lineage_nodes_by_time
        ON artifact_lineage_nodes(project_id, {_ORDER_KEY}, id)"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS artifact_lineage_edges (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        child_node_id TEXT NOT NULL REFERENCES artifact_lineage_nodes(id),
        parent_node_id TEXT NOT NULL REFERENCES artifact_lineage_nodes(id),
        relation TEXT NOT NULL CHECK (
            relation IN ('derived_from', 'trained_from', 'labels_from', 'evaluated_on')
        ),
        rationale TEXT NOT NULL CHECK (length(rationale) BETWEEN 1 AND 4000),
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        created_at TEXT NOT NULL,
        CHECK (child_node_id <> parent_node_id),
        UNIQUE(child_node_id, parent_node_id, relation)
        )"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS artifact_lineage_edges_by_parent
        ON artifact_lineage_edges(parent_node_id, child_node_id)"""
    )
    connection.execute(
        f"""CREATE INDEX IF NOT EXISTS artifact_lineage_edges_by_time
        ON artifact_lineage_edges(project_id, {_ORDER_KEY}, id)"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS artifact_lineage_edges_by_project
        ON artifact_lineage_edges(project_id, created_at, id)"""
    )
    # Both endpoints must already exist in the edge's own project and tenant.
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS artifact_lineage_edges_same_project
        BEFORE INSERT ON artifact_lineage_edges
        WHEN (SELECT count(*) FROM artifact_lineage_nodes
              WHERE id IN (NEW.child_node_id, NEW.parent_node_id)
              AND project_id=NEW.project_id AND org_id=NEW.org_id) <> 2
        BEGIN SELECT RAISE(ABORT, 'lineage edge endpoints must share its project'); END"""
    )
    for table in ("artifact_lineage_nodes", "artifact_lineage_edges"):
        for operation in ("UPDATE", "DELETE"):
            connection.execute(
                f"""CREATE TRIGGER IF NOT EXISTS {table}_append_only_{operation.lower()}
                BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT, 'artifact lineage records are append-only'); END"""
            )


AUTHORITATIVE_SCOPE = "attributed_lineage_bookkeeping_only"
EXPORT_MAX_NODES = 10_000
EXPORT_MAX_EDGES = 50_000
MAX_LINEAGE_DEPTH = 64

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=100)]
RecordId = Annotated[str, StringConstraints(min_length=1, max_length=200)]

_NODE_COLUMNS = (
    "id, org_id, project_id, kind, sha256, label, origin, artifact_id, result_checkpoint_id, "
    "producing_attempt_id, operation_id, framework, format, source_uri, "
    "created_by_principal_id, created_at"
)
_EDGE_COLUMNS = (
    "id, org_id, project_id, child_node_id, parent_node_id, relation, rationale, "
    "created_by_principal_id, created_at"
)


class LineageNodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["model_checkpoint", "dataset", "labels", "evaluation", "other"]
    sha256: Sha256
    label: str = Field(min_length=1, max_length=200)
    origin: Literal["tessera_attempt", "external_import"]
    artifact_id: RecordId | None = None
    result_checkpoint_id: RecordId | None = None
    producing_attempt_id: RecordId | None = None
    operation_id: RecordId | None = None
    framework: ShortText | None = None
    format: ShortText | None = None
    source_uri: str | None = Field(default=None, min_length=1, max_length=2000)


class LineageEdgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    child_node_id: RecordId
    parent_node_id: RecordId
    relation: Literal["derived_from", "trained_from", "labels_from", "evaluated_on"]
    rationale: str = Field(min_length=1, max_length=4000)


def _scoped(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    value = dict(row)
    value["authoritative_scope"] = AUTHORITATIVE_SCOPE
    return value


def lineage_export_payload(connection: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    """Deterministic, bounded lineage export for one project (caller checks tenancy)."""
    node_count = connection.execute(
        "SELECT count(*) FROM artifact_lineage_nodes WHERE project_id=?", (project_id,)
    ).fetchone()[0]
    edge_count = connection.execute(
        "SELECT count(*) FROM artifact_lineage_edges WHERE project_id=?", (project_id,)
    ).fetchone()[0]
    if node_count > EXPORT_MAX_NODES or edge_count > EXPORT_MAX_EDGES:
        raise Conflict("Artifact lineage exceeds the bounded export size.")
    nodes = [
        dict(row)
        for row in connection.execute(
            f"SELECT {_NODE_COLUMNS} FROM artifact_lineage_nodes WHERE project_id=? {_ORDER}",
            (project_id,),
        )
    ]
    edges = [
        dict(row)
        for row in connection.execute(
            f"SELECT {_EDGE_COLUMNS} FROM artifact_lineage_edges WHERE project_id=? {_ORDER}",
            (project_id,),
        )
    ]
    return {
        "version": 1,
        "project_id": project_id,
        "nodes": nodes,
        "edges": edges,
        "digest": canonical_digest({"nodes": nodes, "edges": edges}),
        "authoritative_scope": AUTHORITATIVE_SCOPE,
    }


class ArtifactLineageStore(Store):
    """Human-attributed, append-only lineage bookkeeping; never proves bytes or science."""

    @staticmethod
    def _project(connection: sqlite3.Connection, actor: dict[str, Any], project_id: str) -> None:
        row = connection.execute(
            "SELECT id FROM projects WHERE id=? AND org_id=?", (project_id, actor["org_id"])
        ).fetchone()
        if row is None or (actor["project_id"] is not None and actor["project_id"] != project_id):
            raise NotFound("Project was not found.")

    @staticmethod
    def _node_row(connection: sqlite3.Connection, project_id: str, node_id: str) -> sqlite3.Row:
        row = connection.execute(
            f"SELECT {_NODE_COLUMNS} FROM artifact_lineage_nodes WHERE id=? AND project_id=?",
            (node_id, project_id),
        ).fetchone()
        if row is None:
            raise NotFound("Lineage node was not found.")
        return row

    def register_node(
        self, actor: dict[str, Any], project_id: str, key: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        """Register one byte identity; model_checkpoint needs framework+format, a
        tessera_attempt origin needs an admitted attempt, an external_import forbids one.
        Every referenced record must belong to the same project."""
        try:
            body = LineageNodeRequest.model_validate(payload).model_dump(mode="json")
        except ValidationError as error:
            raise StoreError("Invalid artifact lineage node request.") from error
        if body["kind"] == "model_checkpoint" and not (body["framework"] and body["format"]):
            raise StoreError("A model_checkpoint node requires framework and format.")
        if body["origin"] == "tessera_attempt" and body["producing_attempt_id"] is None:
            raise StoreError("A tessera_attempt node requires producing_attempt_id.")
        if body["origin"] == "external_import" and body["producing_attempt_id"] is not None:
            raise StoreError("An external_import node must not name a producing attempt.")
        path = f"/projects/{project_id}/artifact-lineage/nodes"

        def guard(connection: sqlite3.Connection, live: dict[str, Any], _: Any = None) -> None:
            if live["role"] != "human":
                raise Forbidden("Human artifact lineage authority is required.")
            self._project(connection, live, project_id)

        def command(connection: sqlite3.Connection, live: dict[str, Any]):
            guard(connection, live)
            artifact = checkpoint = attempt = None
            if body["artifact_id"] is not None:
                artifact = connection.execute(
                    "SELECT sha256, operation_id FROM artifacts WHERE id=? AND project_id=?",
                    (body["artifact_id"], project_id),
                ).fetchone()
                if artifact is None:
                    raise NotFound("Artifact was not found.")
                if artifact["sha256"] != body["sha256"]:
                    raise Conflict("Artifact sha256 does not match the lineage node sha256.")
            if body["result_checkpoint_id"] is not None:
                checkpoint = connection.execute(
                    "SELECT operation_id FROM execution_result_checkpoints "
                    "WHERE id=? AND project_id=?",
                    (body["result_checkpoint_id"], project_id),
                ).fetchone()
                if checkpoint is None:
                    raise NotFound("Result checkpoint was not found.")
            if body["producing_attempt_id"] is not None:
                attempt = connection.execute(
                    """SELECT a.operation_id FROM research_experiment_admitted_attempts a
                    JOIN tasks t ON t.id = a.task_id WHERE a.id=? AND t.project_id=?""",
                    (body["producing_attempt_id"], project_id),
                ).fetchone()
                if attempt is None:
                    raise NotFound("Admitted attempt was not found.")
            known = {
                value
                for value in (
                    artifact["operation_id"] if artifact else None,
                    checkpoint["operation_id"] if checkpoint else None,
                    attempt["operation_id"] if attempt else None,
                    body["operation_id"],
                )
                if value is not None
            }
            if len(known) > 1:
                raise Conflict("Lineage node references name different operations.")
            if body["operation_id"] is not None:
                operation = connection.execute(
                    "SELECT operation_id FROM external_operations "
                    "WHERE operation_id=? AND project_id=?",
                    (body["operation_id"], project_id),
                ).fetchone()
                if operation is None:
                    raise NotFound("External operation was not found.")
            if connection.execute(
                "SELECT 1 FROM artifact_lineage_nodes WHERE project_id=? AND sha256=?",
                (project_id, body["sha256"]),
            ).fetchone():
                raise Conflict("A lineage node with this sha256 already exists in the project.")
            row = {
                **body,
                "id": "lineage_node_" + uuid.uuid4().hex,
                "org_id": live["org_id"],
                "project_id": project_id,
                "created_by_principal_id": live["id"],
                "created_at": _created_at(connection, "artifact_lineage_nodes", project_id),
            }
            columns = [name.strip() for name in _NODE_COLUMNS.split(",")]
            connection.execute(
                f"INSERT INTO artifact_lineage_nodes ({', '.join(columns)}) "
                f"VALUES ({', '.join(':' + name for name in columns)})",
                row,
            )
            self._event(
                connection,
                live,
                project_id,
                None,
                "human.artifact_lineage_node_registered",
                1,
                {"node_id": row["id"], "kind": row["kind"], "sha256": row["sha256"]},
            )
            return 201, _scoped({name: row[name] for name in columns})

        return self._mutate(
            actor, key, "POST", path, canonical_digest(body), command, replay_guard=guard
        )

    def record_edge(
        self, actor: dict[str, Any], project_id: str, key: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        """Record child -> parent derivation. Rules: no self edge; no cycle (the child may
        not already be an ancestor of the parent, across all relations); trained_from needs
        a model_checkpoint child; labels_from needs a labels/dataset child and a
        model_checkpoint (teacher) parent; evaluated_on needs an evaluation or
        model_checkpoint child; derived_from is unrestricted."""
        try:
            body = LineageEdgeRequest.model_validate(payload).model_dump(mode="json")
        except ValidationError as error:
            raise StoreError("Invalid artifact lineage edge request.") from error
        if body["child_node_id"] == body["parent_node_id"]:
            raise StoreError("A lineage node cannot be derived from itself.")
        path = f"/projects/{project_id}/artifact-lineage/edges"

        def guard(connection: sqlite3.Connection, live: dict[str, Any], _: Any = None) -> None:
            if live["role"] != "human":
                raise Forbidden("Human artifact lineage authority is required.")
            self._project(connection, live, project_id)

        def command(connection: sqlite3.Connection, live: dict[str, Any]):
            guard(connection, live)
            child = self._node_row(connection, project_id, body["child_node_id"])
            parent = self._node_row(connection, project_id, body["parent_node_id"])
            relation = body["relation"]
            if relation == "trained_from" and child["kind"] != "model_checkpoint":
                raise Conflict("trained_from requires a model_checkpoint child.")
            if relation == "labels_from" and (
                child["kind"] not in ("labels", "dataset") or parent["kind"] != "model_checkpoint"
            ):
                raise Conflict(
                    "labels_from requires a labels or dataset child and a model_checkpoint parent."
                )
            if relation == "evaluated_on" and child["kind"] not in (
                "evaluation",
                "model_checkpoint",
            ):
                raise Conflict("evaluated_on requires an evaluation or model_checkpoint child.")
            cyclic = connection.execute(
                """WITH RECURSIVE up(node_id) AS (
                    SELECT ? UNION
                    SELECT e.parent_node_id FROM artifact_lineage_edges e
                    JOIN up ON e.child_node_id = up.node_id WHERE e.project_id = ?
                ) SELECT 1 FROM up WHERE node_id = ?""",
                (body["parent_node_id"], project_id, body["child_node_id"]),
            ).fetchone()
            if cyclic:
                raise Conflict("Lineage edge would create a cycle.")
            if connection.execute(
                "SELECT 1 FROM artifact_lineage_edges "
                "WHERE child_node_id=? AND parent_node_id=? AND relation=?",
                (body["child_node_id"], body["parent_node_id"], relation),
            ).fetchone():
                raise Conflict("This lineage edge already exists.")
            row = {
                **body,
                "id": "lineage_edge_" + uuid.uuid4().hex,
                "org_id": live["org_id"],
                "project_id": project_id,
                "created_by_principal_id": live["id"],
                "created_at": _created_at(connection, "artifact_lineage_edges", project_id),
            }
            columns = [name.strip() for name in _EDGE_COLUMNS.split(",")]
            connection.execute(
                f"INSERT INTO artifact_lineage_edges ({', '.join(columns)}) "
                f"VALUES ({', '.join(':' + name for name in columns)})",
                row,
            )
            self._event(
                connection,
                live,
                project_id,
                None,
                "human.artifact_lineage_edge_recorded",
                1,
                {
                    "edge_id": row["id"],
                    "child_node_id": row["child_node_id"],
                    "parent_node_id": row["parent_node_id"],
                    "relation": relation,
                },
            )
            return 201, _scoped({name: row[name] for name in columns})

        return self._mutate(
            actor, key, "POST", path, canonical_digest(body), command, replay_guard=guard
        )

    def _read(self, actor: dict[str, Any], project_id: str, reader: Any) -> Any:
        connection = _connect(self.database_path)
        try:
            live = self._live_principal(connection, actor)
            self._project(connection, live, project_id)
            return reader(connection)
        finally:
            connection.close()

    def get_node(self, actor: dict[str, Any], project_id: str, node_id: str) -> dict[str, Any]:
        return self._read(
            actor,
            project_id,
            lambda connection: _scoped(self._node_row(connection, project_id, node_id)),
        )

    def list_nodes(
        self,
        actor: dict[str, Any],
        project_id: str,
        offset: int,
        limit: int,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        if not 0 <= offset <= 2**63 - 1 or not 1 <= limit <= 200:
            raise StoreError("Invalid page bounds.")
        if kind is not None and kind not in LINEAGE_NODE_KINDS:
            raise StoreError("Invalid lineage node kind.")

        def reader(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = connection.execute(
                f"SELECT {_NODE_COLUMNS} FROM artifact_lineage_nodes "
                "WHERE project_id=? AND (? IS NULL OR kind=?) "
                f"{_ORDER} LIMIT ? OFFSET ?",
                (project_id, kind, kind, limit, offset),
            ).fetchall()
            return [_scoped(row) for row in rows]

        return self._read(actor, project_id, reader)

    def lineage(
        self,
        actor: dict[str, Any],
        project_id: str,
        node_id: str,
        direction: str,
        max_depth: int = MAX_LINEAGE_DEPTH,
    ) -> dict[str, Any]:
        """Nodes (with minimum depth) and edges among them reachable from node_id."""
        if direction not in ("ancestors", "descendants"):
            raise StoreError("Direction must be ancestors or descendants.")
        if not 1 <= max_depth <= MAX_LINEAGE_DEPTH:
            raise StoreError("Invalid lineage depth.")
        start, step = ("child_node_id", "parent_node_id")
        if direction == "descendants":
            start, step = step, start

        def reader(connection: sqlite3.Connection) -> dict[str, Any]:
            self._node_row(connection, project_id, node_id)
            reached = connection.execute(
                f"""WITH RECURSIVE walk(node_id, depth) AS (
                    SELECT ?, 0 UNION
                    SELECT e.{step}, walk.depth + 1 FROM artifact_lineage_edges e
                    JOIN walk ON e.{start} = walk.node_id
                    WHERE e.project_id = ? AND walk.depth < ?
                ) SELECT node_id, min(depth) AS depth FROM walk GROUP BY node_id""",
                (node_id, project_id, max_depth),
            ).fetchall()
            depths = {row["node_id"]: row["depth"] for row in reached}
            nodes = []
            for row in connection.execute(
                f"SELECT {_NODE_COLUMNS} FROM artifact_lineage_nodes WHERE project_id=? {_ORDER}",
                (project_id,),
            ):
                if row["id"] in depths:
                    nodes.append({**dict(row), "depth": depths[row["id"]]})
            edges = [
                dict(row)
                for row in connection.execute(
                    f"SELECT {_EDGE_COLUMNS} FROM artifact_lineage_edges "
                    f"WHERE project_id=? {_ORDER}",
                    (project_id,),
                )
                if row["child_node_id"] in depths and row["parent_node_id"] in depths
            ]
            return {
                "node_id": node_id,
                "direction": direction,
                "max_depth": max_depth,
                "nodes": nodes,
                "edges": edges,
                "authoritative_scope": AUTHORITATIVE_SCOPE,
            }

        return self._read(actor, project_id, reader)

    def export_lineage(self, actor: dict[str, Any], project_id: str) -> dict[str, Any]:
        return self._read(
            actor, project_id, lambda connection: lineage_export_payload(connection, project_id)
        )
