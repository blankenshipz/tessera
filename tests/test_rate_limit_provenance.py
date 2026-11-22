"""Rate-limit/quota events reach the provenance outbox, content-free (ADR 0083)."""

import hashlib
import json

import httpx
import pytest

from tessera_agent.provenance_client import ProvenanceClient, sanitize_envelope
from tessera_agent.remote_reasoning_store import ReasoningStore

PROMPT = "SENTINEL-PROMPT-TEXT-do-not-journal"
CREDENTIAL = "anthropic-api-key://SENTINEL-OWNER/SENTINEL-CREDENTIAL"
POLICY = "a" * 64


def binding():
    return {"grant_id": "grant1", "revision": 1, "epoch": 1, "model": "model1"}


def make(tmp_path):
    store = ReasoningStore(tmp_path / "runtime")
    store.bind("run1", binding(), 100)
    return store


def acknowledge(request):
    body = json.loads(request.content)
    ack = {
        **body["record"],
        "sequence": 1,
        "org_id": "org1",
        "project_id": request.url.path.split("/")[2],
        "actor_principal_id": "agent1",
        "credential_id": "credential1",
        "recorded_at": "2026-10-04T00:00:00+00:00",
        "model_attribution": "self_reported" if body["record"]["model_id"] else "unknown",
    }
    ack["remote"] = {
        **ack["remote"],
        "destination_digest": "sha256:"
        + hashlib.sha256(request.headers["X-Agent-Destination"].encode()).hexdigest(),
    }
    if body["grant_proof"]:
        ack["grant_revision"] = body["grant_proof"]["expected_revision"]
        ack["grant_epoch"] = body["grant_proof"]["expected_epoch"]
    return httpx.Response(201, json=ack)


def rate_rows(store):
    return [
        r
        for r in store.provenance_pending("run1", 64)
        if "rate_limit" in json.loads(r["body"])["record"]
    ]


@pytest.mark.parametrize("condition", ["model_rate_limited", "model_quota_exhausted"])
def test_event_enqueues_exactly_one_sanitized_record(tmp_path, condition):
    store = make(tmp_path)
    store.model_call("run1", {"tier": "heavy", "policy_sha256": POLICY, "tokens": 5}, 101)
    before = len(store.provenance_pending("run1", 64))
    store.rate_limit("run1", condition, 250.5, 102)
    rows = rate_rows(store)
    assert len(rows) == 1 and len(store.provenance_pending("run1", 64)) == before + 1
    record = json.loads(rows[0]["body"])["record"]
    assert record["error_code"] == condition
    assert record["causation_id"] == "run1" and record["model_id"] == "model1"
    assert record["rate_limit"] == {
        "backoff_until": 250.5,
        "tier": "heavy",
        "policy_sha256": POLICY,
    }
    store.close()


def test_metadata_optional_when_receipt_absent(tmp_path):
    store = make(tmp_path)
    store.rate_limit("run1", "model_rate_limited", 9.0, 1)
    (row,) = rate_rows(store)
    assert json.loads(row["body"])["record"]["rate_limit"] == {"backoff_until": 9.0}
    store.close()


def test_each_event_row_has_its_own_record_and_drain_is_idempotent(tmp_path):
    store = make(tmp_path)
    store.rate_limit("run1", "model_rate_limited", 10.0, 1)
    store.rate_limit("run1", "model_quota_exhausted", 20.0, 2)
    rows = rate_rows(store)
    assert len({r["record_id"] for r in rows}) == 2 == len({r["key"] for r in rows})
    seen = []

    def handler(request):
        seen.append(request.headers["Idempotency-Key"])
        return acknowledge(request)

    with ProvenanceClient(
        "http://127.0.0.1:8765", "synthetic-token", "lab", transport=httpx.MockTransport(handler)
    ) as client:
        assert store.drain_provenance("run1", client, "project1")["delivery_complete"]
        assert store.drain_provenance("run1", client, "project1")["delivery_complete"]
    assert len(seen) == len(set(seen)) and {r["key"] for r in rows} <= set(seen)
    assert len(rate_rows(store)) == 0  # all accepted, none pending
    assert store.provenance_coverage("run1")["recorded"] == len(seen)
    store.close()


def test_no_prompt_or_credential_in_outbox(tmp_path):
    store = make(tmp_path)
    store.model_call("run1", {"tier": "heavy", "policy_sha256": POLICY}, 1)
    store.rate_limit("run1", "model_quota_exhausted", 5.0, 2)
    blob = json.dumps(store.provenance_pending("run1", 64))
    assert PROMPT not in blob and "SENTINEL" not in blob and "anthropic-api-key" not in blob
    store.close()


def base():
    return {
        "record_id": "r1",
        "kind": "model",
        "outcome": "failed",
        "correlation_id": "s1",
        "error_code": "model_rate_limited",
        "rate_limit": {"backoff_until": 1.0},
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(error_code="made_up_code"),
        lambda r: r.update(error_code="rate_limited"),
        lambda r: r["rate_limit"].update(prompt=PROMPT),
        lambda r: r["rate_limit"].update(tier="free text with spaces"),
        lambda r: r["rate_limit"].update(policy_sha256="nothex"),
        lambda r: r["rate_limit"].update(backoff_until=float("nan")),
        lambda r: r.update(body="response text"),
    ],
)
def test_sanitize_still_rejects_unknown(mutate):
    record = base()
    sanitize_envelope(base())
    mutate(record)
    with pytest.raises(ValueError):
        sanitize_envelope(record)
