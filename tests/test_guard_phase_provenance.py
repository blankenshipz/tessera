"""Actual temp collector/HTTP/SQLite; synthetic SDK/owner outcomes, never private guard."""

import hashlib
import sqlite3
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_agent_authority import enroll, post, prepared  # noqa: F401
from test_agent_provenance_http_integration import collector  # noqa: F401

from tessera_agent.provenance_client import ProvenanceClient
from tessera_control.public_capture import PublicCaptureAdapter, canonical
from tessera_control.public_capture_contexts import resolve_separated_capture_contexts
from tessera_control.public_capture_outbox import PublicCaptureOutbox
from tessera_control.store import Store, _connect
from tessera_mcp.guard_phase_evidence import phase_fingerprint


@pytest.fixture
def guard_capture(request):
    state, client, owner, author, credential, project, grant, legacy = request.getfixturevalue(
        "collector"
    )
    task_id = legacy["record"]["references"]["task_id"]
    text = "Synthetic curated public guard-observation input; no inference."
    digest = hashlib.sha256(text.encode()).hexdigest()
    input_context = post(
        client,
        owner,
        f"/projects/{project}/agent-contexts",
        {
            "credential_id": credential["credential_id"],
            "destination": author["X-Agent-Destination"],
            "title": "guard input",
            "body": text,
            "classification": "project",
            "disclosure_approved": True,
        },
    )
    channel = {
        "issuer_origin": "https://core.example.test",
        "enrollment_ref": "4" * 64,
        "adapter_ref": "5" * 64,
        "nonce": "6" * 64,
        "session_ref": "7" * 64,
        "proof_ref": "8" * 64,
        "order_revision": 2,
        "sequence": 7,
        "model_id": "model-1",
        "provider_sha256": "1" * 64,
        "model_sha256": "2" * 64,
        "tool_policy_sha256": "3" * 64,
        "owner_issuer_origin": "https://owner.example.test",
        "owner_publisher_ref": "9" * 64,
    }
    policy = {
        "schema_version": "tessera-separated-public-harness-disclosure-v1",
        "run_id": "runtime_run_1",
        "session_id": "session_1",
        "harness_instance_id": "instance_1",
        "model_id": "model-1",
        "task_id": task_id,
        "context_digest": digest,
        "input_context_id": input_context["id"],
        "input_body_digest": digest,
        "skill_digest": "b" * 64,
        "config_digest": "c" * 64,
        "allowed_public_events": ["guard_phase"],
        "guard_phase_binding": channel,
    }
    policy_context = post(
        client,
        owner,
        f"/projects/{project}/agent-contexts",
        {
            "credential_id": credential["credential_id"],
            "destination": author["X-Agent-Destination"],
            "title": "human guard capture policy",
            "body": canonical(policy),
            "classification": "project",
            "disclosure_approved": True,
        },
    )
    principal = Store(state).authenticate(author["Authorization"].split(" ", 1)[1])
    principal["agent_destination"] = author["X-Agent-Destination"]
    binding = {
        **{k: policy[k] for k in policy if k != "schema_version"},
        "schema_version": "tessera-public-capture-binding-v1",
        "context_id": input_context["id"],
        "capture_policy_context_id": policy_context["id"],
        "org_id": principal["org_id"],
        "project_id": project,
        "credential_id": principal["credential_id"],
        "actor_principal_id": principal["id"],
        "destination_digest": "sha256:"
        + hashlib.sha256(author["X-Agent-Destination"].encode()).hexdigest(),
    }
    now = datetime.now(UTC)
    timestamp = now.isoformat()
    provider_facts = {
        "evidence_class": "provider_unknown",
        "entitlement": "unknown",
        "billing_guarantee": "unknown",
    }
    projection = {
        "provider": "openai",
        **{
            k: channel[k]
            for k in ("model_id", "provider_sha256", "model_sha256", "tool_policy_sha256")
        },
    }
    projection["fingerprint"] = phase_fingerprint(
        **{
            k: channel[k]
            for k in (
                "order_revision",
                "sequence",
                "model_id",
                "provider_sha256",
                "model_sha256",
                "tool_policy_sha256",
            )
        }
    )
    phase = {
        "protocol": "opencode-model-hook-phase-v1",
        "phase": "post-model-request-hooks",
        "complete": False,
        "phase_finality": "unproven",
        "ordering_enforced": False,
        **{
            k: channel[k]
            for k in (
                "nonce",
                "session_ref",
                "proof_ref",
                "issuer_origin",
                "order_revision",
                "sequence",
            )
        },
        "observed_at": timestamp,
        "valid_until": (now + timedelta(seconds=25)).isoformat(),
        "projection": projection,
        "local_enforcement": {
            "evidence_class": "local_measured_enforcement",
            "model_request_hook_executor": "ordered_snapshot",
            "scope": "model-request-hook-stage-only",
        },
        "financial_settings": {
            "evidence_class": "operator_attested_financial_settings",
            "observation": "not_supplied",
            "freshness": "unknown",
        },
        "provider_facts": provider_facts,
    }
    owner_checks = {
        "protocol": "tessera-owner-guard-checks-v1",
        "publisher_ref": channel["owner_publisher_ref"],
        "issuer_origin": channel["owner_issuer_origin"],
        **{
            k: phase[k]
            for k in ("nonce", "session_ref", "proof_ref", "phase", "order_revision", "sequence")
        },
        "executed_phase": "model.request",
        "projection_fingerprint": projection["fingerprint"],
        "model_id": channel["model_id"],
        "executed_at": timestamp,
        "valid_until": (now + timedelta(seconds=25)).isoformat(),
        "local_checks": {
            "evidence_class": "local_measured_enforcement",
            **{
                k: "passed"
                for k in (
                    "openai_only",
                    "chatgpt_route",
                    "chatgpt_oauth_method",
                    "account_match",
                    "api_key_overrides_absent",
                )
            },
        },
        "quota": {
            "evidence_class": "local_measured_enforcement",
            "outcome": "passed",
            "checked_at": timestamp,
            "snapshot_checked_at": (now - timedelta(seconds=1)).isoformat(),
            "valid_until": (now + timedelta(seconds=20)).isoformat(),
        },
        "financial_settings": {
            "evidence_class": "operator_attested_financial_settings",
            "auto_top_up_off": True,
            "automatic_reload_off": True,
            "purchased_credit_balance_zero": True,
            "verified_at": "2020-01-01",
            "freshness": "unknown",
        },
        "provider_facts": provider_facts,
    }
    observation = {
        "schema_version": "tessera-normalized-public-observation-v1",
        **{
            k: binding[k]
            for k in (
                "run_id",
                "session_id",
                "harness_instance_id",
                "model_id",
                "task_id",
                "context_id",
                "context_digest",
                "skill_digest",
                "config_digest",
                "input_context_id",
                "input_body_digest",
                "capture_policy_context_id",
            )
        },
        "public_event": "guard_phase",
        "source_event_id": "guard_source_1",
        "source_sequence": 1,
        "source_watermark": 2,
        "capture_state": "unknown",
        "gaps": [{"first": 2, "last": 2, "reason": "source_unavailable"}],
        "outcome": "unknown",
        "guard_phase": {
            "protocol": "tessera-provenance-guard-phase-v1",
            "phase": phase,
            "owner_checks": owner_checks,
        },
    }

    def resolver(b):
        with _connect(state / "tessera.sqlite3") as conn:
            conn.execute("BEGIN")
            return resolve_separated_capture_contexts(conn, principal, b)

    adapter = PublicCaptureAdapter(binding, resolver)
    yield {
        "state": state,
        "client": client,
        "owner": owner,
        "author": author,
        "project": project,
        "binding": binding,
        "observation": observation,
        "adapter": adapter,
        "envelope": adapter.envelope(observation),
        "now": now,
    }


def append(setup, body=None, key="guard_append", headers=None):
    return setup["client"].post(
        f"/projects/{setup['project']}/agent-provenance",
        json=body or setup["envelope"],
        headers={**(headers or setup["author"]), "Idempotency-Key": key},
    )


def exported(setup, headers=None):
    response = setup["client"].get(
        f"/projects/{setup['project']}/agent-notebook/export", headers=headers or setup["author"]
    )
    assert response.status_code == 200
    return response.json()


def test_actual_collector_outbox_client_ack_and_nonadmitting_historical_projection(
    guard_capture, tmp_path, monkeypatch
):
    setup = guard_capture
    box = PublicCaptureOutbox(tmp_path / "private", setup["adapter"])
    rid = box.capture(setup["observation"])

    def transport(request):
        response = setup["client"].request(
            request.method, request.url.path, headers=dict(request.headers), content=request.content
        )
        return httpx.Response(response.status_code, json=response.json())

    binding = setup["binding"]
    with ProvenanceClient(
        "http://127.0.0.1:8000",
        setup["author"]["Authorization"].split(" ", 1)[1],
        setup["author"]["X-Agent-Destination"],
        transport=httpx.MockTransport(transport),
        expected_actor={k: binding[k] for k in ("org_id", "actor_principal_id", "credential_id")},
    ) as client:
        assert box.deliver_next(client)["delivery"] == "accepted"
    first = append(setup)
    assert first.status_code == 201
    assert append(setup).json() == first.json()
    row = next(r for r in exported(setup)["provenance"] if r["record_id"] == rid)
    report = row["guard_observation"]
    assert report["admission_allowed"] is False and report["production_verified"] is False
    assert report["consumption_verified"] is False and report["complete"] is False
    assert report["financial_freshness"] == report["provider_entitlement"] == "unknown"
    assert row["guard_phase"]["owner_checks"]["financial_settings"]["verified_at"] == "2020-01-01"
    assert row["harness_capture"]["source_completeness"] == "unknown_source_capture"
    from tessera_control import agent_provenance_guard as module

    class Later:
        @staticmethod
        def now(_):
            return setup["now"] + timedelta(seconds=31)

    monkeypatch.setattr(module, "datetime", Later)
    row = next(r for r in exported(setup)["provenance"] if r["record_id"] == rid)
    assert row["guard_observation"]["freshness_at_export"] == "expired"
    assert row["guard_observation"]["admission_allowed"] is False
    assert (
        append(setup).status_code == 403
    )  # Old ACK cannot be republished as current phase evidence.
    box.close()


@pytest.mark.parametrize(
    "change",
    [
        "stale",
        "future",
        "long_window",
        "nonce",
        "fingerprint",
        "owner_origin",
        "owner_nonce",
        "old_quota",
        "future_quota",
        "missing_snapshot",
        "session",
        "task",
        "model",
        "model_success",
    ],
)
def test_phase_freshness_pins_owner_correlation_and_denial(guard_capture, change):
    setup = guard_capture
    envelope = deepcopy(setup["envelope"])
    phase = envelope["record"]["guard_phase"]["phase"]
    owner = envelope["record"]["guard_phase"]["owner_checks"]
    now = setup["now"]
    if change == "stale":
        phase["observed_at"] = (now - timedelta(seconds=31)).isoformat()
        phase["valid_until"] = (now - timedelta(seconds=1)).isoformat()
    elif change == "future":
        phase["observed_at"] = (now + timedelta(seconds=20)).isoformat()
    elif change == "long_window":
        phase["valid_until"] = (now + timedelta(seconds=40)).isoformat()
    elif change == "nonce":
        phase["nonce"] = "0" * 64
    elif change == "fingerprint":
        phase["projection"]["fingerprint"] = "0" * 64
    elif change == "owner_origin":
        owner["issuer_origin"] = "https://foreign.example.test"
    elif change == "owner_nonce":
        owner["nonce"] = "0" * 64
    elif change == "old_quota":
        owner["quota"]["snapshot_checked_at"] = (now - timedelta(seconds=31)).isoformat()
    elif change == "future_quota":
        owner["quota"]["snapshot_checked_at"] = (now + timedelta(seconds=20)).isoformat()
    elif change == "missing_snapshot":
        owner["quota"]["snapshot_checked_at"] = None
    elif change == "session":
        envelope["record"]["harness"]["session_id"] = "different_session"
    elif change == "task":
        envelope["record"]["harness"]["task_id"] = "different_task"
    elif change == "model":
        envelope["record"]["model_id"] = "different_model"
    elif change == "model_success":
        envelope["record"]["outcome"] = "succeeded"
    assert append(setup, envelope).status_code in {403, 409}
    with _connect(setup["state"] / "tessera.sqlite3") as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM agent_provenance WHERE record_id=?",
                (envelope["record"]["record_id"],),
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "change", ["raw", "complete", "financial_class", "entitlement", "authorized", "numeric_bool"]
)
def test_closed_receipt_privacy_and_no_admission_boolean(guard_capture, change):
    setup = guard_capture
    envelope = deepcopy(setup["envelope"])
    guard = envelope["record"]["guard_phase"]
    if change == "raw":
        guard["owner_checks"]["headers"] = {"Authorization": "PRIVATE_SENTINEL"}
    elif change == "complete":
        guard["phase"]["complete"] = True
    elif change == "financial_class":
        guard["owner_checks"]["financial_settings"]["evidence_class"] = "local_measured_enforcement"
    elif change == "entitlement":
        guard["owner_checks"]["provider_facts"]["entitlement"] = "verified"
    elif change == "authorized":
        guard["authorized"] = True
    elif change == "numeric_bool":
        guard["phase"]["ordering_enforced"] = 0
    response = append(setup, envelope)
    assert response.status_code == 422
    assert "PRIVATE_SENTINEL" not in response.text


@pytest.mark.parametrize("mode", ["missing", "denied", "unavailable"])
def test_partial_unknown_and_negative_owner_facts_are_not_invented_success(guard_capture, mode):
    setup = guard_capture
    envelope = deepcopy(setup["envelope"])
    guard = envelope["record"]["guard_phase"]
    if mode == "missing":
        guard["owner_checks"] = None
    else:
        owner = guard["owner_checks"]
        owner["quota"]["outcome"] = mode
        owner["local_checks"]["account_match"] = "unknown" if mode == "unavailable" else "failed"
        if mode == "unavailable":
            owner["quota"]["snapshot_checked_at"] = None
            owner["financial_settings"]["verified_at"] = None
    response = append(setup, envelope)
    assert response.status_code == 201, response.text
    record = next(
        r
        for r in exported(setup)["provenance"]
        if r["record_id"] == envelope["record"]["record_id"]
    )
    assert record["guard_observation"]["admission_allowed"] is False
    if mode == "missing":
        assert record["guard_observation"]["owner_checks"] == "not_supplied"
        assert "owner_guard_checks" in record["guard_observation"]["missing"]
    else:
        assert record["guard_phase"]["owner_checks"]["quota"]["outcome"] == mode


def test_nonce_rekey_tamper_and_current_credential_revocation(guard_capture):
    setup = guard_capture
    assert append(setup).status_code == 201
    body = deepcopy(setup["envelope"])
    body["record"]["guard_phase"]["owner_checks"]["local_checks"]["account_match"] = "failed"
    assert append(setup, body).status_code == 409
    body = deepcopy(setup["envelope"])
    body["record"]["record_id"] = "rekeyed_guard"
    body["record"]["harness"].update(
        source_event_id="guard_source_2", source_sequence=3, source_watermark=3
    )
    assert append(setup, body, key="nonce_rekey").status_code == 409
    response = setup["client"].post(
        f"/credentials/{setup['binding']['credential_id']}/revoke",
        headers={**setup["owner"], "Idempotency-Key": "revoke_capture"},
    )
    assert response.status_code == 200
    assert append(setup).status_code == 401


def test_undisclosed_peer_and_foreign_project_cannot_observe_or_publish_guard_receipts(
    guard_capture,
):
    setup = guard_capture
    assert append(setup).status_code == 201
    peer, _ = enroll(
        setup["client"],
        setup["owner"],
        setup["project"],
        ["notebook:read", "context:read", "provenance:append"],
    )
    view = exported(setup, peer)
    record = next(
        r for r in view["provenance"] if r["record_id"] == setup["envelope"]["record"]["record_id"]
    )
    assert "guard_phase" not in record
    assert record["guard_observation"]["visibility"] == "withheld_current_disclosure"
    assert append(setup, key="peer_append", headers=peer).status_code in {403, 404}
    other = post(setup["client"], setup["owner"], "/projects", {"name": "foreign guard project"})
    foreign, _ = enroll(setup["client"], setup["owner"], other["id"], ["provenance:append"])
    response = setup["client"].post(
        f"/projects/{other['id']}/agent-provenance",
        json=setup["envelope"],
        headers={**foreign, "Idempotency-Key": "foreign_guard"},
    )
    assert response.status_code in {403, 404}


def test_record_event_and_idempotency_roll_back_on_event_failure(guard_capture):
    setup = guard_capture
    with sqlite3.connect(setup["state"] / "tessera.sqlite3") as conn:
        conn.execute("""CREATE TRIGGER reject_guard_event BEFORE INSERT ON events
            WHEN NEW.event_type='agent_provenance.recorded' BEGIN
            SELECT RAISE(ABORT,'isolated guard rollback'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="guard rollback"):
        append(setup)
    with sqlite3.connect(setup["state"] / "tessera.sqlite3") as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM agent_provenance WHERE record_id=?",
                (setup["envelope"]["record"]["record_id"],),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM idempotency WHERE path LIKE '%/agent-provenance'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("missing", ["category", "binding"])
def test_guard_requires_actual_human_policy_category_and_expectations(guard_capture, missing):
    setup = guard_capture
    body = deepcopy(setup["envelope"])
    policy_id = setup["binding"]["capture_policy_context_id"]
    response = setup["client"].get(f"/agent-contexts/{policy_id}", headers=setup["author"])
    assert response.status_code == 200
    import json

    policy = json.loads(response.json()["body"])
    if missing == "category":
        policy["allowed_public_events"] = ["tool"]
    else:
        del policy["guard_phase_binding"]
    context = post(
        setup["client"],
        setup["owner"],
        f"/projects/{setup['project']}/agent-contexts",
        {
            "credential_id": setup["binding"]["credential_id"],
            "destination": setup["author"]["X-Agent-Destination"],
            "title": "different human policy",
            "body": canonical(policy),
            "classification": "project",
            "disclosure_approved": True,
        },
    )
    body["record"]["harness"]["capture_policy_context_id"] = context["id"]
    assert append(setup, body).status_code == 403


def test_legacy_wire_omits_new_optional_guard_fields():
    from tessera_control.agent_provenance_harness_models import PublicHarnessDisclosure
    from tessera_control.agent_provenance_models import AgentProvenanceRecord

    record = AgentProvenanceRecord.model_validate(
        {
            "record_id": "legacy_record",
            "kind": "agent",
            "outcome": "unknown",
            "correlation_id": "legacy_run",
        }
    ).model_dump(mode="json")
    assert "guard_phase" not in record
    policy = PublicHarnessDisclosure.model_validate(
        {
            "schema_version": "tessera-public-harness-disclosure-v1",
            "run_id": "legacy_run",
            "task_id": "legacy_task",
            "context_digest": "a" * 64,
            "skill_digest": "b" * 64,
            "config_digest": "c" * 64,
            "allowed_public_events": ["message"],
        }
    ).model_dump(mode="json")
    assert "guard_phase_binding" not in policy


def test_existing_typed_core_report_port_preserves_missing_owner_and_proof_limits(guard_capture):
    import json

    from tessera_control.agent_provenance_guard import guard_observation_from_report
    from tessera_mcp.guard_phase_evidence import (
        CorePhaseContext,
        GuardPhaseExpectation,
        parse_core_model_hook_phase,
    )

    setup = guard_capture
    binding = setup["binding"]["guard_phase_binding"]
    expected = GuardPhaseExpectation(
        **{
            k: binding[k]
            for k in (
                "issuer_origin",
                "nonce",
                "session_ref",
                "proof_ref",
                "order_revision",
                "sequence",
                "model_id",
                "provider_sha256",
                "model_sha256",
                "tool_policy_sha256",
            )
        }
    )
    context = CorePhaseContext(
        binding["issuer_origin"],
        binding["enrollment_ref"],
        binding["adapter_ref"],
        binding["nonce"],
        binding["session_ref"],
        binding["proof_ref"],
    )
    phase = parse_core_model_hook_phase(
        json.dumps(setup["envelope"]["record"]["guard_phase"]["phase"]).encode(),
        expected=expected,
        context=context,
        now=datetime.now(UTC),
    )
    guard = guard_observation_from_report(phase)
    assert guard["owner_checks"] is None
    assert (
        guard["production_verified"]
        is guard["admission_allowed"]
        is guard["consumption_verified"]
        is False
    )
    body = deepcopy(setup["envelope"])
    body["record"]["guard_phase"] = guard
    assert append(setup, body).status_code == 201
