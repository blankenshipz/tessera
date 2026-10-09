"""Immutable SQLite artifact bytes without fabricated worker/execution identity."""

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

from .external_agents import ExternalAgentStore
from .store import Conflict, Forbidden, NotFound, StoreError, _connect, canonical_digest, utc_now


def create_generated_output_schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS generated_output_grants (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        issued_by TEXT NOT NULL REFERENCES principals(id), bounds_json TEXT NOT NULL,
        revision INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
        used_outputs INTEGER NOT NULL DEFAULT 0, used_bytes INTEGER NOT NULL DEFAULT 0)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS generated_public_outputs (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        grant_id TEXT NOT NULL REFERENCES generated_output_grants(id),
        actor_id TEXT NOT NULL REFERENCES principals(id), credential_id TEXT NOT NULL,
        sha256 TEXT NOT NULL, size INTEGER NOT NULL, content BLOB NOT NULL,
        metadata_json TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(grant_id,sha256))""")
    connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS generated_output_source_identity
        ON generated_public_outputs(grant_id,json_extract(metadata_json,'$.source_event_id'),
        json_extract(metadata_json,'$.source_sequence'))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS generated_output_disclosures (
        id TEXT PRIMARY KEY, output_id TEXT NOT NULL REFERENCES generated_public_outputs(id),
        issued_by TEXT NOT NULL REFERENCES principals(id), bounds_json TEXT NOT NULL,
        revision INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)""")
    for action in ("UPDATE", "DELETE"):
        connection.execute(f"""CREATE TRIGGER IF NOT EXISTS generated_output_no_{action.lower()}
            BEFORE {action} ON generated_public_outputs BEGIN
            SELECT RAISE(ABORT,'Generated outputs are immutable'); END""")


class GeneratedOutputStore(ExternalAgentStore):
    @staticmethod
    def _expiry(value):
        expiry = datetime.fromisoformat(value)
        if expiry.tzinfo is None or expiry <= datetime.now(UTC):
            raise Conflict("Output authority expired.")
        return expiry

    def _grant(self, connection, actor, grant_id):
        row = connection.execute(
            "SELECT * FROM generated_output_grants WHERE id=?", (grant_id,)
        ).fetchone()
        if row is None:
            raise NotFound("Output grant not found.")
        self._project(connection, actor, row["project_id"])
        bounds = json.loads(row["bounds_json"])
        self._live_principal(connection, {"id": row["issued_by"], "org_id": actor["org_id"]})
        if row["revoked"]:
            raise Conflict("Output grant revoked.")
        self._expiry(bounds["expires_at"])
        return row, bounds

    def _registration_guard(self, connection, actor, body):
        self._scope(connection, actor, "provenance:append")
        grant, bounds = self._grant(connection, actor, body["grant_id"])
        if (
            actor["role"] == "human"
            or actor["credential_id"] != bounds["credential_id"]
            or actor["agent_destination"] != bounds["destination"]
            or grant["revision"] != body["expected_revision"]
            or bounds["pins"] != body["pins"]
            or bounds["output_kind"] != body["output_kind"]
        ):
            raise Forbidden("Output identity, pins or grant fence mismatch.")
        context = self._context(connection, actor, bounds["pins"]["context_id"])
        if context["project_id"] != grant["project_id"] or not context["disclosure_approved"]:
            raise Forbidden("Explicit context disclosure required.")
        return grant, bounds

    def issue(self, actor, project_id, key, body):
        path = f"/projects/{project_id}/generated-output-grants"

        def command(connection, live):
            self._recipient(connection, live, project_id, body)
            if self._expiry(body["expires_at"]) > datetime.now(UTC) + timedelta(hours=24):
                raise Conflict("Output admission exceeds 24 hours.")
            context = self._context(connection, live, body["pins"]["context_id"])
            if (
                context["project_id"] != project_id
                or context["credential_id"] != body["credential_id"]
                or context["destination"] != body["destination"]
                or not context["disclosure_approved"]
            ):
                raise Forbidden("Admission must bind the recipient's disclosed context.")
            if body["pins"]["task_id"] is not None:
                self._task(connection, live, body["pins"]["task_id"])
                task = connection.execute(
                    "SELECT project_id FROM tasks WHERE id=?", (body["pins"]["task_id"],)
                ).fetchone()
                if task["project_id"] != project_id:
                    raise Forbidden("Task project mismatch.")
            if body["pins"]["operation_id"] is not None:
                op = connection.execute(
                    "SELECT * FROM external_operations WHERE operation_id=?",
                    (body["pins"]["operation_id"],),
                ).fetchone()
                if (
                    op is None
                    or op["project_id"] != project_id
                    or op["task_id"] != body["pins"]["task_id"]
                ):
                    raise Forbidden("Operation binding mismatch.")
            ident = "output_grant_" + uuid.uuid4().hex
            connection.execute(
                "INSERT INTO generated_output_grants(id,project_id,issued_by,bounds_json,revision) "
                "VALUES (?,?,?,?,1)",
                (ident, project_id, live["id"], json.dumps(body)),
            )
            self._event(
                connection,
                live,
                project_id,
                None,
                "generated_output.admitted",
                1,
                {
                    "grant_id": ident,
                    "credential_id": body["credential_id"],
                    "execution_approval": False,
                },
            )
            return 201, {"id": ident, "revision": 1, "bounds": body}

        def guard(connection, live, result):
            self._human(live)
            self._grant(connection, live, result["id"])
            self._recipient(connection, live, project_id, body)

        return self._mutate(
            actor, key, "POST", path, canonical_digest(body), command, replay_guard=guard
        )

    def register(self, actor, project_id, key, body):
        path = f"/projects/{project_id}/generated-outputs"
        try:
            content = base64.b64decode(body["content_base64"], validate=True)
            text = content.decode("utf-8")
        except (ValueError, UnicodeError) as error:
            raise StoreError("Public output must be bounded UTF-8 bytes.") from error
        if (
            not content
            or len(content) > 65536
            or "\x00" in text
            or hashlib.sha256(content).hexdigest() != body["sha256"]
        ):
            raise StoreError("Public output size/hash mismatch.")

        def command(connection, live):
            grant, bounds = self._registration_guard(connection, live, body)
            if grant["project_id"] != project_id:
                raise NotFound("Output grant not in project.")
            if (
                grant["used_outputs"] >= bounds["max_outputs"]
                or grant["used_bytes"] + len(content) > bounds["max_total_bytes"]
            ):
                raise Conflict("Output reservation budget exhausted.")
            if connection.execute(
                "SELECT 1 FROM generated_public_outputs WHERE grant_id=? AND sha256=?",
                (grant["id"], body["sha256"]),
            ).fetchone():
                raise Conflict("Output already registered; use original idempotency key.")
            if connection.execute(
                "SELECT 1 FROM generated_public_outputs WHERE grant_id=? AND "
                "json_extract(metadata_json,'$.source_event_id')=? AND "
                "json_extract(metadata_json,'$.source_sequence')=?",
                (grant["id"], body["source_event_id"], body["source_sequence"]),
            ).fetchone():
                raise Conflict("Public source identity already registered.")
            ident = "generated_output_" + uuid.uuid4().hex
            metadata = {k: v for k, v in body.items() if k != "content_base64"}
            metadata.update(
                id=ident,
                artifact_id=ident,
                content_digest="sha256:" + body["sha256"],
                actor_id=live["id"],
                credential_id=live["credential_id"],
                org_id=live["org_id"],
                scientific_validity="not_assessed",
                attribution="registered_self_reported_generated_output",
                custody_state="human_admitted_public",
                execution_established=False,
            )
            connection.execute(
                "INSERT INTO generated_public_outputs VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    ident,
                    project_id,
                    grant["id"],
                    live["id"],
                    live["credential_id"],
                    body["sha256"],
                    len(content),
                    content,
                    json.dumps(metadata),
                    utc_now(),
                ),
            )
            connection.execute(
                "UPDATE generated_output_grants SET used_outputs=used_outputs+1,"
                "used_bytes=used_bytes+? WHERE id=?",
                (len(content), grant["id"]),
            )
            self._event(
                connection,
                live,
                project_id,
                body["pins"]["task_id"],
                "generated_output.registered",
                1,
                metadata,
            )
            return 201, metadata | {"size": len(content)}

        def guard(connection, live, response):
            grant, _ = self._registration_guard(connection, live, body)
            if grant["project_id"] != project_id:
                raise NotFound("Output not in project.")

        return self._mutate(
            actor, key, "POST", path, canonical_digest(body), command, replay_guard=guard
        )

    def disclose(self, actor, output_id, key, body):
        path = f"/generated-outputs/{output_id}/disclosures"

        def command(connection, live):
            output = self._output(connection, live, output_id)
            self._recipient(connection, live, output["project_id"], body)
            self._expiry(body["expires_at"])
            ident = "output_disclosure_" + uuid.uuid4().hex
            connection.execute(
                "INSERT INTO generated_output_disclosures VALUES (?,?,?,?,1,0)",
                (ident, output_id, live["id"], json.dumps(body)),
            )
            self._event(
                connection,
                live,
                output["project_id"],
                None,
                "generated_output.disclosed",
                1,
                {
                    "output_id": output_id,
                    "disclosure_id": ident,
                    "credential_id": body["credential_id"],
                },
            )
            return 201, {"id": ident, "output_id": output_id, "revision": 1}

        def guard(connection, live, result):
            output = self._output(connection, live, output_id)
            self._recipient(connection, live, output["project_id"], body)
            row = connection.execute(
                "SELECT revoked FROM generated_output_disclosures WHERE id=?", (result["id"],)
            ).fetchone()
            self._expiry(body["expires_at"])
            if row is None or row["revoked"]:
                raise Conflict("Output disclosure revoked.")

        return self._mutate(
            actor, key, "POST", path, canonical_digest(body), command, replay_guard=guard
        )

    def _output(self, connection, actor, output_id):
        row = connection.execute(
            "SELECT * FROM generated_public_outputs WHERE id=?", (output_id,)
        ).fetchone()
        if row is None:
            raise NotFound("Generated output not found.")
        self._project(connection, actor, row["project_id"])
        return row

    def read(self, actor, output_id, disclosure_id=None):
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN")
            live = self._actor(connection, actor)
            row = self._output(connection, live, output_id)
            if live["role"] != "human":
                self._scope(connection, live, "context:read")
                disclosure = connection.execute(
                    "SELECT * FROM generated_output_disclosures WHERE id=? AND output_id=?",
                    (disclosure_id, output_id),
                ).fetchone()
                if disclosure is None or disclosure["revoked"]:
                    raise Forbidden("Current human output disclosure required.")
                bounds = json.loads(disclosure["bounds_json"])
                self._live_principal(
                    connection, {"id": disclosure["issued_by"], "org_id": live["org_id"]}
                )
                self._expiry(bounds["expires_at"])
                if (
                    bounds["credential_id"] != live["credential_id"]
                    or bounds["destination"] != live["agent_destination"]
                ):
                    raise Forbidden("Output recipient mismatch.")
            if (
                hashlib.sha256(row["content"]).hexdigest() != row["sha256"]
                or len(row["content"]) != row["size"]
            ):
                raise Conflict("Output integrity failure.")
            return json.loads(row["metadata_json"]) | {
                "project_id": row["project_id"],
                "actor_id": row["actor_id"],
                "credential_id": row["credential_id"],
                "size": row["size"],
                "content_base64": base64.b64encode(row["content"]).decode(),
            }
        finally:
            connection.close()

    def revoke(self, actor, ident, key, body, *, disclosure=False):
        table = "generated_output_disclosures" if disclosure else "generated_output_grants"
        resource = "generated-output-disclosures" if disclosure else "generated-output-grants"
        path = f"/{resource}/{ident}/revoke"

        def command(connection, live):
            self._human(live)
            row = connection.execute(f"SELECT * FROM {table} WHERE id=?", (ident,)).fetchone()
            if row is None:
                raise NotFound("Output authority not found.")
            project_id = (
                self._output(connection, live, row["output_id"])["project_id"]
                if disclosure
                else row["project_id"]
            )
            self._project(connection, live, project_id)
            if row["revision"] != body["expected_revision"] or row["revoked"]:
                raise Conflict("Output authority fence changed.")
            connection.execute(
                f"UPDATE {table} SET revoked=1,revision=revision+1 WHERE id=?", (ident,)
            )
            self._event(
                connection,
                live,
                project_id,
                None,
                "generated_output.authority_revoked",
                row["revision"] + 1,
                {"id": ident},
            )
            return 200, {"id": ident, "revision": row["revision"] + 1, "revoked": True}

        return self._mutate(actor, key, "POST", path, canonical_digest(body), command)
