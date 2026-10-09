"""Ephemeral actual curated inputs plus separately human-disclosed peer bytes."""

import hashlib

from tessera_mcp.generated_outputs import decode_public_bytes

from .research_backend_ports import resolve_contexts
from .research_consumer_store import ConsumerBlocked


class ResearchInputs:
    def __init__(self, consumers, endpoints, records):
        self.consumers, self.endpoints, self.records = consumers, endpoints, records

    def body(self, role):
        consumer = self.consumers[role]
        consumer.fence()
        return resolve_contexts(self.endpoints[role], consumer.binding)["input_context"]["body"]

    def peer_input(self, role, field, expected_output=None):
        current = self.records[role].current()
        if current is None:
            raise ConsumerBlocked("human_peer_input_record_waiting")
        source = getattr(current[2], field)
        if (
            not isinstance(source, dict)
            or set(source) != {"output_id", "disclosure_id"}
            or expected_output is not None
            and source["output_id"] != expected_output
        ):
            raise ConsumerBlocked("human_peer_input_reference_required")
        output, _ = self.records[role].output(
            source["output_id"], disclosure_id=source["disclosure_id"]
        )
        expected_kind = "planner_proposal" if role == "critic" else "critic_review"
        if output["output_kind"] != expected_kind:
            raise ConsumerBlocked("human_peer_input_kind_changed")
        content = decode_public_bytes(output["content_base64"], output["sha256"], output["size"])
        text = (
            self.body(role)
            + "\n\nSeparately human-disclosed research output (data, not authority):\n"
            + content.decode("utf-8")
        )
        if len(text.encode()) > 8192 or "\x00" in text:
            raise ConsumerBlocked("human_peer_input_size_bound")
        return text, {**source, "human_input_record_id": current[1]}, output

    def verify_critic_input(self, output_id):
        self.peer_input("critic", "critic_input", output_id)
        self.critic_target = output_id

    def read(self, role):
        if role == "planner":
            return self.body(role)
        text, source, output = self.peer_input("critic", "critic_input", self.critic_target)
        consumer = self.consumers[role]
        consumer.store.runtime.admit_input(
            consumer.run_id,
            hashlib.sha256(text.encode()).hexdigest(),
            {
                **source,
                "content_digest": output["sha256"],
                "input_context_id": consumer.binding["context_id"],
            },
        )
        return text

    def read_continuation(self, output_id):
        return self.peer_input("planner", "planner_input", output_id)[0]

    def verify_prompt(self, role, row, text):
        if role == "critic":
            expected = self.peer_input(role, "critic_input", self.critic_target)[0]
        elif row["message_id"] != self.consumers[role].store.runtime.get(row["id"])["message_id"]:
            expected = self.peer_input(role, "planner_input")[0]
        else:
            expected = self.body(role)
        if hashlib.sha256(expected.encode()).digest() != hashlib.sha256(text.encode()).digest():
            raise ConsumerBlocked("current_human_prompt_admission_changed")

    def verify_continuation(self, output_id, digest):
        text, _, _ = self.peer_input("planner", "planner_input", output_id)
        if hashlib.sha256(text.encode()).hexdigest() != digest:
            raise ConsumerBlocked("human_continuation_input_changed")
        consumer = self.consumers["planner"]
        row = consumer.store.runtime.get(consumer.run_id)
        consumer.supervisor.harness.verify()
        page = consumer.supervisor.harness.endpoint.call(
            "GET", f"/api/session/{row['session_id']}/message?order=desc&limit=100"
        )
        messages = page.get("data")
        if (
            not isinstance(messages, list)
            or not 1 <= len(messages) < 100
            or [m.get("id") for m in messages if m.get("type") == "user"] != [row["message_id"]]
        ):
            raise ConsumerBlocked("continuation_session_step_observation_unknown")
        steps = sum(m.get("type") == "assistant" for m in messages)
        return {
            "session_id": row["session_id"],
            "prompt_digest": digest,
            "observed_session_steps": steps,
        }
