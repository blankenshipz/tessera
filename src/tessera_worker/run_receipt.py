"""Pure builder of an observed run-receipt body from facts a worker really saw.

Nothing here performs I/O or consults a clock.  Every field is either copied
from the confirmed terminal ``docker inspect`` reply, the output manifest digest
control returned at upload (the local seal digest rides in the payload as
``sealed_manifest_digest``), or the already admitted operation.  The builder never
supplies a default for a missing fact: a missing or malformed fact raises
:class:`RunReceiptError` and no receipt exists.  The control plane verifies the
digest with the same canonical encoding and attributes the receipt to one
immutable operation; this module does not decide success or scientific meaning.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

ADAPTER_VERSION = "docker-local-cpu-v1"
RECEIPT_SCHEMA = "tessera.run-receipt.docker-local-cpu.v1"
MAX_RECEIPT_PAYLOAD_BYTES = 65536

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_DIGEST = re.compile(r"@sha256:([0-9a-f]{64})\Z")
_IMAGE_ID = re.compile(r"sha256:([0-9a-f]{64})\Z")
_DOCKER_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?Z\Z")
_ZERO_TIME = "0001-01-01T00:00:00Z"


class RunReceiptError(ValueError):
    """A required terminal fact is missing or malformed; no receipt may be built."""


def canonical_json(value: Any) -> str:
    """The exact encoding the control plane verifies (sorted, compact, ASCII)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def command_digest(argv: Sequence[str]) -> str:
    """sha256 over the canonical JSON array of the exact argv strings."""
    if not argv or any(not isinstance(item, str) for item in argv):
        raise RunReceiptError("Command argv is required.")
    return sha256_hex(canonical_json(list(argv)))


def parse_docker_time(value: Any) -> str:
    """Normalize Docker's RFC3339Nano UTC stamp to a microsecond ISO-8601 offset."""
    if not isinstance(value, str) or value == _ZERO_TIME:
        raise RunReceiptError("Container timestamp was not observed.")
    match = _DOCKER_TIME.fullmatch(value)
    if match is None:
        raise RunReceiptError("Container timestamp is malformed.")
    year, month, day, hour, minute, second = (int(part) for part in match.groups()[:6])
    micro = int((match.group(7) or "").ljust(6, "0")[:6])
    try:
        stamp = datetime(year, month, day, hour, minute, second, micro, tzinfo=UTC)
    except ValueError as error:
        raise RunReceiptError("Container timestamp is malformed.") from error
    return stamp.isoformat()


@dataclass(frozen=True)
class TerminalFacts:
    """What one confirmed-exit inspect reply showed; paths and env are never read."""

    container_id: str
    exit_code: int
    started_at: str
    finished_at: str
    image_id: str | None


def terminal_facts(
    inspect_stdout: str, *, operation_id: str, plan_digest: str, container_id: str, exit_code: int
) -> TerminalFacts:
    """Extract and cross-check terminal facts against the already confirmed exit."""
    try:
        items = json.loads(inspect_stdout)
        if not isinstance(items, list) or len(items) != 1:
            raise ValueError
        info = items[0]
        labels = info["Config"]["Labels"]
        state = info["State"]
        if (
            labels.get("tessera.operation") != operation_id
            or labels.get("tessera.plan") != plan_digest
            or info["Id"] != container_id
            or state.get("Status") not in {"exited", "dead"}
            or state.get("Running") is not False
            or type(state.get("ExitCode")) is not int
            or state["ExitCode"] != exit_code
        ):
            raise ValueError
        started = parse_docker_time(state.get("StartedAt"))
        finished = parse_docker_time(state.get("FinishedAt"))
        raw_image = info.get("Image")
        image_match = _IMAGE_ID.fullmatch(raw_image) if isinstance(raw_image, str) else None
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        if isinstance(error, RunReceiptError):
            raise
        raise RunReceiptError(
            "Terminal inspect reply does not match the confirmed exit."
        ) from error
    return TerminalFacts(
        container_id, exit_code, started, finished, image_match.group(1) if image_match else None
    )


def idempotency_key(operation_id: str, lease_id: str, epoch: int) -> str:
    """Deterministic, so a restart after a lost reply replays the same request."""
    return "development-run-receipt-" + sha256_hex(f"v1:{operation_id}:{lease_id}:{epoch}")


def build_run_receipt(
    *,
    lease_id: str,
    epoch: int,
    operation_id: str,
    plan_digest: str,
    image: str,
    argv: Sequence[str],
    network_profile: str,
    facts: TerminalFacts,
    control_manifest_digest: str,
    sealed_manifest_digest: str,
    scratch_bytes: int | None = None,
) -> dict[str, Any]:
    """Return the ``ReportRunReceipt`` body; raises rather than guessing a fact."""
    image_match = _IMAGE_DIGEST.search(image)
    if image_match is None:
        raise RunReceiptError("Digest-pinned image is required.")
    if not isinstance(sealed_manifest_digest, str) or not _DIGEST.fullmatch(sealed_manifest_digest):
        raise RunReceiptError("Sealed output manifest digest is required.")
    if not isinstance(control_manifest_digest, str) or not _DIGEST.fullmatch(
        control_manifest_digest
    ):
        raise RunReceiptError("Control output manifest digest is required.")
    if not facts.container_id or len(facts.container_id) > 200:
        raise RunReceiptError("Container identity is required.")
    if type(epoch) is not int or epoch < 1 or not lease_id:
        raise RunReceiptError("Lease identity is required.")
    if not -(2**31) <= facts.exit_code <= 2**31 - 1:
        raise RunReceiptError("Exit code is out of range.")
    if datetime.fromisoformat(facts.finished_at) < datetime.fromisoformat(facts.started_at):
        raise RunReceiptError("Container finished before it started.")
    argv_digest = command_digest(argv)
    payload: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "adapter_version": ADAPTER_VERSION,
        "operation_id": operation_id,
        "plan_digest": plan_digest,
        "container_id": facts.container_id,
        "image_digest": image_match.group(1),
        "argv_digest": argv_digest,
        "argc": len(argv),
        "network_profile": network_profile,
        "exit_code": facts.exit_code,
        "started_at": facts.started_at,
        "finished_at": facts.finished_at,
        "termination_confirmed": True,
        "sealed_manifest_digest": sealed_manifest_digest,
    }
    if facts.image_id is not None:
        payload["image_id"] = facts.image_id
    if scratch_bytes is not None:
        # ADR 0059: the scratch the container actually ran with (granted, not requested).
        payload["scratch_bytes"] = scratch_bytes
    encoded = canonical_json(payload)
    if len(encoded.encode()) > MAX_RECEIPT_PAYLOAD_BYTES:
        raise RunReceiptError("Run receipt payload exceeds its bound.")
    return {
        "lease_id": lease_id,
        "expected_epoch": epoch,
        "provider_id": facts.container_id,
        "command_digest": argv_digest,
        "exit_code": facts.exit_code,
        "started_at": facts.started_at,
        "finished_at": facts.finished_at,
        "output_digest": control_manifest_digest,
        "receipt_payload": payload,
        "receipt_digest": sha256_hex(encoded),
    }


__all__ = [
    "ADAPTER_VERSION",
    "RECEIPT_SCHEMA",
    "RunReceiptError",
    "TerminalFacts",
    "build_run_receipt",
    "canonical_json",
    "command_digest",
    "idempotency_key",
    "parse_docker_time",
    "terminal_facts",
]
