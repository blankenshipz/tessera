"""Real temp backend context/issuer readback and consumer fence; synthetic harness."""

import json

from test_research_consumer import arguments, consumer_setup, output_setup  # noqa: F401

from tessera_agent.research_consumer import ResearchConsumer
from tessera_control.agent_provenance_harness_models import SeparatedPublicHarnessDisclosure
from tessera_control.public_capture_contexts import resolve_separated_capture_contexts
from tessera_control.store import Store, _connect


def test_consumer_fence_uses_actual_separate_input_policy_resolution(request):
    constructor, prompts, http, owner, grant, observed, mcp = request.getfixturevalue(
        "consumer_setup"
    )
    state, _, _, _, sender, *_ = request.getfixturevalue("output_setup")
    capture = dict(constructor["capture_binding"])
    capture.update(
        input_context_id=capture["context_id"], input_body_digest=capture["context_digest"]
    )
    policy = {
        k: capture[k]
        for k, field in SeparatedPublicHarnessDisclosure.model_fields.items()
        if k != "schema_version" and (field.is_required() or k in capture)
    }
    policy["schema_version"] = "tessera-separated-public-harness-disclosure-v1"
    validated_policy = SeparatedPublicHarnessDisclosure.model_validate(policy)
    assert validated_policy.guard_phase_binding is None
    assert "guard_phase_binding" not in validated_policy.model_dump(mode="json")
    result = http.post(
        f"/projects/{capture['project_id']}/agent-contexts",
        headers={**owner, "Idempotency-Key": "human-separated-consumer-policy"},
        json={
            "credential_id": capture["credential_id"],
            "destination": sender["X-Agent-Destination"],
            "classification": "project",
            "title": "Separate human consumer policy",
            "disclosure_approved": True,
            "body": json.dumps(policy),
        },
    )
    assert result.status_code == 201
    capture["capture_policy_context_id"] = result.json()["id"]
    principal = Store(state).authenticate(sender["Authorization"].removeprefix("Bearer "))
    principal["agent_destination"] = sender["X-Agent-Destination"]

    def contexts(binding):
        conn = _connect(Store(state).database_path)
        try:
            conn.execute("BEGIN")
            return resolve_separated_capture_contexts(conn, principal, binding)
        finally:
            conn.close()

    constructor["capture_binding"] = capture
    constructor["context_resolver"] = contexts
    consumer = ResearchConsumer(**constructor)
    assert consumer.fence()["session_id"] == capture["session_id"]
    assert not prompts
