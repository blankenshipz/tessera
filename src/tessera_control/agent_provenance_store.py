"""Authenticated remote provenance intake; recording never grants authority."""

import hashlib
import json

from .agent_provenance import _body, record_agent_provenance
from .agent_provenance_models import AppendAgentProvenance
from .external_agents import ExternalAgentStore
from .store import Conflict, Forbidden, canonical_digest


class AgentProvenanceStore(ExternalAgentStore):
    @staticmethod
    def _validate_references(conn, actor, project_id, body):
        try:
            record = _body(body["record"])
        except ValueError as error:
            raise Forbidden(
                "Provenance envelope requires content-free opaque identities."
            ) from error
        refs = record["references"]
        destination_digest = (
            "sha256:" + hashlib.sha256(actor["agent_destination"].encode()).hexdigest()
        )
        if record["remote"]["destination_digest"] not in {None, destination_digest}:
            raise Forbidden("Remote provenance destination attribution differs from enrollment.")
        record["remote"]["destination_digest"] = destination_digest
        if not any(refs.values()):
            raise Forbidden("Remote provenance requires a durable project-owned anchor.")
        if record["tool_id"] and record["tool_id"].startswith(
            ("human.", "approval.", "decision.", "authority.issue", "authority.approve")
        ):
            raise Forbidden("Agents cannot attribute human approval or decision events.")
        queries = {
            "study_id": "SELECT 1 FROM research_studies WHERE id=? AND project_id=?",
            "experiment_id": "SELECT 1 FROM research_experiments WHERE id=? AND project_id=?",
            "task_id": "SELECT 1 FROM tasks WHERE id=? AND project_id=?",
            "artifact_id": "SELECT 1 FROM artifacts WHERE id=? AND project_id=?",
            "operation_id": "SELECT 1 FROM external_operations "
            "WHERE operation_id=? AND project_id=?",
            "permission_id": "SELECT 1 FROM execution_grants WHERE id=? AND project_id=?",
            "control_event_id": "SELECT 1 FROM events "
            "WHERE CAST(cursor AS TEXT)=? AND project_id=?",
            "attempt_id": "SELECT 1 FROM research_experiment_admitted_attempts a "
            "JOIN research_experiments e ON e.id=a.experiment_id WHERE a.id=? AND e.project_id=?",
            "variant_id": "SELECT 1 FROM research_experiment_variants v "
            "JOIN research_experiments e ON e.id=v.experiment_id WHERE v.id=? AND e.project_id=?",
            "provenance_record_id": "SELECT 1 FROM agent_provenance "
            "WHERE record_id=? AND project_id=?",
        }
        for key, value in refs.items():
            if value is None or key in {
                "grant_id",
                "consumption_id",
                "context_id",
                "output_manifest_id",
                "provider_job_uid",
            }:
                continue
            if (
                key not in queries
                or conn.execute(queries[key], (value, project_id)).fetchone() is None
            ):
                raise Forbidden("Provenance reference is not a supported project-owned record.")
        context_id = refs["context_id"]
        if (
            context_id
            and conn.execute(
                "SELECT 1 FROM external_agent_contexts WHERE id=? AND project_id=? "
                "AND credential_id=? AND destination=?",
                (context_id, project_id, actor["credential_id"], actor["agent_destination"]),
            ).fetchone()
            is None
        ):
            raise Forbidden("Context reference is outside this agent's disclosure scope.")
        proof = body["grant_proof"]
        if refs["grant_id"]:
            if proof is None or proof["grant_id"] != refs["grant_id"]:
                raise Forbidden("Grant provenance requires exact grant revision and epoch proof.")
            grant = conn.execute(
                "SELECT * FROM agent_authority_grants WHERE id=? AND org_id=? AND project_id=? "
                "AND subject_credential_id=?",
                (refs["grant_id"], actor["org_id"], project_id, actor["credential_id"]),
            ).fetchone()
            if grant is None:
                raise Forbidden("Grant reference is outside this agent's project scope.")
            if (
                grant["revision"] != proof["expected_revision"]
                or grant["epoch"] != proof["expected_epoch"]
            ):
                raise Conflict("Grant provenance revision or epoch is stale.")
            if record["grant_revision"] not in {None, grant["revision"]} or record[
                "grant_epoch"
            ] not in {None, grant["epoch"]}:
                raise Conflict("Provenance grant attribution differs from supplied proof.")
            record["grant_revision"], record["grant_epoch"] = grant["revision"], grant["epoch"]
            if json.loads(grant["bounds_json"])["destination"] != actor["agent_destination"]:
                raise Forbidden("Grant destination differs from enrolled destination.")
        elif proof is not None:
            raise Forbidden("Grant proof requires the corresponding central grant reference.")
        elif record["grant_revision"] is not None or record["grant_epoch"] is not None:
            raise Forbidden("Grant revision attribution requires a proven central grant reference.")
        if refs["consumption_id"] and (
            not refs["grant_id"]
            or conn.execute(
                "SELECT 1 FROM agent_authority_consumptions WHERE id=? AND grant_id=? "
                "AND credential_id=?",
                (refs["consumption_id"], refs["grant_id"], actor["credential_id"]),
            ).fetchone()
            is None
        ):
            raise Forbidden("Consumption reference differs from the attributed agent and grant.")
        manifest_id = refs.get("output_manifest_id")
        proof = body["output_proof"]
        if manifest_id:
            from .agent_provenance_outputs import (
                _manifest,
                disclosed_artifacts,
                record_output_provider_binding,
            )

            if proof is None or proof["output_manifest_id"] != manifest_id:
                raise Forbidden("Output provenance requires an exact manifest digest proof.")
            manifest = _manifest(conn, actor, project_id, manifest_id)
            if proof["manifest_digest"] != manifest["manifest_digest"]:
                raise Conflict("Output manifest digest proof is stale or mismatched.")
            visible = disclosed_artifacts(conn, actor, manifest)
            if not visible or refs.get("artifact_id") and refs["artifact_id"] not in visible:
                raise Forbidden("Output provenance requires current artifact disclosure.")
            for field, expected in (
                ("operation_id", manifest["operation_id"]),
                ("task_id", manifest["task_id"]),
                ("permission_id", manifest["grant_id"]),
            ):
                if refs.get(field) not in {None, expected}:
                    raise Conflict(
                        "Output provenance references disagree with the registered manifest."
                    )
            if refs.get("attempt_id"):
                attempt = conn.execute(
                    "SELECT operation_id FROM research_experiment_admitted_attempts WHERE id=?",
                    (refs["attempt_id"],),
                ).fetchone()
                if attempt["operation_id"] != manifest["operation_id"]:
                    raise Conflict("Output provenance attempt belongs to a different operation.")
            if proof["provider_job"] is not None:
                binding = record_output_provider_binding(
                    conn, actor, project_id, manifest_id, proof["provider_job"]
                )
                if refs.get("provider_job_uid") not in {None, binding["job_uid"]}:
                    raise Conflict("Output provenance Job UID differs from the recorded binding.")
            elif refs.get("provider_job_uid"):
                binding = conn.execute(
                    "SELECT job_uid FROM agent_output_provider_bindings WHERE output_manifest_id=?",
                    (manifest_id,),
                ).fetchone()
                if binding is None or binding["job_uid"] != refs["provider_job_uid"]:
                    raise Forbidden("Job UID reference has no matching central output binding.")
        elif proof is not None or refs.get("provider_job_uid"):
            raise Forbidden("Output proof or Job UID requires a central output manifest reference.")
        elif refs.get("artifact_id"):
            raise Forbidden("Artifact provenance requires a manifest proof and current disclosure.")
        if record.get("generated_output") is not None:
            from .agent_provenance_generated import validate_generated_output_record

            if body["output_proof"] is not None:
                raise Forbidden("Research output provenance cannot carry a worker manifest proof.")
            validate_generated_output_record(conn, actor, project_id, record)
        if (
            record.get("harness", {}).get("input_context_id") is not None
            or record.get("guard_phase") is not None
        ):
            from .agent_provenance_harness import validate_public_harness_record

            validate_public_harness_record(conn, actor, project_id, record)
        return record

    def append(self, actor, project_id, key, payload):
        body = AppendAgentProvenance.model_validate(payload).model_dump(mode="json")
        digest_body = json.loads(json.dumps(body))
        if digest_body["output_proof"] is None:
            # Preserve the canonical request shape of pre-output-link outboxes.
            digest_body.pop("output_proof")
            for field in ("output_manifest_id", "provider_job_uid"):
                if digest_body["record"]["references"].get(field) is None:
                    digest_body["record"]["references"].pop(field, None)
        path = f"/projects/{project_id}/agent-provenance"

        def validate(conn, live):
            if live["kind"] != "agent" or live["project_id"] != project_id:
                raise Forbidden("A project-bound external agent is required.")
            self._project(conn, live, project_id)
            self._scope(conn, live, "provenance:append")
            return self._validate_references(conn, live, project_id, body)

        def command(conn, live):
            record = validate(conn, live)
            existing = conn.execute(
                "SELECT 1 FROM agent_provenance WHERE org_id=? AND project_id=? AND record_id=?",
                (live["org_id"], project_id, record["record_id"]),
            ).fetchone()
            result = record_agent_provenance(conn, live, project_id, record)
            if existing is None:
                self._event(
                    conn,
                    live,
                    project_id,
                    None,
                    "agent_provenance.recorded",
                    1,
                    {
                        "provenance_record_id": result["record_id"],
                        "sequence": result["sequence"],
                        "outcome": result["outcome"],
                        "attribution": "self_reported",
                    },
                )
            return 201, result

        return self._mutate(
            actor,
            key,
            "POST",
            path,
            canonical_digest(digest_body),
            command,
            replay_guard=lambda conn, live, response: validate(conn, live),
        )
