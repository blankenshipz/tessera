"""Image pull-size preflight for a stage's provider.

A provider on interruptible capacity can lose a node while a very large image is still being
pulled, and the whole attempt is lost with it. The operator may state a ceiling on the compressed
pull size per provider (``max_image_pull_bytes``) and may make it a refusal (``strict_image_pull``).

* No ceiling (``None``, the default for worker-grant providers): nothing is evaluated or recorded.
* A ceiling and a known size over it: a warning section with the closed reason
  ``image_pull_over_limit`` (placement refuses instead when the provider is strict, with the
  permanent reason ``image_pull_refused``).
* A ceiling and no known size: the section says ``unknown`` with the closed reason
  ``image_size_unknown``; the stage is never refused or warned about for a size nobody recorded.

The size comes from a provider's optional ``image_pull_bytes(stage)`` hook, else from the size
resolvers registered here (a provider-neutral hook for whatever records the compressed size of an
image), else it is unknown. Nothing here contacts a registry. The recorded section is named
``image_pull_preflight`` and holds only integers, booleans and closed codes.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any

EVENT = "campaign.image_pull_preflight"
SECTION = "image_pull_preflight"
OVER_LIMIT = "image_pull_over_limit"
SIZE_UNKNOWN = "image_size_unknown"
REFUSED_REASON = "image_pull_refused"

# The default ceiling of an interruptible (rented, reschedulable) provider: 4 GiB compressed.
DEFAULT_INTERRUPTIBLE_MAX_IMAGE_PULL_BYTES = 4 * 1024**3
MAX_IMAGE_PULL_BYTES = 1 << 50

SizeResolver = Callable[[Any], "int | None"]
_RESOLVERS: list[SizeResolver] = []


def valid_limit(value: Any) -> bool:
    """True for None (no ceiling) or a positive integer (never a bool) up to the maximum."""
    return value is None or (type(value) is int and 0 < value <= MAX_IMAGE_PULL_BYTES)


def validate_limits(max_image_pull_bytes: Any, strict_image_pull: Any, where: str) -> None:
    """Refuse a malformed pair (ValueError)."""
    if not valid_limit(max_image_pull_bytes):
        raise ValueError(
            f"{where}: max_image_pull_bytes must be null or an integer in 1..{MAX_IMAGE_PULL_BYTES}"
        )
    if not isinstance(strict_image_pull, bool):
        raise ValueError(f"{where}: strict_image_pull must be a boolean")
    if strict_image_pull and max_image_pull_bytes is None:
        raise ValueError(f"{where}: strict_image_pull needs max_image_pull_bytes")


def register_size_resolver(resolver: SizeResolver) -> None:
    """Add a provider-neutral source of a stage image's compressed pull size (bytes or None)."""
    if resolver not in _RESOLVERS:
        _RESOLVERS.append(resolver)


def unregister_size_resolver(resolver: SizeResolver) -> None:
    if resolver in _RESOLVERS:
        _RESOLVERS.remove(resolver)


def limits_of(provider: Any, stage: Any) -> tuple[int | None, bool]:
    """The provider's ceiling and strictness for this stage: its own ``image_pull_limits(stage)``
    hook, else its descriptor's fields. A malformed report counts as no ceiling."""
    hook = getattr(provider, "image_pull_limits", None)
    try:
        if callable(hook):
            limit, strict = hook(stage)
        else:
            descriptor = provider.descriptor
            limit = getattr(descriptor, "max_image_pull_bytes", None)
            strict = getattr(descriptor, "strict_image_pull", False)
    except Exception:  # noqa: BLE001 - an unreadable stage config is not a ceiling
        return None, False
    if not valid_limit(limit):
        return None, False
    return limit, bool(strict) and limit is not None


def size_of(provider: Any, stage: Any) -> int | None:
    """The stage image's compressed pull size in bytes when something recorded it, else None."""
    sources = [getattr(provider, "image_pull_bytes", None), *_RESOLVERS]
    for source in sources:
        if not callable(source):
            continue
        try:
            found = source(stage)
        except Exception:  # noqa: BLE001 - a failing source means unknown
            continue
        if type(found) is int and found >= 0:
            return found
    return None


def evaluate(provider: Any, stage: Any) -> dict[str, Any] | None:
    """The ``image_pull_preflight`` section for a stage on a provider, or None when the provider
    has no ceiling. ``status`` is ``within_limit``, ``over_limit``, ``refused`` or ``unknown``."""
    limit, strict = limits_of(provider, stage)
    if limit is None:
        return None
    size = size_of(provider, stage)
    base = {"limit_bytes": limit, "strict": strict}
    if size is None:
        return {"status": "unknown", "reason": SIZE_UNKNOWN, "size_bytes": None, **base}
    if size > limit:
        return {
            "status": "refused" if strict else "over_limit",
            "reason": OVER_LIMIT,
            "size_bytes": size,
            **base,
        }
    return {"status": "within_limit", "reason": None, "size_bytes": size, **base}


def plan(stage: Any) -> list[dict[str, Any]]:
    """The pre-dispatch readout: one section (with its ``provider_id``) per registered candidate
    provider of the stage that has a ceiling, in candidate order. Reads only local hooks and
    records nothing; an unregistered candidate is left out (placement skips it too)."""
    from .campaign_models import stage_on_candidate
    from .campaign_providers import get_provider

    out = []
    for provider_id in stage.candidate_ids:
        try:
            section = evaluate(get_provider(provider_id), stage_on_candidate(stage, provider_id))
        except KeyError:
            continue
        if section is not None:
            out.append({"provider_id": provider_id, **section})
    return out


def refusal_detail(section: Mapping[str, Any] | None) -> str | None:
    """Placement's skip detail when a strict provider's image is over its ceiling, else None."""
    if section is None or section.get("status") != "refused":
        return None
    return f"{section['size_bytes']} > {section['limit_bytes']}"


def event_payload(
    stage_key: str, attempt: int, provider_id: str, section: Mapping[str, Any]
) -> dict[str, Any]:
    return {"stage_key": stage_key, "attempt": attempt, "provider_id": provider_id, **section}


def recorded(connection: sqlite3.Connection, campaign: Mapping[str, Any]) -> dict:
    """Every recorded section of a campaign by (stage_key, attempt), for receipts."""
    rows = connection.execute(
        """SELECT payload_json FROM events WHERE project_id=? AND event_type=?
        AND json_extract(payload_json,'$.campaign_id')=? ORDER BY cursor""",
        (campaign["project_id"], EVENT, campaign["id"]),
    ).fetchall()
    out = {}
    for row in rows:
        payload = json.loads(row["payload_json"])
        out[(payload.get("stage_key"), payload.get("attempt"))] = {
            key: payload.get(key)
            for key in ("provider_id", "status", "reason", "size_bytes", "limit_bytes", "strict")
        }
    return out


def latest_by_stage(connection: sqlite3.Connection, campaign: Mapping[str, Any]) -> dict:
    """The newest recorded section per stage key (the campaign status view)."""
    latest: dict[str, tuple[int, dict]] = {}
    for (stage_key, attempt), value in recorded(connection, campaign).items():
        if stage_key not in latest or (attempt or 0) >= latest[stage_key][0]:
            latest[stage_key] = (attempt or 0, value)
    return {key: value for key, (_, value) in latest.items()}
