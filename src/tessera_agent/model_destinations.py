"""Operator-attested model destinations for Tessera's own reasoning runtime (ADR 0082).

This is the single source of truth for which model providers the reasoning
plane may admit, under which account mode, with which credential-reference
shape, and which hosts the model-runtime component may reach. It reads no
credentials, opens no connections, and never widens the general sandbox or
campaign-stage egress allowlist.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

OPENAI_ACCOUNT_MODE = "included-chatgpt-oauth"
# Default: Tessera calls the Anthropic Messages API directly with an API key held
# only as a manager-delivered credential file (systemd LoadCredential). The key is
# never placed in ANTHROPIC_API_KEY or any process environment, and no Claude
# subscription login is used.
ANTHROPIC_ACCOUNT_MODE = "anthropic-api-key-ref"

# Model runtimes. OpenCode V2 is the operator-pinned harness for OpenAI included
# OAuth. The Anthropic destination is called directly by Tessera's own
# Messages API client, whose transport enforces the model-runtime allowlist.
RUNTIME_OPENCODE = "opencode-v2"
RUNTIME_ANTHROPIC_MESSAGES = "anthropic-messages-api"

# Opaque references only; never bearer values. OWNER and REFERENCE are
# operator-chosen labels, not account names or tokens.
_REF_PART = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
ANTHROPIC_CREDENTIAL_REF = re.compile(rf"anthropic-api-key://{_REF_PART}/{_REF_PART}\Z")

_MODEL_ID = r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}"

# Environment names that would signal an API-key fallback. A reasoning process
# refuses to start when any of them is present (ADR 0082 §3).
FORBIDDEN_ENVIRONMENT = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY")

# Policy tiers recorded on receipts. Fable needs a per-job human approval;
# nothing in Tessera ever picks a cheaper tier on the caller's behalf.
TIER_DEFAULT = ("sonnet", "opus")
TIER_CHEAP_CHECK = "haiku"
TIER_APPROVAL_REQUIRED = "fable"
TIER_UNCLASSIFIED = "unclassified"


class DestinationRefused(ValueError):
    """Sanitized refusal; never echoes supplied credential material."""


@dataclass(frozen=True)
class ModelDestination:
    provider_id: str
    account_mode: str
    # Hosts the model-runtime component may open for this destination. Empty
    # means Tessera adds nothing (the existing OpenAI route is unchanged).
    model_runtime_hosts: tuple[tuple[str, int], ...]
    requires_credential_ref: bool
    # Billing-route evidence contract (ADR 0082 §9; for OpenAI, the included-OAuth
    # route): the billing route a trusted guard must measure for this destination,
    # and the local-check names v2 runtime-admission evidence must report as passed.
    billing_route: str
    provider_only_check: str
    endpoint_check: str
    runtime: str = RUNTIME_OPENCODE
    credential_pattern: re.Pattern | None = None
    # True when the vendor bills per call (cost must fit per-call, daily and grant
    # caps); False for included usage (any billed cost halts the run).
    billed: bool = False

    def guard_policy(self) -> dict:
        """Exact account policy a trusted guard must measure; unknown/exhausted stop."""
        return {
            "allowed_auth_methods": ["oauth"],
            "billing_routes": [self.billing_route],
            "on_unknown": "deny",
            "on_exhausted": "stop",
        }


DESTINATIONS: dict[str, ModelDestination] = {
    "openai": ModelDestination(
        "openai",
        OPENAI_ACCOUNT_MODE,
        (),
        False,
        billing_route="chatgpt-included",
        provider_only_check="openai_only",
        endpoint_check="chatgpt_endpoint",
    ),
    "anthropic": ModelDestination(
        "anthropic",
        ANTHROPIC_ACCOUNT_MODE,
        (("api.anthropic.com", 443),),
        True,
        billing_route="anthropic-api-key",
        provider_only_check="anthropic_only",
        endpoint_check="anthropic_endpoint",
        runtime=RUNTIME_ANTHROPIC_MESSAGES,
        credential_pattern=ANTHROPIC_CREDENTIAL_REF,
        billed=True,
    ),
}

# The default destination of the on-platform reasoning loop (owner direction,
# 2026-11-22). OpenAI included OAuth stays an optional alternative.
DEFAULT_PROVIDER = "anthropic"

ACCOUNT_MODES = {d.account_mode: d for d in DESTINATIONS.values()}

# Qualified model identifiers accepted by harness binding, observer and the
# research-service config: ``openai/<id>`` or ``anthropic/<id>``.
QUALIFIED_MODEL = re.compile(rf"(?:openai|anthropic)/{_MODEL_ID}\Z")
MODEL_ID = re.compile(rf"{_MODEL_ID}\Z")


def destination(provider_id: object, account_mode: object) -> ModelDestination:
    """Return the enrolled destination for a provider/account-mode pair or refuse."""
    found = ACCOUNT_MODES.get(account_mode) if isinstance(account_mode, str) else None
    if found is None or provider_id != found.provider_id:
        raise DestinationRefused("unenrolled_model_destination")
    return found


def require_runtime(found: ModelDestination, runtime: str) -> ModelDestination:
    """Refuse a destination on a runtime it is not enrolled for (e.g. Anthropic on OpenCode)."""
    if found.runtime != runtime:
        raise DestinationRefused("destination_requires_other_runtime")
    return found


def destination_for_account_mode(account_mode: object) -> ModelDestination:
    found = ACCOUNT_MODES.get(account_mode) if isinstance(account_mode, str) else None
    if found is None:
        raise DestinationRefused("unenrolled_account_mode")
    return found


def split_model(qualified: object) -> tuple[str, str]:
    """Split ``provider/id``; refuse anything outside the enrolled providers."""
    if not isinstance(qualified, str) or not QUALIFIED_MODEL.fullmatch(qualified):
        raise DestinationRefused("unenrolled_model_identifier")
    provider, model_id = qualified.split("/", 1)
    return provider, model_id


def validate_credential_ref(found: ModelDestination, reference: object) -> str | None:
    """Anthropic needs an opaque API-key credential reference; OpenAI keeps its route."""
    if not found.requires_credential_ref:
        if reference is not None:
            raise DestinationRefused("unexpected_credential_reference")
        return None
    pattern = found.credential_pattern or ANTHROPIC_CREDENTIAL_REF
    if not isinstance(reference, str) or not pattern.fullmatch(reference):
        raise DestinationRefused("invalid_credential_reference")
    return reference


def refuse_api_key_environment(environment: dict[str, str]) -> None:
    """Fail closed if an API-key fallback is present; never reads its value."""
    if any(name in environment for name in FORBIDDEN_ENVIRONMENT):
        raise DestinationRefused("api_key_environment_present")


def model_tier(provider_id: str, model_id: str) -> str:
    """Policy tier recorded on receipts. Classification only; never substitution."""
    if provider_id != "anthropic":
        return TIER_UNCLASSIFIED
    lowered = model_id.lower()
    for tier in (TIER_APPROVAL_REQUIRED, TIER_CHEAP_CHECK, *TIER_DEFAULT):
        if tier in lowered:
            return tier
    return TIER_UNCLASSIFIED


def require_tier_admitted(tier: str, fable_approval_id: object = None) -> None:
    """Fable needs an explicit per-job approval id; unclassified Anthropic ids refuse."""
    if tier == TIER_APPROVAL_REQUIRED and not (
        isinstance(fable_approval_id, str) and MODEL_ID.fullmatch(fable_approval_id)
    ):
        raise DestinationRefused("tier_requires_job_approval")


def model_runtime_egress_allowed(provider_id: str, host: str, port: int) -> bool:
    """The model-runtime allowlist: only the enrolled destination's own API host.

    This list is never merged into the sandbox or campaign-stage allowlist.
    """
    found = DESTINATIONS.get(provider_id)
    return found is not None and (host.lower().rstrip("."), port) in found.model_runtime_hosts


def model_runtime_allowlist() -> frozenset[tuple[str, int]]:
    return frozenset(h for d in DESTINATIONS.values() for h in d.model_runtime_hosts)
