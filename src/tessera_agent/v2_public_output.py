"""Pinned V2 final assistant-text projection; never a complete public-event stream.

The V2 Session.Message.Assistant schema separates text/reasoning/tool content.
Only terminal assistant text may be registered under a separate human output
admission. Text type alone is not public disclosure or verified model generation.
No input, reasoning, provider state or tool payload is returned or persisted.
"""

import hashlib
import json
import math
import re
from dataclasses import dataclass, field

from .research_consumer_store import ConsumerBlocked


@dataclass(frozen=True)
class FinalOutput:
    source_event_id: str
    source_sequence: int
    content: bytes = field(repr=False)

    @property
    def digest(self):
        return hashlib.sha256(self.content).hexdigest()


class V2FinalOutputReader:
    """One explicitly bounded eligible final-output stream, sequence 1.

    Schema pin verification is delegated to the existing V2 harness verifier on
    every read. This never assigns sequences by counting private message parts.
    Human registration admission remains an independent backend check.
    """

    def __init__(self, harness):
        self.harness = harness

    def read(self, row, *, source_sequence=1):
        if type(source_sequence) is not int or source_sequence not in {1, 2}:
            raise ConsumerBlocked("unsupported_final_output_phase")
        self.harness.verify()
        page = self.harness.endpoint.call(
            "GET", f"/api/session/{row['session_id']}/message?type=assistant&order=desc&limit=1"
        )
        if not isinstance(page, dict) or set(page) != {"data", "cursor"}:
            raise ConsumerBlocked("unsupported_v2_message_schema")
        if not isinstance(page["data"], list) or len(page["data"]) != 1:
            raise ConsumerBlocked("final_public_output_unavailable")
        message = page["data"][0]
        return self._project(row, message, source_sequence)

    def read_after_prompt(self, row, prompt_message_id, source_sequence):
        """Bounded actual message readback; no historical-final substitution.

        The serial session must contain this exact admitted user prompt and no
        later user prompt. The assistant must be created after that user prompt.
        Private content is never used for ordering, watermark or eligibility.
        """
        self.harness.verify()
        digest = row.get("expected_prompt_digest")
        if digest is None and source_sequence == 1:
            digest = json.loads(row["binding"])["prompt_sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ConsumerBlocked("public_output_prompt_digest_required")
        page = self.harness.endpoint.call(
            "GET", f"/api/session/{row['session_id']}/message?order=desc&limit=100"
        )
        if (
            not isinstance(page, dict)
            or set(page) != {"data", "cursor"}
            or not isinstance(page["data"], list)
            or not 1 <= len(page["data"]) <= 100
        ):
            raise ConsumerBlocked("public_output_prompt_readback_unknown")
        messages = page["data"]
        targets = [m for m in messages if isinstance(m, dict) and m.get("id") == prompt_message_id]
        if len(targets) != 1 or targets[0].get("type") != "user":
            raise ConsumerBlocked("public_output_prompt_readback_unknown")
        target = targets[0]
        parts = target.get("content")
        if (
            not isinstance(parts, list)
            or not parts
            or any(
                not isinstance(p, dict)
                or p.get("type") != "text"
                or not isinstance(p.get("text"), str)
                for p in parts
            )
        ):
            raise ConsumerBlocked("public_output_prompt_readback_unknown")
        if hashlib.sha256("\n".join(p["text"] for p in parts).encode()).hexdigest() != digest:
            raise ConsumerBlocked("public_output_prompt_digest_changed")
        created = target.get("time", {}).get("created")
        if type(created) not in {int, float} or not math.isfinite(created):
            raise ConsumerBlocked("public_output_prompt_time_unknown")
        candidates = []
        for message in messages:
            if not isinstance(message, dict) or not isinstance(message.get("time"), dict):
                raise ConsumerBlocked("public_output_message_order_unknown")
            if message.get("type") not in {"user", "assistant"}:
                raise ConsumerBlocked("public_output_message_order_unknown")
            timestamp = message["time"].get("created")
            if type(timestamp) not in {int, float} or not math.isfinite(timestamp):
                raise ConsumerBlocked("public_output_message_order_unknown")
            if message.get("id") == prompt_message_id or timestamp < created:
                continue
            if timestamp == created or message.get("type") == "user":
                raise ConsumerBlocked("public_output_session_turn_ambiguous")
            if message.get("type") == "assistant":
                candidates.append(message)
        if not candidates:
            raise ConsumerBlocked("fresh_public_output_unavailable")
        latest = max(candidates, key=lambda m: m["time"]["created"])
        if sum(m["time"]["created"] == latest["time"]["created"] for m in candidates) != 1:
            raise ConsumerBlocked("public_output_session_turn_ambiguous")
        completed = latest["time"].get("completed")
        if type(completed) not in {int, float} or completed < latest["time"]["created"]:
            raise ConsumerBlocked("public_output_session_turn_ambiguous")
        return self._project(row, latest, source_sequence)

    def _project(self, row, message, source_sequence):
        if type(source_sequence) is not int or source_sequence not in {1, 2}:
            raise ConsumerBlocked("unsupported_final_output_phase")
        allowed = {
            "id",
            "metadata",
            "time",
            "type",
            "agent",
            "model",
            "content",
            "snapshot",
            "finish",
            "rawFinish",
            "providerState",
            "cost",
            "tokens",
            "error",
            "retry",
        }
        if (
            not isinstance(message, dict)
            or set(message) - allowed
            or message.get("type") != "assistant"
            or message.get("finish") != "stop"
            or message.get("error") is not None
            or message.get("agent") != self.harness.profile["agent"]
            or message.get("model") != self.harness.profile["model"]
            or not isinstance(message.get("id"), str)
            or not re.fullmatch(r"msg_[A-Za-z0-9_.:-]{1,155}", message["id"])
            or not isinstance(message.get("time"), dict)
            or type(message["time"].get("completed")) not in {int, float}
            or not math.isfinite(message["time"]["completed"])
            or not isinstance(message.get("content"), list)
        ):
            raise ConsumerBlocked("final_public_output_binding_unknown")
        text = []
        for part in message["content"]:
            if not isinstance(part, dict) or part.get("type") not in {"text", "reasoning", "tool"}:
                raise ConsumerBlocked("unsupported_v2_content_schema")
            if part["type"] != "text":
                continue  # Do not inspect or copy hidden/provider/tool content.
            if set(part) - {"type", "text", "state"} or not isinstance(part.get("text"), str):
                raise ConsumerBlocked("unsupported_v2_text_schema")
            text.append(part["text"])
        content = "\n".join(text).encode("utf-8")
        if not content or len(content) > 65536 or b"\x00" in content:
            raise ConsumerBlocked("public_output_size_invalid")
        event = (
            "final_"
            + hashlib.sha256((row["session_id"] + "\n" + message["id"]).encode()).hexdigest()
        )
        return FinalOutput(event, source_sequence, content)
