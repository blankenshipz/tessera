"""Production wiring of available transports; missing installed observations stay closed."""

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from tessera_control.public_capture import PublicCaptureAdapter
from tessera_control.public_capture_outbox import PublicCaptureOutbox
from tessera_control.public_capture_producer import NormalizedPublicMetadataProducer
from tessera_mcp.config import Config
from tessera_mcp.harness_binding import SKILL_ID, HarnessBinding

from .remote_reasoning_runtime import OpenCodeV2Reasoning, ReasoningSupervisor, validate_profile
from .remote_reasoning_store import private_file
from .research_approvals import MCPJourneyApprovals
from .research_backend_ports import (
    BackendExperimentReader,
    BackendJourneyRecords,
    RegisteredCriticTransport,
    resolve_contexts,
)
from .research_consumer import ResearchConsumer
from .research_consumer_store import ConsumerBlocked, ResearchConsumerStore
from .research_final_metadata import read_registered_final_metadata
from .research_inputs import ResearchInputs
from .research_journey import ResearchJourney
from .research_output_pipeline import ResearchOutputPipeline
from .research_peer import ResearchPeer
from .research_public_capture import PublicStatusCapture
from .research_transports import (
    MCPStdioTools,
    ResearchEndpoint,
    ResearchStateLease,
    observer_endpoint,
)
from .v2_public_output import V2FinalOutputReader


@dataclass(frozen=True)
class InstalledResearchPorts:
    """Installed trusted adapters, not config-selected modules or admission flags.

    Observer returns complete fresh verify_binding fields (including instance).
    Context resolver returns actual live human input/policy readback, not GET row
    fields relabelled as verified issuer state. Public producer returns only
    independently eligible metadata. These ports cannot be made from manifests.
    """

    observer: object
    contexts: object
    public_metadata: object
    records: object
    prompts: object
    critic_transport: object
    experiments: object


class _FinalReader(V2FinalOutputReader):
    def read(self, row, *, source_sequence=1):
        row = {**row, "expected_prompt_digest": self.store.input_digest(row["id"])}
        return self.read_after_prompt(row, row["message_id"], source_sequence)


@dataclass(frozen=True)
class PhaseDiagnosticPorts:
    """Explicit trusted embedding callbacks, never config-selected auth booleans.

    observer_factory returns a live-scope SupervisedPhaseObserver. Owner publisher
    and trust must be separately approved. No default production adapter exists.
    """

    observer_factory: object
    owner_publish: object
    owner_trust: object


class ResearchPhaseDiagnostics:
    """Local durable NON-admitting diagnostics, separate from consumer observation_reader."""

    def __init__(self, observer, owner_publish, owner_trust, directory, role):
        self.observer, self.owner_publish, self.owner_trust, self.role = (
            observer,
            owner_publish,
            owner_trust,
            role,
        )
        directory = Path(directory)
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ConsumerBlocked("diagnostic_state_path_invalid")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / "phase-diagnostics.sqlite3"
        if path.is_symlink():
            raise ConsumerBlocked("diagnostic_state_path_invalid")
        self._db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS evidence "
            "(role TEXT, nonce TEXT, state TEXT, payload TEXT, digest TEXT, "
            "PRIMARY KEY(role,nonce))"
        )
        self._db.commit()

    def read(self):
        report = self.observer.read()
        return {
            "protocol": "tessera-phase-diagnostic-v1",
            "admission_allowed": False,
            "production_verified": False,
            "origin_authentication": report.origin_authentication,
            "phase": report.receipt.model_dump(),
            "missing": list(report.missing),
        }

    def consume(self):
        nonce = self.observer.expected.nonce
        # Durable claim precedes uncertain cross-runtime consumption. Restart cannot replay it.
        try:
            self._db.execute(
                "INSERT INTO evidence VALUES(?,?,?,NULL,NULL)", (self.role, nonce, "pending")
            )
            self._db.commit()
        except sqlite3.IntegrityError:
            self._db.rollback()
            raise ConsumerBlocked("diagnostic_attempt_already_recorded") from None
        try:
            joined = self.observer.consume_and_join(
                owner_publish=self.owner_publish, owner_trust=self.owner_trust
            )
            value = joined.notebook_projection()
            payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
            digest = hashlib.sha256(payload.encode()).hexdigest()
            self._db.execute(
                "UPDATE evidence SET state=?,payload=?,digest=? WHERE role=? AND nonce=?",
                ("recorded", payload, digest, self.role, nonce),
            )
            self._db.commit()
            return value
        except Exception:
            self._db.execute(
                "UPDATE evidence SET state=? WHERE role=? AND nonce=?",
                ("uncertain", self.role, nonce),
            )
            self._db.commit()
            raise ConsumerBlocked("phase_diagnostic_consumption_uncertain") from None

    def read_record(self):
        row = self._db.execute(
            "SELECT state,payload,digest FROM evidence WHERE role=? AND nonce=?",
            (self.role, self.observer.expected.nonce),
        ).fetchone()
        if row is None:
            return None
        state, payload, digest = row
        if payload is not None and hashlib.sha256(payload.encode()).hexdigest() != digest:
            raise ConsumerBlocked("phase_diagnostic_record_integrity_changed")
        return {
            "state": state,
            "evidence": json.loads(payload) if payload else None,
            "admission_allowed": False,
            "production_verified": False,
        }

    def close(self):
        self._db.close()


class ProductionResearchFactory:
    def __init__(self, ports=None, *, phase_ports=None, admission_observer=None, owner=None):
        self.ports = ports
        self.phase_ports = phase_ports
        self.admission_observer = admission_observer
        self.owner = owner

    def close(self):
        if self.owner is not None:
            self.owner.close()

    def construct_phase_diagnostics(self, state_dir, *, role):
        """Opt-in diagnostics only; never constructs/starts a research consumer/session.

        Existing construct()/attested_observer() and full admission are untouched.
        Missing authority/publisher ports fail before creating any local state.
        """
        from tessera_mcp.guard_phase_join import OwnerPublisherTrust, SupervisedPhaseObserver

        ports = self.phase_ports
        if (
            type(ports) is not PhaseDiagnosticPorts
            or not callable(ports.observer_factory)
            or not callable(ports.owner_publish)
            or type(ports.owner_trust) is not OwnerPublisherTrust
        ):
            raise ConsumerBlocked("approved_phase_diagnostic_ports_missing")
        if role not in {"planner", "critic"}:
            raise ConsumerBlocked("phase_diagnostic_role_invalid")
        observer = ports.observer_factory()
        if type(observer) is not SupervisedPhaseObserver:
            raise ConsumerBlocked("approved_phase_diagnostic_observer_missing")
        return ResearchPhaseDiagnostics(
            observer,
            ports.owner_publish,
            ports.owner_trust,
            Path(state_dir) / "phase-diagnostics" / role,
            role,
        )

    def attested_observer(self, config, ref, binding, capture, resources):
        """Fixed production constructor, with only explicitly approved trust pins."""
        from tessera_mcp.harness_observer import GuardTrust, V2AttestedHarnessObserver

        from .research_service import validate_guard_relationships

        validate_guard_relationships(config)
        if getattr(ref, "admission_version", "v1") == "v2":
            from tessera_mcp.harness_observer import EvidenceBackedHarnessObserver

            if not callable(self.admission_observer):
                raise ConsumerBlocked("approved_v2_factory_ports_missing")
            observer = self.admission_observer(ref, binding, capture, resources)
            if (
                type(observer) is not EvidenceBackedHarnessObserver
                or observer.binding.digest != binding.digest
                or observer.verifier.pins.binding != binding.digest
                or observer.verifier.policy.admission_policy_ref != ref.admission_policy_ref
                or observer.verifier.policy.digest != ref.admission_policy_digest
                or observer.verifier.pins.instance != capture["harness_instance_id"]
                or observer.verifier.scope.principal != capture["actor_principal_id"]
                or observer.verifier.scope.context_ref
                != (capture.get("input_context_id") or capture["context_id"])
                or observer.verifier.scope.context_digest
                != (capture.get("input_body_digest") or capture["context_digest"])
                or observer.verifier.scope.session_ref
                != hashlib.sha256(capture["session_id"].encode()).hexdigest()
            ):
                raise ConsumerBlocked("approved_v2_factory_policy_or_observer_missing")
            return observer
        if ref.guard.instance_id != capture["harness_instance_id"]:
            raise ConsumerBlocked("guard_instance_capture_binding_changed")
        observed_harness = observer_endpoint(
            ref.harness_origin, ref.harness_password_file, ca_file=ref.harness_ca_file, basic=True
        )
        resources.append(observed_harness)
        observed_control = observer_endpoint(
            config.control_origin,
            ref.agent_token_file,
            ca_file=config.control_ca_file,
            destination=json.loads(binding.manifest)["destination"],
        )
        resources.append(observed_control)
        guard = observer_endpoint(
            ref.guard.origin, ref.guard.credential_file, ca_file=ref.guard.ca_file
        )
        resources.append(guard)
        trust = GuardTrust(**ref.guard.model_dump(exclude={"origin", "credential_file", "ca_file"}))
        return V2AttestedHarnessObserver(
            binding=binding,
            harness_endpoint=observed_harness,
            control_endpoint=observed_control,
            guard_endpoint=guard,
            guard_trust=trust,
            research_origin=ref.harness_origin,
            coding_origins=config.coding_origins,
            session_id=capture["session_id"],
        )

    def construct(self, config, state_dir):
        resources, consumers = [], []
        state_lease = None
        try:
            if any(
                getattr(getattr(config, role, None), "admission_version", "v1") == "v2"
                for role in ("planner", "critic")
            ):
                if self.ports is not None or not callable(self.admission_observer):
                    raise ConsumerBlocked("approved_v2_factory_ports_missing")
            if self.ports is None:
                from .research_service import validate_guard_relationships

                validate_guard_relationships(config)
            roles, records, endpoints = {}, {}, {}
            for name in ("planner", "critic"):
                ref = getattr(config, name)
                binding = HarnessBinding(
                    private_file(Path(ref.binding_config_file)),
                    private_file(Path(ref.binding_manifest_file)),
                    private_file(Path(ref.skill_file)),
                )
                manifest = json.loads(binding.manifest)
                configured = json.loads(binding.config)["agents"][manifest["agent"]]
                profile_bytes = private_file(Path(ref.reasoning_profile_file))
                profile = json.loads(profile_bytes)
                allowed_profile = {
                    "run_id",
                    "project_id",
                    "agent",
                    "model",
                    "account_mode",
                    "grant_id",
                    "revision",
                    "epoch",
                    "subject_credential_id",
                    "destination",
                    "control_url",
                    "harness_url",
                    "workspace",
                    "permissions",
                    "schema_sha256",
                    "version",
                    "prompt_sha256",
                    "max_steps",
                    "max_tokens",
                    "max_duration_seconds",
                }
                if not isinstance(profile, dict) or set(profile) != allowed_profile:
                    raise ConsumerBlocked("unsupported_research_reasoning_profile")
                validate_profile(profile, skill_id=SKILL_ID)
                for key, maximum in (
                    ("max_steps", 3),
                    ("max_tokens", 8192),
                    ("max_duration_seconds", 120),
                ):
                    if profile[key] > maximum:
                        raise ConsumerBlocked("research_usage_bound_changed")
                capture = json.loads(private_file(Path(ref.capture_binding_file)))
                expected = {
                    "run_id": ref.run_id,
                    "project_id": config.project_id,
                    "context_id": ref.context_id,
                    "input_context_id": ref.context_id,
                    "capture_policy_context_id": ref.policy_id,
                    "credential_id": ref.credential_id,
                    "actor_principal_id": ref.principal_id,
                    "model_id": ref.model[7:],
                }
                if (
                    any(capture.get(k) != v for k, v in expected.items())
                    or profile["run_id"] != ref.run_id
                    or profile["model"]["id"] != ref.model[7:]
                    or profile["subject_credential_id"] != ref.credential_id
                    or profile["control_url"] != config.control_origin
                    or profile["harness_url"] != ref.harness_origin
                    or manifest["role"] != name
                    or manifest["model"] != ref.model
                    or profile["destination"] != manifest["destination"]
                    or profile["agent"] != manifest["agent"]
                    or profile["max_steps"] != configured["steps"]
                    or profile["permissions"] != configured["permissions"]
                    or profile["account_mode"] != config.account_mode
                    or ref.harness_origin in config.coding_origins
                ):
                    raise ConsumerBlocked("installed_research_reference_changed")
                control = ResearchEndpoint(
                    config.control_origin,
                    ref.agent_token_file,
                    ca_file=config.control_ca_file,
                    destination=manifest["destination"],
                )
                harness_endpoint = ResearchEndpoint(
                    ref.harness_origin,
                    ref.harness_password_file,
                    ca_file=ref.harness_ca_file,
                    basic=True,
                )
                resources.extend([control, harness_endpoint])
                if self.ports is None:
                    observer = self.attested_observer(config, ref, binding, capture, resources)
                else:
                    observer = self.ports.observer(
                        binding,
                        harness_endpoint,
                        control,
                        ref.observer_admission_id,
                        capture["session_id"],
                        config.coding_origins,
                    )
                observer()  # Current complete admission BEFORE MCP children/effects.
                if state_lease is None:
                    state_lease = ResearchStateLease(state_dir)
                    resources.append(state_lease)
                mcp_config = Config(
                    config.control_origin,
                    Path(ref.agent_token_file),
                    config.project_id,
                    manifest["destination"],
                    Path(config.control_ca_file),
                )
                tools = MCPStdioTools(mcp_config)
                resources.append(tools)

                def context_resolver(pins, endpoint=control):
                    return (
                        resolve_contexts(endpoint, pins)
                        if self.ports is None
                        else self.ports.contexts(endpoint, pins)
                    )

                adapter = PublicCaptureAdapter(capture, context_resolver)
                resolution = adapter.resolve_contexts()
                if capture["allowed_public_events"] != ["generated_output"]:
                    raise ConsumerBlocked("final_output_only_capture_policy_required")
                if (
                    hashlib.sha256(resolution["input_context"]["body"].encode()).hexdigest()
                    != profile["prompt_sha256"]
                ):
                    raise ConsumerBlocked("installed_harness_input_digest_changed")
                store = ResearchConsumerStore(state_dir / name)
                resources.append(store)
                runtime_binding = {
                    k: profile[k]
                    for k in (
                        "grant_id",
                        "revision",
                        "epoch",
                        "subject_credential_id",
                        "destination",
                    )
                }
                runtime_binding.update(
                    model=profile["model"]["id"],
                    prompt_sha256=profile["prompt_sha256"],
                    profile_sha256=hashlib.sha256(profile_bytes).hexdigest(),
                )
                row = store.runtime.bind(ref.run_id, runtime_binding, time.time())
                if row["session_id"] != capture["session_id"]:
                    raise ConsumerBlocked("installed_session_binding_changed")
                harness = OpenCodeV2Reasoning(harness_endpoint, profile)
                final_reader = _FinalReader(harness)
                final_reader.store = store.runtime
                role_records = (
                    BackendJourneyRecords(control, capture, config.journey_id)
                    if self.ports is None
                    else self.ports.records
                )
                records[name], endpoints[name] = role_records, control
                from .provenance_client import ProvenanceClient

                collector = ProvenanceClient(
                    config.control_origin,
                    private_file(Path(ref.agent_token_file)).decode().strip(),
                    manifest["destination"],
                    verify=str(config.control_ca_file),
                )
                resources.append(collector)

                # Output resolver always re-reads real registry/current disclosure.
                def output_resolver(output_id, pins, endpoint=control, reader=role_records):
                    return reader.resolve_output(endpoint, pins, output_id)

                def capture_factory(
                    metadata,
                    pins=capture,
                    resolver=context_resolver,
                    output_reader=output_resolver,
                    directory=state_dir / name,
                ):
                    return PublicCaptureOutbox(
                        directory / "capture", PublicCaptureAdapter(pins, resolver, output_reader)
                    )

                status_outbox = capture_factory(None)
                resources.append(status_outbox)

                def metadata_reader(
                    runtime, journal=store, pins=capture, source=harness, reader=role_records
                ):
                    if self.ports is None:
                        return read_registered_final_metadata(
                            journal, pins, source, reader, runtime
                        )
                    return self.ports.public_metadata(source.endpoint, runtime)

                producer = NormalizedPublicMetadataProducer(
                    status_outbox.adapter,
                    metadata_reader,
                    batch_limit=8,
                )
                supervisor = ReasoningSupervisor(
                    store.runtime,
                    harness,
                    lambda gid, endpoint=control: endpoint.call(
                        "GET", "/agent-authority-grants/" + gid
                    ),
                    profile,
                    public_capture=PublicStatusCapture(producer, status_outbox, collector),
                )
                peer = ResearchPeer(
                    ref.peer_origin,
                    private_file(Path(ref.peer_token_file)).decode().strip(),
                    verify=ref.peer_ca_file or True,
                )
                resources.append(peer)
                consumer = ResearchConsumer(
                    store=store,
                    supervisor=supervisor,
                    capture_binding=capture,
                    harness_binding=binding,
                    observation_reader=observer,
                    final_reader=final_reader,
                    mcp=tools,
                    output_grant_id=ref.registration_grant_id,
                    output_grant_revision=ref.registration_revision,
                    output_kind="planner_proposal" if name == "planner" else "critic_review",
                    capture_factory=capture_factory,
                    collector=collector,
                    peer=peer,
                    context_resolver=context_resolver,
                    admission_version=getattr(ref, "admission_version", "v1"),
                )
                consumer.fence()
                consumers.append(consumer)
                roles[name] = consumer
            planner, critic = roles["planner"], roles["critic"]
            experiments = (
                BackendExperimentReader(planner, records["planner"])
                if self.ports is None
                else self.ports.experiments
            )
            inputs = (
                ResearchInputs(roles, endpoints, records)
                if self.ports is None
                else self.ports.prompts
            )
            if self.ports is None:
                for role_name, consumer in roles.items():
                    consumer.supervisor.harness.prompt_fence = lambda row, text, role=role_name: (
                        inputs.verify_prompt(role, row, text)
                    )
            critic_transport = (
                RegisteredCriticTransport(critic, records["critic"], records["planner"])
                if self.ports is None
                else self.ports.critic_transport
            )
            approvals = MCPJourneyApprovals(
                planner=planner,
                journey_store=planner.store,
                journey_id=config.journey_id,
                record_source=records["planner"],
                experiment_reader=experiments,
            )
            journey = ResearchJourney(
                config.journey_id,
                planner,
                critic,
                approvals=approvals,
                output_pipeline=ResearchOutputPipeline(
                    continuation_reader=planner.final_reader,
                    experiment_reader=experiments,
                ),
                initial_prompts=inputs,
                critic_transport=critic_transport,
            )
            journey.resources = resources
            return journey
        except Exception:
            for resource in reversed(resources):
                resource.close()
            raise
