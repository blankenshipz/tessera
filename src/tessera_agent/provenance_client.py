"""One-request enrolled-agent metadata collector client; no authority or inference."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Literal

import httpx
from pydantic import ConfigDict, Field

from tessera_control.agent_provenance_models import AgentProvenanceRecord, AppendAgentProvenance
from tessera_worker.opencode_http import OpenCodeHttpClient

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")
_DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")


def sanitize_envelope(record, grant_proof=None) -> dict:
    """Strict content-free shape; opaque IDs must come from authorized registries."""
    try:
        body = AppendAgentProvenance.model_validate(
            {
                "record": record,
                "grant_proof": grant_proof,
            }
        ).model_dump(mode="json")
        value = body["record"]
        identifiers = (
            [
                value[k]
                for k in (
                    "record_id",
                    "correlation_id",
                    "causation_id",
                    "model_id",
                    "tool_id",
                )
            ]
            + list(value["references"].values())
            + list(value["remote"].values())
        )
        proof = body["grant_proof"]
        if proof:
            identifiers.append(proof["grant_id"])
        if any(item is not None and not _ID.fullmatch(item) for item in identifiers):
            raise ValueError()
        if value["content_digest"] is not None and not _DIGEST.fullmatch(value["content_digest"]):
            raise ValueError()
        if value["tool_id"] and value["tool_id"].startswith(
            (
                "human.",
                "approval.",
                "decision.",
                "authority.issue",
                "authority.approve",
            )
        ):
            raise ValueError()
        if len(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()) > 16384:
            raise ValueError()
        return body
    except Exception:
        raise ValueError("Provenance requires a bounded content-free typed envelope.") from None


def stable_key(record_id: str) -> str:
    if not _ID.fullmatch(record_id):
        raise ValueError("Invalid opaque provenance identity.")
    return "provenance-" + hashlib.sha256(record_id.encode()).hexdigest()


@dataclass(frozen=True)
class DeliveryResult:
    status: Literal["accepted", "rejected", "unknown"]
    code: str
    record_id: str
    status_code: int | None = None
    sequence: int | None = None


class _CollectorAcknowledgement(AgentProvenanceRecord):
    """Strict actual collector response, including authenticated row attribution."""

    model_config = ConfigDict(extra="forbid", strict=True, regex_engine="python-re")

    sequence: int = Field(ge=1)
    org_id: str = Field(pattern="^" + _ID.pattern)
    project_id: str = Field(pattern="^" + _ID.pattern)
    actor_principal_id: str = Field(pattern="^" + _ID.pattern)
    credential_id: str = Field(pattern="^" + _ID.pattern)
    recorded_at: str = Field(min_length=1, max_length=128)
    model_attribution: Literal["self_reported", "unknown"]


class ProvenanceClient:
    def __init__(
        self,
        control_url,
        agent_token,
        destination,
        *,
        timeout=5,
        transport=None,
        expected_actor=None,
        verify=True,
    ):
        if verify is False:
            raise ValueError("Collector TLS verification cannot be disabled.")
        self.origin = OpenCodeHttpClient._origin(control_url)
        if not 0 < timeout <= 5:
            raise ValueError("Provenance timeout must be bounded to five seconds.")
        if any(
            not isinstance(v, str)
            or not v
            or len(v) > 4096
            or any(ord(c) <= 32 or ord(c) >= 127 for c in v)
            for v in (agent_token, destination)
        ):
            raise ValueError("Invalid supplied enrolled-agent reference.")
        self._headers = {
            "Authorization": "Bearer " + agent_token,
            "X-Agent-Destination": destination,
            "Content-Type": "application/json",
        }
        self._timeout = timeout
        self._destination = destination
        self._actor = None
        if expected_actor is not None:
            if (
                not isinstance(expected_actor, dict)
                or set(expected_actor) != {"org_id", "actor_principal_id", "credential_id"}
                or any(
                    not isinstance(value, str) or not _ID.fullmatch(value)
                    for value in expected_actor.values()
                )
            ):
                raise ValueError("Expected collector actor requires exact opaque enrollment IDs.")
            self._actor = dict(expected_actor)
        self._client = httpx.Client(
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
            verify=verify,
        )

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def append(self, project_id, record, grant_proof=None, idempotency_key=None) -> DeliveryResult:
        if not _ID.fullmatch(project_id):
            raise ValueError("Invalid project reference.")
        body = sanitize_envelope(record, grant_proof)
        rid = body["record"]["record_id"]
        key = idempotency_key or stable_key(rid)
        if not _ID.fullmatch(key):
            raise ValueError("Invalid stable idempotency identity.")
        headers = {**self._headers, "Idempotency-Key": key}
        deadline = time.monotonic() + self._timeout
        try:
            self._client.cookies.clear()
            with self._client.stream(
                "POST",
                self.origin + f"/projects/{project_id}/agent-provenance",
                headers=headers,
                content=json.dumps(body, sort_keys=True, separators=(",", ":")),
            ) as response:
                status = response.status_code
                if 300 <= status < 500:
                    return DeliveryResult("rejected", "collector_rejected", rid, status)
                if status not in {200, 201}:
                    return DeliveryResult("unknown", "collector_unavailable", rid, status)
                raw = bytearray()
                for chunk in response.iter_bytes(chunk_size=4096):
                    if time.monotonic() > deadline:
                        return DeliveryResult("unknown", "response_timeout", rid, status)
                    raw.extend(chunk)
                    if len(raw) > 65536:
                        return DeliveryResult("unknown", "response_bound", rid, status)
                try:
                    ack = _CollectorAcknowledgement.model_validate(json.loads(raw))
                except Exception:
                    return DeliveryResult("unknown", "invalid_acknowledgement", rid, status)
                expected = AgentProvenanceRecord.model_validate(body["record"]).model_dump(
                    mode="json"
                )
                if body["grant_proof"]:
                    proof = body["grant_proof"]
                    if expected["references"]["grant_id"] != proof["grant_id"] or any(
                        expected[field] is not None and expected[field] != proof[pin]
                        for field, pin in (
                            ("grant_revision", "expected_revision"),
                            ("grant_epoch", "expected_epoch"),
                        )
                    ):
                        return DeliveryResult("unknown", "invalid_acknowledgement", rid, status)
                    expected["grant_revision"] = proof["expected_revision"]
                    expected["grant_epoch"] = proof["expected_epoch"]
                expected["remote"]["destination_digest"] = (
                    "sha256:" + hashlib.sha256(self._destination.encode()).hexdigest()
                )
                observed = AgentProvenanceRecord.model_validate(
                    {field: getattr(ack, field) for field in AgentProvenanceRecord.model_fields}
                ).model_dump(mode="json")
                actor = {
                    field: getattr(ack, field)
                    for field in (
                        "org_id",
                        "actor_principal_id",
                        "credential_id",
                    )
                }
                if (
                    observed != expected
                    or ack.project_id != project_id
                    or (self._actor is not None and actor != self._actor)
                    or ack.model_attribution
                    != ("self_reported" if expected["model_id"] else "unknown")
                ):
                    return DeliveryResult("unknown", "invalid_acknowledgement", rid, status)
                # Without enrollment pins, this is first-ack server attribution,
                # not independent actor verification. Keep it stable thereafter.
                self._actor = actor
                return DeliveryResult(
                    "accepted", "collector_acknowledged", rid, status, ack.sequence
                )
        except Exception:
            return DeliveryResult("unknown", "transport_failure", rid)
        finally:
            self._client.cookies.clear()
