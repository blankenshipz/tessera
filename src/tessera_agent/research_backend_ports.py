"""Native authenticated backend readers; human control records are approved contexts.

All bodies are ephemeral. A control record is not an execution grant: promotion
and current separately issued authority remain mandatory at the mutation boundary.
"""

import hashlib
import json
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from tessera_control.public_capture import CaptureBinding
from tessera_control.public_capture_contexts import validate_separated_resolution

from .research_consumer_store import ConsumerBlocked


def resolve_contexts(endpoint, binding):
    pins = CaptureBinding.model_validate(binding).model_dump(mode="json")
    reply = endpoint.call(
        "POST", f"/projects/{pins['project_id']}/agent-capture-contexts/resolve", pins
    )
    if (
        not isinstance(reply, dict)
        or set(reply)
        != {"verified_binding", "resolution", "verification_scope", "authority_effect"}
        or reply["verified_binding"] != pins
        or reply["verification_scope"] != "current_issuer_context_policy_and_input_digest"
        or reply["authority_effect"] != "none"
    ):
        raise ConsumerBlocked("native_context_resolution_receipt_changed")
    return validate_separated_resolution(pins, reply["resolution"])


class HumanJourneyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: str = Field(pattern=r"^tessera-human-research-record-v1$")
    journey_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
    sequence: int = Field(ge=1)
    references: dict = Field(default_factory=dict)
    disclosures: dict[str, str] = Field(default_factory=dict)
    review: dict | None = None
    critic_input: dict | None = None
    planner_input: dict | None = None


class BackendJourneyRecords:
    def __init__(self, endpoint, binding, journey_id):
        self.endpoint, self.binding, self.id = endpoint, binding, journey_id

    def current(self):
        rows = []
        for offset in range(0, 400, 100):
            reply = self.endpoint.call(
                "GET",
                f"/projects/{self.binding['project_id']}/agent-contexts?offset={offset}&limit=100",
            )
            page = reply.get("contexts")
            if not isinstance(page, list) or len(page) > 100:
                raise ConsumerBlocked("human_control_context_catalog_unknown")
            rows.extend(page)
            if len(page) < 100:
                break
        else:
            raise ConsumerBlocked("human_control_context_catalog_bound")
        policies, records = {}, []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("body"), str):
                continue
            try:
                body = json.loads(row["body"])
            except ValueError:
                continue
            if not isinstance(body, dict):
                continue
            if body.get("schema_version") == "tessera-separated-public-harness-disclosure-v1":
                policies.setdefault(body.get("input_context_id"), []).append(row["id"])
            elif (
                body.get("schema_version") == "tessera-human-research-record-v1"
                and body.get("journey_id") == self.id
            ):
                records.append((row, HumanJourneyRecord.model_validate(body)))
        if records:
            maximum = max(record.sequence for _, record in records)
            records = [(row, record) for row, record in records if record.sequence == maximum]
            if len(records) != 1:
                raise ConsumerBlocked("human_control_record_sequence_ambiguous")
        verified = []
        for row, record in records:
            ids = policies.get(row["id"], [])
            if len(ids) != 1:
                raise ConsumerBlocked("human_control_record_policy_required")
            digest = hashlib.sha256(row["body"].encode()).hexdigest()
            pins = {
                **self.binding,
                "context_id": row["id"],
                "context_digest": digest,
                "input_context_id": row["id"],
                "input_body_digest": digest,
                "capture_policy_context_id": ids[0],
            }
            proof = resolve_contexts(self.endpoint, pins)
            if proof["input_context"]["body"] != row["body"]:
                raise ConsumerBlocked("human_control_record_readback_changed")
            verified.append((record.sequence, row["id"], record, proof["input_context"]))
        if not verified:
            return None
        sequence = max(r[0] for r in verified)
        latest = [r for r in verified if r[0] == sequence]
        if len(latest) != 1:
            raise ConsumerBlocked("human_control_record_sequence_ambiguous")
        return latest[0]

    def record(self):
        current = self.current()
        if current is None:
            raise ConsumerBlocked("human_research_record_waiting")
        return current[2]

    def read_current_references(self, journey_id):
        if journey_id != self.id:
            raise ConsumerBlocked("human_record_journey_changed")
        current = self.current()
        return {} if current is None else dict(current[2].references)

    def read_human_review(self, proposal_id):
        current = self.current()
        if current is None or current[2].review is None:
            return None
        review = current[2].review
        if set(review) != {"subject_id", "decision"} or review["subject_id"] != proposal_id:
            raise ConsumerBlocked("human_record_review_subject_changed")
        return {
            "id": current[1],
            "subject_id": proposal_id,
            "decision": review["decision"],
            "issuer_role": current[3]["issuer_role"],
            "project_id": self.binding["project_id"],
            "org_id": self.binding["org_id"],
        }

    def read_status_command(self, attempt_id):
        command = self.record().references["commands"]["status"]
        if command["payload"]["command"].get("attempt_id") != attempt_id:
            raise ConsumerBlocked("human_status_attempt_changed")
        return command

    def output(self, output_id, *, disclosure_id=None):
        disclosure = disclosure_id or self.record().disclosures.get(output_id)
        if not isinstance(disclosure, str) or not disclosure:
            raise ConsumerBlocked("human_output_disclosure_waiting")
        value = self.endpoint.call(
            "GET",
            "/generated-outputs/"
            + quote(output_id, safe="")
            + "?disclosure_id="
            + quote(disclosure, safe=""),
        )
        if value.get("id") != output_id:
            raise ConsumerBlocked("registered_output_readback_changed")
        return value, disclosure

    def resolve_output(self, endpoint, pins, output_id):
        value, disclosure = self.output(output_id)
        if value["pins"] != {k: pins[k] for k in value["pins"] if k != "operation_id"} | {
            "operation_id": None
        }:
            raise ConsumerBlocked("registered_output_capture_pins_changed")
        return {
            "family": "generated_public_research_output_v1",
            "output_id": value["id"],
            "artifact_id": value["artifact_id"],
            "disclosure_id": disclosure,
            "output_kind": value["output_kind"],
            "writer_actor_id": value["actor_id"],
            "writer_credential_id": value["credential_id"],
            "writer_org_id": value["org_id"],
            "binding": pins,
            "source_event_id": value["source_event_id"],
            "source_sequence": value["source_sequence"],
            "sha256": value["sha256"],
            "operation_id": None,
        }


class BackendExperimentReader:
    def __init__(self, consumer, records):
        self.consumer, self.records = consumer, records

    def status(self, response, attempt_id):
        result = response.get("result")
        if (
            response.get("action") != "attempt:status"
            or not isinstance(result, dict)
            or result.get("id") != attempt_id
        ):
            raise ConsumerBlocked("attempt_status_receipt_changed")
        state = result.get("state")
        if state in {"succeeded", "failed", "cancelled"}:
            if (
                result.get("operational_observed_state") != "exited"
                or result.get("operational_termination_confirmed") != 1
            ):
                return "unknown"
            return "canceled" if state == "cancelled" else state
        states = {
            "admitted": "queued",
            "preparing": "prepared",
            "running": "running",
            "unknown": "unknown",
            "interrupted": "unknown",
            "restart_pending": "unknown",
        }
        if state not in states:
            raise ConsumerBlocked("attempt_native_state_unknown")
        return states[state]

    def observation(self, attempt_id):
        command = self.records.read_status_command(attempt_id)
        sequence = self.consumer.store.next_poll(self.records.id)
        if sequence > 64:
            raise ConsumerBlocked("attempt_poll_bound_reached")
        response = self.consumer.mcp.call(
            "tessera_consume_grant",
            {
                **command,
                "idempotency_key": f"research-observation-status-{self.records.id}-{sequence}",
            },
        )
        status = self.status(response, attempt_id)
        if status not in {"succeeded", "failed", "canceled"}:
            raise ConsumerBlocked("attempt_observation_not_terminal")
        return {
            "attempt_id": attempt_id,
            "expected_attempt_revision": response["result"]["revision"],
            "summary": "Authenticated terminal attempt status: "
            + status
            + "; scientific validity not assessed.",
            "artifact_ids": [],
            "provenance": {
                "client": "tessera-research-consumer",
                "model": self.consumer.binding["model_id"],
                "session_id": self.consumer.binding["session_id"],
            },
        }


class RegisteredCriticTransport:
    def __init__(self, critic, critic_records, planner_records):
        self.critic, self.records, self.planner_records = critic, critic_records, planner_records

    def read_registered_pins(self, output_id, snapshot, coverage):
        from .research_peer import validate_generated

        output, _ = self.records.output(output_id)
        recipient, disclosure = self.planner_records.output(output_id)
        if recipient != output or output.get("output_kind") != "critic_review":
            raise ConsumerBlocked("critic_recipient_output_changed")
        cursor = 0
        event_cursor = 0
        capture = None
        for _ in range(4):
            page = self.critic.notebook(
                after_sequence=cursor, after_event_cursor=event_cursor, limit=100
            )
            for record in page["provenance"]:
                if (
                    (record.get("generated_output") or {}).get("output_id") == output_id
                    and (record.get("harness") or {}).get("run_id") == self.critic.run_id
                    and record.get("outcome") == "succeeded"
                ):
                    capture = record
            if not page["has_more"]:
                break
            next_cursor = page["next"]["after_sequence"]
            next_event = page["next"]["after_event_cursor"]
            if next_cursor <= cursor and next_event <= event_cursor:
                raise ConsumerBlocked("critic_capture_cursor_unknown")
            cursor = next_cursor
            event_cursor = next_event
        else:
            raise ConsumerBlocked("critic_capture_notebook_bound")
        if capture is None:
            raise ConsumerBlocked("critic_capture_ack_required")
        value = {
            "family": "research-generated-v1",
            "output_id": output_id,
            "disclosure_id": disclosure,
            "actor_id": output["actor_id"],
            "critic_credential_id": output["credential_id"],
            "org_id": output["org_id"],
            "project_id": output["project_id"],
            **output["pins"],
            "source_event_id": output["source_event_id"],
            "source_sequence": output["source_sequence"],
            "content_digest": "sha256:" + output["sha256"],
            "registration_grant_id": output["grant_id"],
            "registration_revision": output["expected_revision"],
            "output_kind": "critic_review",
            "capture_record_id": capture["record_id"],
            "capture_sequence": capture["sequence"],
        }
        return validate_generated(value)
