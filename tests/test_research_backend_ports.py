"""Real HTTP/SQLite human role proof; synthetic research records and model input."""

import hashlib
import json

import pytest
from test_capture_context_api import contexts, registered, separated  # noqa: F401

from tessera_agent.research_backend_ports import BackendJourneyRecords, resolve_contexts
from tessera_agent.research_consumer_store import ConsumerBlocked


class Endpoint:
    def __init__(self, setup):
        self.setup = setup

    def call(self, method, path, body=None):
        response = self.setup["client"].request(
            method, path, headers=self.setup["author"], json=body
        )
        if response.status_code != 200:
            raise ConsumerBlocked("actual_backend_read_denied")
        return response.json()


def issue_record(setup, *, decision="approved", sequence=1, peer=None):
    body = json.dumps(
        {
            "schema_version": "tessera-human-research-record-v1",
            "journey_id": "journey",
            "sequence": sequence,
            "references": {"control_state": "continue"},
            "disclosures": {} if peer is None else {peer["output_id"]: peer["disclosure_id"]},
            "planner_input": peer,
            "review": {"subject_id": "proposal", "decision": decision},
        }
    )

    def create(text):
        response = setup["client"].post(
            f"/projects/{setup['project']}/agent-contexts",
            headers=setup["http_human"]
            | {"Idempotency-Key": hashlib.sha256(text.encode()).hexdigest()},
            json={
                "title": "Human journey decision",
                "body": text,
                "classification": "project",
                "credential_id": setup["binding"]["credential_id"],
                "destination": setup["author"]["X-Agent-Destination"],
                "disclosure_approved": True,
            },
        )
        assert response.status_code == 201
        return response.json()

    record = create(body)
    policy = dict(
        setup["policy"],
        input_context_id=record["id"],
        input_body_digest=hashlib.sha256(body.encode()).hexdigest(),
        context_digest=hashlib.sha256(body.encode()).hexdigest(),
    )
    create(json.dumps(policy))
    return record


@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_actual_human_context_record_review_never_mints_execution_authority(request, decision):
    setup = request.getfixturevalue("contexts")
    endpoint = Endpoint(setup)
    resolution = resolve_contexts(endpoint, setup["binding"])
    assert resolution["input_context"]["issuer_role"] == "human"
    record = issue_record(setup, decision=decision)
    records = BackendJourneyRecords(endpoint, setup["binding"], "journey")
    review = records.read_human_review("proposal")
    assert review["id"] == record["id"] and review["decision"] == decision
    assert review["issuer_role"] == "human"
    assert "commands" not in records.read_current_references("journey")
    with pytest.raises(ConsumerBlocked, match="subject_changed"):
        records.read_human_review("foreign-proposal")


def test_current_human_issuer_revocation_blocks_record_not_a_cached_boolean(request):
    import sqlite3

    from tessera_control.store import Store

    setup = request.getfixturevalue("contexts")
    issue_record(setup)
    records = BackendJourneyRecords(Endpoint(setup), setup["binding"], "journey")
    assert records.read_human_review("proposal")["decision"] == "approved"
    with sqlite3.connect(Store(setup["state"]).database_path) as conn:
        conn.execute(
            "UPDATE principals SET revoked_at='now' WHERE id=?",
            (setup["http_issuer"]["principal_id"],),
        )
    with pytest.raises(ConsumerBlocked):
        records.read_human_review("proposal")


def test_admitted_prompt_restart_keeps_digest_and_opaque_references_only(tmp_path):
    from tessera_agent.remote_reasoning_store import ReasoningStore

    refs = {
        "output_id": "registered-output",
        "disclosure_id": "human-disclosure",
        "content_digest": "a" * 64,
        "input_context_id": "curated-input",
        "human_input_record_id": "human-input-record",
    }
    store = ReasoningStore(tmp_path / "private")
    store.bind("run", {"prompt_sha256": "b" * 64}, 1)
    store.admit_input("run", "c" * 64, refs)
    store.close()
    store = ReasoningStore(tmp_path / "private")
    try:
        assert store.input_digest("run") == "c" * 64
        store.admit_input("run", "c" * 64, refs)
        with pytest.raises(ValueError):
            store.admit_input("run", "d" * 64, refs)
        with pytest.raises(ValueError):
            store.admit_input("run", "c" * 64, {"raw_prompt": "must never persist"})
    finally:
        store.close()


def test_continuation_contains_actual_disclosed_critic_bytes_not_just_ack(request):
    from types import SimpleNamespace

    from tessera_agent.research_inputs import ResearchInputs

    setup = request.getfixturevalue("contexts")
    peer = {"output_id": setup["output"]["id"], "disclosure_id": setup["disclosure"]["id"]}
    issue_record(setup, peer=peer)
    endpoint = Endpoint(setup)
    records = BackendJourneyRecords(endpoint, setup["binding"], "journey")
    consumer = SimpleNamespace(binding=setup["binding"], fence=lambda: None)
    inputs = ResearchInputs({"planner": consumer}, {"planner": endpoint}, {"planner": records})
    text = inputs.read_continuation(peer["output_id"])
    assert setup["input_body"] in text
    assert "Synthetic separate-input critic recommendation; review required." in text
    assert "not authority" in text
