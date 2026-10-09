"""Production constructor/HTTP guard verification, entirely synthetic trust proof."""

from types import SimpleNamespace

import pytest
from test_mcp_harness_observer_plugin import (  # noqa: F401
    compiled_plugin,
    protocol_fixture,
    trusted_fixture,
)

from tessera_agent import research_factory
from tessera_agent.research_consumer_store import ConsumerBlocked
from tessera_agent.research_service import GuardReferences
from tessera_mcp.harness_observer import ObservationBlocked, V2AttestedHarnessObserver


@pytest.mark.parametrize("change", [None, "issuer", "stale", "revoked"])
def test_default_constructor_uses_fixed_trust_and_rejects_before_prompt(
    trusted_fixture,  # noqa: F811
    protocol_fixture,  # noqa: F811
    monkeypatch,
    change,
):
    measured, state = trusted_fixture
    kwargs, _, _ = protocol_fixture
    guard = GuardReferences(
        origin=measured.guard_endpoint.origin,
        credential_file="/synthetic/guard-reference",
        **vars(measured.guard_trust),
    )
    ref = SimpleNamespace(
        guard=guard,
        principal_id=guard.principal_id,
        observer_admission_id=guard.enrollment_id,
        harness_origin=measured.origin,
        harness_password_file="/synthetic/harness-reference",
        harness_ca_file=None,
        agent_token_file="/synthetic/agent-reference",
    )
    config = SimpleNamespace(
        planner=ref,
        critic=ref,
        control_origin=kwargs["control_endpoint"].origin,
        control_ca_file=None,
        coding_origins=kwargs["coding_origins"],
    )
    endpoints = {
        ref.harness_origin: kwargs["harness_endpoint"],
        config.control_origin: kwargs["control_endpoint"],
        guard.origin: measured.guard_endpoint,
    }
    opened = []

    def mapped(origin, reference, **options):
        opened.append((origin, reference, options))
        return endpoints[origin]

    monkeypatch.setattr(research_factory, "observer_endpoint", mapped)
    resources = []
    actual = research_factory.ProductionResearchFactory().attested_observer(
        config,
        ref,
        kwargs["binding"],
        {"harness_instance_id": guard.instance_id, "session_id": kwargs["session_id"]},
        resources,
    )
    assert isinstance(actual, V2AttestedHarnessObserver)
    assert len(resources) == 3 and opened[-1][1] == guard.credential_file
    actual.clock = measured.clock  # Synthetic fixture time, not installed evidence.
    if change:

        def mutate(claim):
            if change == "issuer":
                claim["issuer"] = "foreign"
            elif change == "stale":
                claim["expires_at"] = "2029-01-01T00:00:00Z"
            else:
                claim["state"] = "revoked"

        state["mutation"] = mutate
        with pytest.raises(ObservationBlocked):
            actual()
    else:
        assert actual()["observed_harness_instance_id"] == guard.instance_id


@pytest.mark.parametrize("bad", ["coding", "harness", "control", "principal", "enrollment"])
def test_guard_relationship_rejects_before_credential_mapping(monkeypatch, bad):
    guard = GuardReferences(
        origin="https://guard.example.test",
        credential_file="/synthetic/ref",
        issuer="issuer",
        trust_reference="trust",
        producer_sha256="a" * 64,
        enrollment_id="enrollment",
        instance_id="instance",
        principal_id="actor",
        entitlement_reference="entitlement",
        entitlement_sha256="b" * 64,
        policy_sha256="c" * 64,
    )
    config = SimpleNamespace(
        control_origin="https://control.example.test",
        coding_origins=("https://coding.example.test",),
    )
    ref = SimpleNamespace(
        guard=guard,
        harness_origin="https://research.example.test",
        principal_id="actor",
        observer_admission_id="enrollment",
    )
    config.planner = config.critic = ref
    if bad in {"coding", "harness", "control"}:
        origin = {
            "coding": config.coding_origins[0],
            "harness": ref.harness_origin,
            "control": config.control_origin,
        }[bad]
        ref.guard = guard.model_copy(update={"origin": origin})
    else:
        ref.guard = guard.model_copy(update={bad + "_id": "foreign"})

    def forbidden(*args, **kwargs):
        raise AssertionError("Unsafe enrollment must fail before private credential mapping")

    monkeypatch.setattr(research_factory, "observer_endpoint", forbidden)
    with pytest.raises(ConsumerBlocked, match="independent_guard_enrollment_required"):
        research_factory.ProductionResearchFactory().attested_observer(config, ref, None, {}, [])


def test_guard_configuration_has_no_plaintext_or_relative_reference():
    with pytest.raises(ValueError):
        GuardReferences.https_origin("http://127.0.0.1:9000")
    with pytest.raises(ValueError):
        GuardReferences.private_reference("relative/token")
