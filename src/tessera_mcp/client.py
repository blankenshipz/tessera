"""Explicit allowlisted HTTP calls; no generic proxy and no response/error logging."""

import base64
import hashlib
import json
import ssl
from urllib.parse import quote

import httpx

from tessera_mcp.config import Config


class ControlError(Exception):
    pass


MAX_ARTIFACT_BYTES = 65536
MAX_CONTEXT_JSON_BYTES = 128 * 1024


def segment(value: str) -> str:
    if not value or len(value) > 256 or value in {".", ".."}:
        raise ControlError("Invalid record identifier")
    return quote(value, safe="")


class ControlClient:
    def __init__(self, config: Config, transport=None):
        self.config = config
        self.transport = transport

    async def _request(self, method, path, *, body=None, key=None, max_bytes=2 * 1024 * 1024):
        headers = {
            "Authorization": "Bearer " + self.config.token(),
            "X-Agent-Destination": self.config.destination,
        }
        if key is not None:
            if not 1 <= len(key) <= 128 or any(ord(c) < 33 or ord(c) > 126 for c in key):
                raise ControlError("Idempotency key must be 1–128 printable non-space characters")
            headers["Idempotency-Key"] = key
        try:
            async with httpx.AsyncClient(
                base_url=self.config.endpoint,
                timeout=20,
                trust_env=False,
                follow_redirects=False,
                transport=self.transport,
                verify=ssl.create_default_context(cafile=str(self.config.ca_file))
                if self.config.ca_file
                else True,
            ) as client:
                async with client.stream(method, path, json=body, headers=headers) as response:
                    if response.status_code not in {200, 201}:
                        # Never load/relay denial bodies, which could contain protected content.
                        raise ControlError(
                            f"Control API denied or rejected request (HTTP {response.status_code})"
                        )
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(content) + len(chunk) > max_bytes:
                            raise ControlError(
                                "Control response exceeds adapter serialization bound"
                            )
                        content.extend(chunk)
            return json.loads(content)
        except (httpx.HTTPError, ValueError):
            raise ControlError(
                "Control API unavailable or invalid response; no automatic retry"
            ) from None

    async def status(self):
        return await self._request("GET", "/external-agent/status")

    async def contexts(self, offset=0, limit=20):
        result = await self._request(
            "GET",
            self._collection("agent-contexts", offset, limit),
            max_bytes=MAX_CONTEXT_JSON_BYTES * limit,
        )
        for context in result["contexts"]:
            self._validate_context(context)
        return result

    async def context(self, context_id):
        result = await self._request(
            "GET", "/agent-contexts/" + segment(context_id), max_bytes=MAX_CONTEXT_JSON_BYTES
        )
        self._validate_context(result)
        return result

    @staticmethod
    def _validate_context(context):
        """Defense-in-depth output bound, not a substitute for backend disclosure authority."""
        if len(json.dumps(context).encode()) > MAX_CONTEXT_JSON_BYTES:
            raise ControlError("Context exceeds adapter serialization bound")
        if "content_base64" not in context:
            return
        encoded = context["content_base64"]
        try:
            if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_ARTIFACT_BYTES + 2) // 3):
                raise ValueError
            content = base64.b64decode(encoded, validate=True)
            if (
                len(content) > MAX_ARTIFACT_BYTES
                or context.get("size") != len(content)
                or context.get("sha256") != hashlib.sha256(content).hexdigest()
            ):
                raise ValueError
        except ValueError:
            raise ControlError("Invalid or oversized disclosed artifact context") from None

    async def proposals(self, offset=0, limit=20):
        return await self._request("GET", self._collection("agent-proposals", offset, limit))

    async def proposal(self, proposal_id):
        return await self._request("GET", "/agent-proposals/" + segment(proposal_id))

    async def propose(self, plan, provenance, idempotency_key):
        return await self._request(
            "POST",
            f"/projects/{segment(self.config.project_id)}/agent-proposals",
            body={"proposal_type": "task_plan", "plan": plan, "provenance": provenance},
            key=idempotency_key,
        )

    def _collection(self, name, offset, limit):
        if not 0 <= offset or not 1 <= limit <= 100:
            raise ControlError("Pagination requires offset >= 0 and limit 1–100")
        return f"/projects/{segment(self.config.project_id)}/{name}?offset={offset}&limit={limit}"
