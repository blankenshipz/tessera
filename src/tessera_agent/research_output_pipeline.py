"""Real registry/MCP mutations for a separately admitted planner continuation."""

import base64

from tessera_control.generated_output_models import RegisterGeneratedOutput

from .research_consumer_store import ConsumerBlocked


class ResearchOutputPipeline:
    def __init__(self, *, continuation_reader, experiment_reader):
        self.continuation_reader, self.experiment_reader = continuation_reader, experiment_reader

    def register_and_capture(
        self,
        consumer,
        *,
        phase,
        source_sequence,
        prompt_message_id,
        author_disclosure_id,
        reconcile=False,
    ):
        consumer.fence()
        row = consumer.store.runtime.get(consumer.run_id)
        prompt = consumer.store.reference(consumer.run_id, "continuation_prompt")
        if prompt is not None:
            if prompt["message_id"] != prompt_message_id:
                raise ConsumerBlocked("continuation_prompt_binding_changed")
            row = {**row, "expected_prompt_digest": prompt["prompt_digest"]}
        # Reader is the actual authenticated session/public projection adapter.
        # It must associate a fresh assistant output with this exact prompt; the
        # latest historical finish=stop message is not adequate proof.
        final = self.continuation_reader.read_after_prompt(row, prompt_message_id, source_sequence)
        original = consumer.store.get(consumer.run_id)
        if (
            final.source_event_id == original["source_id"]
            or final.source_sequence != source_sequence
            or source_sequence != 2
        ):
            raise ConsumerBlocked("continuation_output_not_fresh")
        payload = RegisterGeneratedOutput.model_validate(
            {
                "grant_id": consumer.grant_id,
                "expected_revision": consumer.grant_revision,
                "pins": consumer.pins,
                "source_event_id": final.source_event_id,
                "source_sequence": source_sequence,
                "output_kind": "planner_proposal",
                "visibility": "public",
                "sha256": final.digest,
                "content_base64": base64.b64encode(final.content).decode("ascii"),
            }
        ).model_dump(mode="json")

        def register(key):
            result = consumer.mcp.call(
                "tessera_register_generated_output",
                {
                    "payload": payload,
                    "idempotency_key": key,
                },
            )
            if (
                any(result.get(k) != v for k, v in payload.items() if k != "content_base64")
                or result.get("actor_id") != consumer.binding["actor_principal_id"]
                or result.get("credential_id") != consumer.binding["credential_id"]
                or result.get("org_id") != consumer.binding["org_id"]
            ):
                raise ConsumerBlocked("continuation_registration_receipt_changed")
            return result

        output_id = consumer.effect("register_" + phase, payload, register, reconcile=reconcile)
        if not author_disclosure_id:
            raise ConsumerBlocked("continuation_author_disclosure_required")
        reference = {
            "output_id": output_id,
            "source_id": final.source_event_id,
            "source_sequence": source_sequence,
            "output_digest": final.digest,
            "author_disclosure_id": author_disclosure_id,
        }
        consumer.capture(author_disclosure_id, registered_ref=reference)
        return consumer._phase_ref(reference)

    def propose_registered(self, consumer, reference):
        return consumer.propose(reference["author_disclosure_id"], registered_ref=reference)

    def attempt_status(self, response, attempt_id):
        return self.experiment_reader.status(response, attempt_id)

    def observe_registered_experiment(self, consumer, attempt_id):
        # Only a current real attempt/output readback may construct this typed
        # caller-attributed observation; no model-generated execution receipt.
        consumer.observe(self.experiment_reader.observation(attempt_id))

    def export_notebook(self, consumer):
        consumer.fence()
        return consumer.mcp.call("tessera_export_notebook", {})
