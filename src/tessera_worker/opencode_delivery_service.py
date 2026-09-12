# ruff: noqa: E501
"""Worker-side consumer for context-only OpenCode message deliveries.

It receives no Kubernetes credentials and has no provider/model/tool path.  The
live adapter is the only upstream caller; control remains authoritative for
claim, acknowledgement, uncertainty, and cancellation fencing.
"""

# fmt: off

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .control_channel import HttpControlChannel
from .opencode_harness import OpenCodeHarnessError, TesseraBinding
from .opencode_live_adapter import LiveOpenCodeHarnessAdapter
from .service import _private_directory, read_private_credential


@dataclass(frozen=True)
class OpenCodeDeliveryWorkerConfig:
    control_url: str
    delivery_id: str
    credential_file: Path
    workspace_root: Path


class OpenCodeDeliveryWorkerService:
    """Claim once, call the real live adapter once, then report immutable evidence."""

    def __init__(
        self,
        config: OpenCodeDeliveryWorkerConfig,
        adapter: LiveOpenCodeHarnessAdapter,
        *,
        channel: Any | None = None,
    ) -> None:
        if not config.delivery_id or len(config.delivery_id) > 128:
            raise ValueError("OpenCode delivery configuration is invalid.")
        self.config = config
        self.adapter = adapter
        self.root = _private_directory(config.workspace_root)
        self.channel = channel or HttpControlChannel(
            config.control_url, read_private_credential(config.credential_file)
        )

    def _key(self, action: str, body: dict[str, object]) -> str:
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return "opencode-delivery-" + hashlib.sha256(
            f"v1:{self.config.delivery_id}:{action}:{encoded}".encode()
        ).hexdigest()

    def run(self, expected_epoch: int) -> dict[str, object]:
        claim_body = {"expected_epoch": expected_epoch}
        response = self.channel.post(
            f"/worker/interaction-deliveries/{self.config.delivery_id}/claim",
            claim_body,
            self._key("claim", claim_body),
        )
        delivery = response.get("delivery")
        if not isinstance(delivery, dict) or delivery.get("delivery_id") != self.config.delivery_id:
            raise OpenCodeHarnessError("Control returned an invalid OpenCode delivery claim.")
        if delivery.get("epoch") != expected_epoch or not isinstance(delivery.get("body"), str):
            raise OpenCodeHarnessError("OpenCode delivery claim has an invalid epoch or context.")
        binding = TesseraBinding(
            organization_id=str(delivery.get("organization_id", "")), project_id=str(delivery.get("project_id", "")),
            task_id=str(delivery.get("task_id", "")), operation_id=str(delivery.get("operation_id", "")),
            epoch=expected_epoch, harness_instance_id=str(delivery.get("harness_instance_id", "")),
            attempt_id=str(delivery.get("admitted_attempt_id", "")),
            operation_authority_ref="delivery", attempt_authority_ref=f"delivery-{self.config.delivery_id}",
            session_id=str(delivery.get("upstream_session_id", "")),
        )
        context_digest = str(delivery.get("context_digest", ""))
        try:
            upstream_id, readback_digest = self.adapter.deliver_context_only(
                binding, self.config.delivery_id, delivery["body"], context_digest
            )
        except OpenCodeHarnessError:
            body = {"expected_revision": delivery["revision"], "context_digest": context_digest,
                    "outcome": "uncertain", "error_code": "upstream_uncertain"}
            return self.channel.post(
                f"/worker/interaction-deliveries/{self.config.delivery_id}/report", body, self._key("report", body)
            )
        body = {"expected_revision": delivery["revision"], "context_digest": context_digest,
                "upstream_message_id": upstream_id, "upstream_readback_digest": readback_digest}
        return self.channel.post(
            f"/worker/interaction-deliveries/{self.config.delivery_id}/ack", body, self._key("ack", body)
        )
