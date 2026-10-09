"""Closed HTTP readback of separately admitted curated input/capture policy."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .agent_provenance_harness_models import HarnessDigest, HarnessID
from .public_capture import CaptureBinding
from .public_capture_contexts import SeparatedCaptureResolution


class ResolveCaptureContexts(CaptureBinding):
    input_context_id: HarnessID
    input_body_digest: HarnessDigest
    capture_policy_context_id: HarnessID
    destination_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class CaptureContextsReply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    verified_binding: ResolveCaptureContexts
    resolution: SeparatedCaptureResolution
    verification_scope: Literal["current_issuer_context_policy_and_input_digest"]
    authority_effect: Literal["none"]
