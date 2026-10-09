"""Durable planner/critic public-output consumer with explicit human boundaries.

Ports must be authenticated installed adapters, not admission booleans or model
output claiming permission. No provider setup, owner proxy, automatic authority
renewal, arbitrary tool selection or inference replay is performed here.
"""

import base64
import hashlib
import json
import re
from datetime import UTC, datetime

from tessera_control.external_agent_models import AgentPlan
from tessera_control.generated_output_models import OutputPins, RegisterGeneratedOutput
from tessera_control.public_capture import CaptureBinding
from tessera_mcp.harness_binding import verify_binding
from tessera_mcp.mcp_workflow import Consume, Observation, ReviewRequest

from .research_consumer_store import ConsumerBlocked, canonical


class _BoundMCP:
    def __init__(self, client, permitted_tools):
        self.client, self.permitted = client, frozenset(permitted_tools)

    def call(self, name, arguments):
        if name not in self.permitted:
            raise ConsumerBlocked("consumer_tool_not_permitted")
        return self.client.call(name, arguments)


class ResearchConsumer:
    """One immutable admitted role/run; caller retains its journal and session.

    mcp.call is the actual permitted SDK client; peer.send/read/consume are the
    authenticated genuine peer transport. No synthetic defaults are installed.
    observation_reader obtains current public installed-harness admission facts;
    supplying fixtures to these ports yields synthetic evidence, not live proof.
    """

    def __init__(
        self,
        *,
        store,
        supervisor,
        capture_binding,
        harness_binding,
        observation_reader,
        final_reader,
        mcp,
        output_grant_id,
        output_grant_revision,
        output_kind,
        capture_factory,
        collector,
        peer=None,
        context_resolver=None,
        clock=lambda: datetime.now(UTC),
        admission_version="v1",
    ):
        if admission_version not in {"v1", "v2"}:
            raise ConsumerBlocked("unknown_admission_version")
        self.admission_version = admission_version
        self.native_tick = None
        if admission_version == "v2":
            from tessera_mcp.harness_observer import EvidenceBackedHarnessObserver

            if type(observation_reader) is not EvidenceBackedHarnessObserver:
                raise ConsumerBlocked("explicit_v2_observer_required")
            self.native_tick = observation_reader.verifier.bind_native_tick()
        self.store, self.supervisor = store, supervisor
        self.binding = CaptureBinding.model_validate(capture_binding).model_dump(mode="json")
        if output_kind not in {"planner_proposal", "critic_review"}:
            raise ConsumerBlocked("unknown_consumer_role")
        if not isinstance(output_grant_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", output_grant_id
        ):
            raise ConsumerBlocked("unknown_output_admission")
        if type(output_grant_revision) is not int or output_grant_revision < 1:
            raise ConsumerBlocked("unknown_output_admission")
        self.harness_binding, self.observation_reader = harness_binding, observation_reader
        manifest = json.loads(harness_binding.manifest)
        if manifest["role"] != ("planner" if output_kind == "planner_proposal" else "critic"):
            raise ConsumerBlocked("consumer_role_changed")
        for key, maximum in (("max_steps", 3), ("max_duration_seconds", 120), ("max_tokens", 8192)):
            value = supervisor.profile.get(key)
            if type(value) is not int or not 0 < value <= maximum:
                raise ConsumerBlocked("consumer_observed_usage_guard_required")
        configured_agent = json.loads(harness_binding.config)["agents"][manifest["agent"]]
        if configured_agent.get("steps") != supervisor.profile["max_steps"]:
            raise ConsumerBlocked("consumer_step_configuration_changed")
        self.final_reader, self.clock = final_reader, clock
        self.mcp = _BoundMCP(mcp, manifest["permitted_tools"])
        self.capture_factory, self.collector, self.peer = capture_factory, collector, peer
        self.context_resolver = context_resolver
        self.grant_id, self.grant_revision, self.kind = (
            output_grant_id,
            output_grant_revision,
            output_kind,
        )
        self.run_id = self.binding["run_id"]
        self.pins = OutputPins.model_validate(
            {key: self.binding[key] for key in OutputPins.model_fields if key != "operation_id"}
        ).model_dump(mode="json")
        immutable = {
            "capture": self.binding,
            "harness_binding_digest": harness_binding.digest,
            "output_grant_id": output_grant_id,
            "output_grant_revision": output_grant_revision,
            "output_kind": output_kind,
        }
        if admission_version == "v2":
            immutable["admission_version"] = "v2"
            immutable["admission_policy_digest"] = observation_reader.verifier.policy.digest
        self.store.bind(self.run_id, immutable)

    def fence(self):
        # No effect or public-output read until installed MCP/skill/config/principal
        # observations have passed the existing full binding verifier.
        manifest = json.loads(self.harness_binding.manifest)
        if manifest["model"] != "openai/" + self.binding["model_id"]:
            raise ConsumerBlocked("consumer_model_changed")
        if self.admission_version == "v2":
            verifier = self.observation_reader.verifier
            if (
                verifier.pins.instance != self.binding["harness_instance_id"]
                or verifier.scope.principal != self.binding["actor_principal_id"]
                or verifier.scope.context_ref
                != (self.binding.get("input_context_id") or self.binding["context_id"])
                or verifier.scope.context_digest
                != (self.binding.get("input_body_digest") or self.binding["context_digest"])
                or verifier.scope.session_ref
                != hashlib.sha256(self.binding["session_id"].encode()).hexdigest()
            ):
                raise ConsumerBlocked("consumer_v2_instance_principal_session_changed")
            result = self.observation_reader()
        else:
            observations = dict(self.observation_reader())
            if (
                observations.pop("observed_harness_instance_id", None)
                != self.binding["harness_instance_id"]
            ):
                raise ConsumerBlocked("consumer_harness_instance_changed")
            if (
                observations.get("principal_status", {}).get("id")
                != self.binding["actor_principal_id"]
            ):
                raise ConsumerBlocked("consumer_principal_changed")
            result = verify_binding(
                self.harness_binding,
                **observations,
                now=self.clock(),
                expected_binding_sha256=self.harness_binding.digest,
            )
        if any(
            result[name] != self.binding[field]
            for name, field in (
                ("config_sha256", "config_digest"),
                ("skill_sha256", "skill_digest"),
            )
        ):
            raise ConsumerBlocked("consumer_admission_digest_changed")
        if self.binding.get("input_context_id") is not None:
            from tessera_control.public_capture_contexts import validate_separated_resolution

            if self.context_resolver is None:
                raise ConsumerBlocked("consumer_separated_context_reader_required")
            resolution = validate_separated_resolution(
                self.binding, self.context_resolver(dict(self.binding))
            )
            context = {
                **resolution["input_context"],
                "id": resolution["input_context"]["context_id"],
                "destination": manifest["destination"],
            }
        else:
            context = self.mcp.call(
                "tessera_read_context", {"context_id": self.binding["context_id"]}
            )
        if (
            not isinstance(context, dict)
            or context.get("id") != self.binding["context_id"]
            or context.get("project_id") != self.binding["project_id"]
            or context.get("credential_id") != self.binding["credential_id"]
            or context.get("destination") != manifest["destination"]
            or context.get("disclosure_approved") is not True
            or not isinstance(context.get("body"), str)
            or hashlib.sha256(context["body"].encode()).hexdigest()
            != self.binding["context_digest"]
        ):
            raise ConsumerBlocked("consumer_curated_context_changed")
        row = self.store.runtime.get(self.run_id)
        if row["session_id"] != self.binding["session_id"]:
            raise ConsumerBlocked("consumer_session_changed")
        self.supervisor.fence(row)
        return row

    def effect(self, boundary, payload, call, *, reconcile=False):
        self.fence()
        digest = hashlib.sha256(canonical(payload).encode()).hexdigest()
        intent, fresh = self.store.begin(self.run_id, boundary, digest)
        if intent["state"] == "acknowledged":
            return intent["result_id"]
        if not fresh and not reconcile:
            raise ConsumerBlocked("consumer_effect_unknown")
        # Explicit reconciliation is only for typed idempotent network mutations,
        # never session creation/prompting or model/tool inference.
        try:
            result = call(intent["identity"])
            identifier = result["id"]
            if not isinstance(identifier, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", identifier
            ):
                raise ValueError()
        except Exception:
            raise ConsumerBlocked("consumer_effect_unknown") from None
        self.store.acknowledged(self.run_id, boundary, identifier)
        return identifier

    def tick(self, prompt=None, *, reconcile_registration=False):
        row = self.fence()
        current = self.store.get(self.run_id)
        if current["stage"] == "reasoning":
            if row["state"] != "completed":
                if self.admission_version == "v2":
                    # Owner-approved installed adapter owns the actual native last
                    # pre-effect boundary. No Python phase join after dispatch.
                    row = self.native_tick(self.supervisor, self.run_id, prompt)
                else:
                    row = self.supervisor.tick(self.run_id, prompt)
            if row["state"] != "completed":
                return self.receipt()
            self.fence()
            final = self.final_reader.read(row)
            payload = RegisterGeneratedOutput.model_validate(
                {
                    "grant_id": self.grant_id,
                    "expected_revision": self.grant_revision,
                    "pins": self.pins,
                    "source_event_id": final.source_event_id,
                    "source_sequence": final.source_sequence,
                    "output_kind": self.kind,
                    "visibility": "public",
                    "sha256": final.digest,
                    "content_base64": base64.b64encode(final.content).decode("ascii"),
                }
            ).model_dump(mode="json")

            def register(key):
                result = self.mcp.call(
                    "tessera_register_generated_output",
                    {
                        "payload": payload,
                        "idempotency_key": key,
                    },
                )
                if (
                    any(
                        result.get(field) != value
                        for field, value in payload.items()
                        if field != "content_base64"
                    )
                    or result.get("actor_id") != self.binding["actor_principal_id"]
                    or result.get("credential_id") != self.binding["credential_id"]
                    or result.get("org_id") != self.binding["org_id"]
                    or result.get("artifact_id") != result.get("id")
                    or result.get("content_digest") != "sha256:" + final.digest
                    or result.get("execution_established") is not False
                    or result.get("custody_state") != "human_admitted_public"
                ):
                    raise ConsumerBlocked("registration_receipt_changed")
                return result

            output_id = self.effect(
                "register_public_output",
                payload,
                register,
                reconcile=reconcile_registration,
            )
            self.store.registered(
                self.run_id, output_id, final.source_event_id, final.source_sequence, final.digest
            )
        return self.receipt()

    def capture(self, author_disclosure_id, *, registered_ref=None):
        self.fence()
        row = (
            self.store.get(self.run_id)
            if registered_ref is None
            else self._phase_ref(registered_ref)
        )
        if not row["output_id"]:
            raise ConsumerBlocked("public_output_not_registered")
        metadata = self.mcp.call(
            "tessera_generated_output_metadata",
            {
                "output_id": row["output_id"],
                "disclosure_id": author_disclosure_id,
            },
        )
        self._output(metadata, row)
        # Factory must supply authenticated human-policy and actual registered-output
        # resolvers. Absence/withdrawal is a wait/block, never inferred disclosure.
        outbox = self.capture_factory(metadata)
        try:
            event = {
                key: self.binding[key]
                for key in (
                    "run_id",
                    "session_id",
                    "harness_instance_id",
                    "task_id",
                    "context_id",
                    "context_digest",
                    "skill_digest",
                    "config_digest",
                    "model_id",
                )
            }
            event.update(
                schema_version="tessera-normalized-public-observation-v1",
                public_event="generated_output",
                source_event_id=row["source_id"],
                source_sequence=row["source_sequence"],
                source_watermark=row["source_sequence"],
                capture_state="unknown",
                gaps=[],
                outcome="succeeded",
                registered_output_id=row["output_id"],
            )
            for key in ("input_context_id", "input_body_digest", "capture_policy_context_id"):
                if key in self.binding:
                    event[key] = self.binding[key]
            # Only final output is projected. V2 has no approved full interaction
            # watermark here; do not label this a complete bounded public transcript.
            outbox.capture(event)
            self.fence()
            outbox.deliver_next(self.collector)
            coverage = outbox.watermarks()
            self.store.stage(
                self.run_id,
                "awaiting_human_review"
                if self.kind == "planner_proposal"
                else "awaiting_peer_delivery",
            )
            return coverage
        finally:
            outbox.close()

    def _output(self, metadata, row):
        expected = {
            "id": row["output_id"],
            "project_id": self.binding["project_id"],
            "credential_id": self.binding["credential_id"],
            "org_id": self.binding["org_id"],
            "actor_id": self.binding["actor_principal_id"],
            "pins": self.pins,
            "source_event_id": row["source_id"],
            "source_sequence": row["source_sequence"],
            "sha256": row["output_digest"],
            "output_kind": self.kind,
        }
        if any(metadata.get(k) != v for k, v in expected.items()):
            raise ConsumerBlocked("registered_public_output_changed")

    def propose(self, author_disclosure_id, *, reconcile=False, registered_ref=None):
        if self.kind != "planner_proposal":
            raise ConsumerBlocked("critic_cannot_propose_as_planner")
        self.fence()
        row = (
            self.store.get(self.run_id)
            if registered_ref is None
            else self._phase_ref(registered_ref)
        )
        output = self.mcp.call(
            "tessera_read_generated_output",
            {
                "output_id": row["output_id"],
                "disclosure_id": author_disclosure_id,
            },
        )
        self._output(output, row)
        from tessera_mcp.generated_outputs import decode_public_bytes

        content = decode_public_bytes(output["content_base64"], output["sha256"], output["size"])
        try:
            plan = AgentPlan.model_validate_json(content).model_dump(mode="json")
        except Exception:
            raise ConsumerBlocked("generated_proposal_schema_invalid") from None
        args = {
            **plan,
            "client_name": "tessera-research-consumer",
            "model": self.binding["model_id"],
            "session_id": self.binding["session_id"],
        }
        proposal_id = self.effect(
            "submit_proposal",
            args,
            lambda key: self.mcp.call(
                "tessera_propose_task_plan", {**args, "idempotency_key": key}
            ),
            reconcile=reconcile,
        )
        proposal = self.mcp.call("tessera_read_proposal", {"proposal_id": proposal_id})
        request = ReviewRequest.model_validate(
            {
                "subject_type": "proposal",
                "subject_id": proposal_id,
                "expected_revision": proposal["revision"],
                "reason": "Generated planner proposal requires human review.",
            }
        ).model_dump(mode="json")
        self.effect(
            "request_review",
            request,
            lambda key: self.mcp.call(
                "tessera_request_review", {"payload": request, "idempotency_key": key}
            ),
            reconcile=reconcile,
        )
        self.store.stage(self.run_id, "awaiting_human_review")
        return proposal

    def consume_grant(self, grant_id, payload, *, reconcile=False):
        """Caller supplies already-human-issued exact grant; never generates one.

        Closed MCP union/backend recheck expiry/revocation/budget/task/destination.
        This method does not interpret a generated proposal as human approval.
        """
        args = Consume.model_validate(payload).model_dump(mode="json")
        if self.kind != "planner_proposal":
            raise ConsumerBlocked("critic_execution_not_permitted")
        proposal_id = self.store.effect_result(self.run_id, "submit_proposal")
        if not proposal_id:
            raise ConsumerBlocked("human_review_required")
        proposal = self.mcp.call("tessera_read_proposal", {"proposal_id": proposal_id})
        promotion = proposal.get("promotion")
        if not isinstance(promotion, dict) or promotion.get("proposal_id") != proposal_id:
            raise ConsumerBlocked("human_review_required")
        grant = self.mcp.call("tessera_authority_status", {"grant_id": grant_id})
        accepted_bindings = [
            item
            for item in grant.get("bounds", {}).get("bindings", [])
            if item.get("task_id") == promotion.get("task_id")
        ]
        if not accepted_bindings:
            raise ConsumerBlocked("accepted_grant_task_changed")
        command = args["command"]
        if command["action"] == "attempt:admit" and not any(
            all(
                command.get(k) == item.get(k)
                for k in (
                    "variant_id",
                    "expected_variant_revision",
                    "expected_design_revision",
                    "execution_grant_id",
                )
            )
            for item in accepted_bindings
        ):
            # A grant can contain several bindings. Merely finding an accepted
            # task somewhere in that grant must not admit a different task.
            raise ConsumerBlocked("accepted_grant_command_changed")
        boundary = "consume_" + args["command"]["action"]
        bound = {"grant_id": grant_id, "payload": args}

        def consume(key):
            from .research_control_receipts import consumption_reference

            response = self.mcp.call("tessera_consume_grant", {**bound, "idempotency_key": key})
            reference = consumption_reference(response, grant_id, args["command"]["action"])
            self.store.reference(self.run_id, boundary + "_result", reference)
            return {"id": reference["consumption_id"]}

        consumption_id = self.effect(
            boundary,
            bound,
            consume,
            reconcile=reconcile,
        )
        reference = self.store.reference(self.run_id, boundary + "_result")
        if reference is None or reference["consumption_id"] != consumption_id:
            raise ConsumerBlocked("control_consumption_receipt_unknown")
        return reference

    def deliver_critic(self, generated, authority, *, reconcile=False):
        if self.kind != "critic_review" or self.peer is None:
            raise ConsumerBlocked("critic_peer_not_bound")
        from .research_peer import validate_generated

        generated = validate_generated(generated)
        if not {"capture_record_id", "capture_sequence"} <= set(generated):
            raise ConsumerBlocked("critic_author_capture_not_acknowledged")
        row = self.store.get(self.run_id)
        if (
            any(
                generated[key] != row[field]
                for key, field in (
                    ("output_id", "output_id"),
                    ("source_event_id", "source_id"),
                    ("source_sequence", "source_sequence"),
                )
            )
            or generated["content_digest"] != "sha256:" + row["output_digest"]
        ):
            raise ConsumerBlocked("critic_transport_output_changed")
        if any(generated[k] != v for k, v in self.pins.items()):
            raise ConsumerBlocked("critic_transport_pins_changed")
        if any(
            generated[k] != self.binding[field]
            for k, field in (
                ("actor_id", "actor_principal_id"),
                ("critic_credential_id", "credential_id"),
                ("org_id", "org_id"),
                ("project_id", "project_id"),
            )
        ):
            raise ConsumerBlocked("critic_transport_writer_changed")
        payload = {"reference": row["output_id"], "generated": generated, "authority": authority}
        task_id = self.effect(
            "peer_submit",
            payload,
            lambda key: self.peer.send("submit", payload, key),
            reconcile=reconcile,
        )
        result_payload = {**payload, "task_id": task_id}
        self.effect(
            "peer_result",
            result_payload,
            lambda key: self.peer.send("result", payload, key, task_id=task_id),
            reconcile=reconcile,
        )
        self.store.stage(self.run_id, "awaiting_planner_consumption")
        return task_id

    def consume_critic(self, task_id, generated, authority, *, reconcile=False):
        if self.kind != "planner_proposal" or self.peer is None:
            raise ConsumerBlocked("planner_peer_not_bound")
        from tessera_mcp.generated_outputs import decode_public_bytes

        from .research_peer import validate_generated

        self.fence()
        generated = validate_generated(generated)
        task = self.peer.read(task_id)
        if task.get("id") != task_id or task.get("metadata", {}).get("generated") != generated:
            raise ConsumerBlocked("critic_task_pins_changed")
        output = self.mcp.call(
            "tessera_read_generated_output",
            {
                "output_id": generated["output_id"],
                "disclosure_id": generated["disclosure_id"],
            },
        )
        expected = {
            "id": generated["output_id"],
            "actor_id": generated["actor_id"],
            "credential_id": generated["critic_credential_id"],
            "org_id": generated["org_id"],
            "project_id": self.binding["project_id"],
            "output_kind": "critic_review",
            "content_digest": generated["content_digest"],
            "source_event_id": generated["source_event_id"],
            "source_sequence": generated["source_sequence"],
            "pins": {key: generated[key] for key in OutputPins.model_fields},
        }
        if any(output.get(k) != v for k, v in expected.items()):
            raise ConsumerBlocked("critic_registered_output_changed")
        # Actual authenticated byte read precedes ACK; no claim of model attention.
        decode_public_bytes(output["content_base64"], output["sha256"], output["size"])
        payload = {
            "reference": generated["output_id"],
            "generated": generated,
            "authority": authority,
        }
        self.effect(
            "peer_consume",
            {**payload, "task_id": task_id},
            lambda key: self.peer.send("consume", payload, key, task_id=task_id),
            reconcile=reconcile,
        )
        self.store.stage(self.run_id, "awaiting_human_review")
        return {
            "output_id": generated["output_id"],
            "task_id": task_id,
            "consumption": "authenticated_read_and_ack_not_model_understanding",
        }

    def observe(self, payload, *, reconcile=False):
        args = Observation.model_validate(payload).model_dump(mode="json")
        return self.effect(
            "record_observation",
            args,
            lambda key: self.mcp.call(
                "tessera_record_observation", {"payload": args, "idempotency_key": key}
            ),
            reconcile=reconcile,
        )

    def notebook(self, **cursors):
        self.fence()
        return self.mcp.call("tessera_notebook", cursors)

    def receipt(self):
        row = self.store.get(self.run_id)
        receipt = {
            k: row[k] for k in ("run_id", "stage", "output_id", "source_id", "source_sequence")
        }
        runtime = self.store.runtime.get(self.run_id)
        receipt.update(reasoning_state=runtime["state"], reasoning_reason=runtime["reason"])
        return receipt

    def _phase_ref(self, value):
        if not isinstance(value, dict) or set(value) != {
            "output_id",
            "source_id",
            "source_sequence",
            "output_digest",
            "author_disclosure_id",
        }:
            raise ConsumerBlocked("phase_output_reference_invalid")
        if (
            type(value["source_sequence"]) is not int
            or value["source_sequence"] != 2
            or not re.fullmatch(r"[a-f0-9]{64}", value["output_digest"])
            or any(
                not isinstance(value[k], str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", value[k])
                for k in ("output_id", "source_id", "author_disclosure_id")
            )
        ):
            raise ConsumerBlocked("phase_output_reference_invalid")
        return value
