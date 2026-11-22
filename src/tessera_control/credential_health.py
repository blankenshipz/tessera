"""Expiry health of provider and service credentials: metadata only, never a value.

A fixed-lifetime credential that silently expires makes a campaign fail deep in a run. This module
turns a credential's *metadata* (version, expiry, revocation) into a state an operator, the health
route and campaign admission can act on up front. Nothing here reads, returns or logs a secret.

A provider module may define ``credential_health(now) -> list[dict]`` (vendor-neutral, optional);
``collect`` gathers them. ``evaluate`` is the one place that decides a state.
"""

from __future__ import annotations

import time
from typing import Any

from .campaign_providers import get_provider, provider_ids

WARN_SECONDS = 24 * 3600
STATES = ("ok", "warning", "expired", "revoked", "version_mismatch", "unknown")
# States that make a stage's provider credential unusable now. ``unknown`` (no credential store
# configured or readable by this service) and ``warning`` never block: the guardian that resolves
# the credential at run time stays the authority, this is an additional early check.
BLOCKING_STATES = frozenset({"expired", "revoked", "version_mismatch"})
_RANK = {"ok": 0, "unknown": 1, "warning": 2, "version_mismatch": 3, "expired": 4, "revoked": 4}


def evaluate(
    metadata: dict[str, Any] | None,
    now: float,
    warn_seconds: int = WARN_SECONDS,
    pinned_version: int | None = None,
) -> dict[str, Any]:
    """The state of one credential from its metadata header (never its value)."""
    if not isinstance(metadata, dict):
        return _entry(
            "unknown", None, None, None, None, pinned_version, "credential store not read"
        )
    version, expires_at = metadata.get("version"), metadata.get("expires_at")
    revoked = metadata.get("revoked")
    remaining = None if not isinstance(expires_at, int | float) else int(expires_at - now)
    if revoked is True:
        state, reason = "revoked", "the credential was revoked"
    elif remaining is None:
        state, reason = "unknown", "no expiry recorded"
    elif remaining <= 0:
        state, reason = "expired", f"expired {-remaining} seconds ago"
    elif pinned_version is not None and version != pinned_version:
        state, reason = (
            "version_mismatch",
            f"the profile pins version {pinned_version}, the store holds version {version}",
        )
    elif remaining <= warn_seconds:
        state, reason = "warning", f"expires in {remaining} seconds"
    else:
        state, reason = "ok", f"expires in {remaining} seconds"
    return _entry(state, remaining, expires_at, revoked, version, pinned_version, reason)


def _entry(state, remaining, expires_at, revoked, version, pinned_version, reason):
    return {
        "state": state,
        "seconds_remaining": remaining,
        "expires_at": expires_at,
        "revoked": revoked,
        "version": version,
        "pinned_version": pinned_version,
        "reason": reason,
    }


def collect(now: float | None = None) -> list[dict[str, Any]]:
    """Every credential entry the registered providers report (no hook, no entries)."""
    when = time.time() if now is None else now
    found: list[dict[str, Any]] = []
    for provider_id in provider_ids():
        hook = getattr(get_provider(provider_id), "credential_health", None)
        if callable(hook):
            try:
                found.extend(hook(when))
            except Exception:  # noqa: BLE001 - a provider's failure never hides the others
                found.append(
                    {
                        "provider_id": provider_id,
                        **_entry("unknown", None, None, None, None, None, "provider check failed"),
                    }
                )
    return found


def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Worst state, counts and the (metadata only) items, for the health projection."""
    worst = max((i["state"] for i in items), key=lambda s: _RANK.get(s, 0), default="ok")
    return {
        "worst_state": worst,
        "warning_count": sum(1 for i in items if i["state"] == "warning"),
        "expired_count": sum(1 for i in items if i["state"] in BLOCKING_STATES),
        "items": items,
    }
