"""Read original V2 tool completion through an owner-supplied runtime connection.

No discovery, auth issuance or service startup. The connection is trusted setup,
not message/payload evidence: get(path, max_bytes=...) must perform one bounded
authenticated read against its pinned runtime_id, with no redirects or retries.
Published schema: https://opencode.ai/v2/openapi.json, session.message.get.
This module does not establish installed-runtime or per-effect admission proof.
"""

import json
import re
from pathlib import Path

from .maintenance_witness import read_json
from .schema_supervised import SupervisedDenied, digest

LIMIT = 1024 * 1024
FIELDS = {"runtime_id", "session_id", "message_id", "call_id", "tool", "build"}


def success(packet, ack, binding, upper):
    """Only returned by apply after the journal's final fsync and readback."""
    return dict(
        status="release_journal_durable",
        operation=packet["plan"]["operation"],
        plan_sha256=packet["plan_sha256"],
        claim_sha256=digest(packet),
        ack_sha256=digest(ack),
        action_claim_sha256=digest(upper),
        binding=binding,
    )


class MaintenanceCompletion:
    def __init__(self, connection, binding, action_directory):
        self.connection = connection
        self.binding = dict(binding)
        self.action_directory = Path(action_directory)
        if set(binding) != FIELDS or any(not isinstance(v, str) or not v for v in binding.values()):
            raise SupervisedDenied("invalid native completion binding")
        for key in ("session_id", "message_id", "call_id"):
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", binding[key]):
                raise SupervisedDenied("invalid native completion identity")

    def invocation(self, packet):
        """Derive exact effect arguments from retained upper/lower claims.

        Native call context must be supplied by trusted integration, not tool
        arguments. It is never inserted into prepared bytes (no digest cycle).
        """
        upper = read_json(self.action_directory / "release.claim.json")[0]
        prepared = read_json(self.action_directory / "release.prepared.json")[0]
        if (
            upper != prepared
            or upper["operation"] != packet["plan"]["operation"]
            or upper["action"] != "release"
            or upper["lower_sha256"] != digest(packet)
        ):
            raise SupervisedDenied("upper/lower release claim mismatch")
        return upper, dict(
            operation=upper["operation"], action="release", expected_action_sha256=digest(upper)
        )

    def result(self, packet, ack, upper):
        observed, _ = self.invocation(packet)
        if observed != upper:
            raise SupervisedDenied("upper release claim mismatch")
        return success(packet, ack, self.binding, upper)

    def confirm(self, packet, ack):
        try:
            b = self.binding
            if self.connection is None or self.connection.runtime_id != b["runtime_id"]:
                raise ValueError
            upper, invocation = self.invocation(packet)
            path = f"/api/session/{b['session_id']}/message/{b['message_id']}"
            raw = self.connection.get(path, max_bytes=LIMIT)
            if not isinstance(raw, bytes) or len(raw) > LIMIT:
                raise ValueError
            message = json.loads(raw)["data"]
            if (
                message["id"] != b["message_id"]
                or message["type"] != "assistant"
                or message.get("error")
                or message.get("finish") in {"length", "content-filter", "error", "unknown"}
            ):
                raise ValueError
            parts = [
                p
                for p in message["content"]
                if p.get("type") == "tool" and p.get("id") == b["call_id"]
            ]
            if len(parts) != 1:
                raise ValueError
            part = parts[0]
            state = part["state"]
            if (
                part["name"] != b["tool"]
                or part.get("executed") is True
                or state["status"] != "completed"
                or state["input"] != invocation
                or state.get("metadata", {}).get("truncated")
                or len(state["content"]) != 1
                or state["content"][0]["type"] != "text"
                or json.loads(state["content"][0]["text"]) != success(packet, ack, b, upper)
            ):
                raise ValueError
        except Exception:
            # Never include full messages, transport errors or credential material.
            raise SupervisedDenied("original native completion unavailable or mismatched") from None
        return True
