"""Bounded A2A 1.0 JSON-RPC client for the existing official SDK peer routes."""

import json
import re

import httpx

from tessera_control.a2a_store import generated_reference, validate_generated_pins
from tessera_control.agent_authority_models import AgentAuthorityBinding
from tessera_worker.opencode_http import OpenCodeHttpClient

from .research_consumer_store import ConsumerBlocked


def validate_generated(value):
    # Share the peer's closed wire validator, never accept a caller public flag.
    from tessera_control.a2a_store import validate_generated_pins

    if not isinstance(value, dict) or value.get("family") != "research-generated-v1":
        raise ConsumerBlocked("research_generated_family_required")
    return validate_generated_pins(value)


class ResearchPeer:
    def __init__(self, url, token, *, transport=None, verify=True):
        if verify is False:
            raise ConsumerBlocked("peer_tls_verification_required")
        self.url = OpenCodeHttpClient._origin(url) + "/"
        if (
            not isinstance(token, str)
            or not token
            or any(ord(c) <= 32 or ord(c) >= 127 for c in token)
        ):
            raise ConsumerBlocked("invalid_peer_credential_reference")
        self.client = httpx.Client(
            timeout=5, trust_env=False, follow_redirects=False, transport=transport, verify=verify
        )
        self.headers = {"Authorization": "Bearer " + token, "A2A-Version": "1.0"}

    def close(self):
        self.client.close()

    def call(self, method, params, request_id):
        try:
            with self.client.stream(
                "POST",
                self.url,
                headers=self.headers,
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                },
            ) as response:
                if response.status_code != 200:
                    raise ValueError()
                body = bytearray()
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > 256 * 1024:
                        raise ValueError()
                    body.extend(chunk)
            value = json.loads(body)
            if value.get("id") != request_id or value.get("jsonrpc") != "2.0" or "error" in value:
                raise ValueError()
            return value["result"]
        except Exception:
            raise ConsumerBlocked("peer_delivery_unknown_or_denied") from None

    def send(self, operation, payload, message_id, *, task_id=""):
        if operation not in {"submit", "result", "consume"}:
            raise ConsumerBlocked("unsupported_peer_operation")
        try:
            if set(payload) != {"reference", "generated", "authority"}:
                raise ValueError()
            generated = validate_generated_pins(payload["generated"])
            if payload["reference"] != generated_reference(generated):
                raise ValueError()
            authority = payload["authority"]
            if set(authority) != {"grant_id", "revision", "epoch", "binding"}:
                raise ValueError()
            for name in ("revision", "epoch"):
                if type(authority[name]) is not int or authority[name] < 1:
                    raise ValueError()
            binding = AgentAuthorityBinding.model_validate(authority["binding"])
            if any(
                isinstance(item, str)
                and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", item)
                for item in binding.model_dump().values()
            ):
                raise ValueError()
            if any(
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", value)
                for value in (message_id, authority["grant_id"], payload["reference"])
            ):
                raise ValueError()
            if task_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", task_id):
                raise ValueError()
            if len(json.dumps(payload).encode()) > 16384:
                raise ValueError()
        except Exception:
            raise ConsumerBlocked("peer_reference_envelope_invalid") from None
        value = {**payload, "operation": operation}
        result = self.call(
            "SendMessage",
            {
                "message": {
                    "messageId": message_id,
                    "role": "ROLE_USER",
                    "taskId": task_id,
                    "parts": [{"data": value}],
                }
            },
            message_id,
        )
        task = result["task"]
        if task_id and task["id"] != task_id:
            raise ConsumerBlocked("peer_task_changed")
        return task

    def read(self, task_id):
        return self.call("GetTask", {"id": task_id}, "read-" + task_id)
