"""The egress allowlist of a stage and the check of what a provider reports it transferred.

Provider-neutral and pure (ADR 0134). The allowlist is derived from the approved spec only:
the pinned source hosts and redirect hosts of ``public_artifact`` inputs (ADR 0097), plus the
stage's registered egress destinations (ADR 0034). A provider enforces it at launch in its own
way (for Salad, the reviewed workload contract must carry exactly these hosts); the receipt
shows the list and checks every transfer the provider reports against it, so a transfer outside
it is recorded as refused instead of being dropped.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from .public_fetch_models import canonical_public_host, public_fetch_host

PUBLIC_PORT = 443


def stage_allowlist(spec_stage: Any) -> list[dict[str, Any]]:
    """Sorted ``{host, port, source}`` rows: ``pinned``, ``redirect`` or ``registered``."""
    found: dict[tuple[str, int], str] = {}
    for source in getattr(spec_stage, "inputs", ()):
        if getattr(source, "kind", None) != "public_artifact":
            continue
        for _name, url, _sha, _size in source.public_files:
            host = public_fetch_host(url)
            if host is not None:
                found[(host, PUBLIC_PORT)] = "pinned"
        for host in source.redirect_hosts:
            found.setdefault((host, PUBLIC_PORT), "redirect")
    policy = getattr(spec_stage, "egress", None)
    for item in getattr(policy, "destinations", ()) or ():
        found.setdefault((item.host, item.port), "registered")
    return [
        {"host": host, "port": port, "source": source}
        for (host, port), source in sorted(found.items())
    ]


def refused_transfers(
    allowlist: list[dict[str, Any]], pins: set[tuple[str, str]], rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Reported transfers (``url``, ``sha256``) outside the allowlist or not a pinned file.

    Only the host is returned, never the URL. ``pins`` is the set of pinned (url, sha256). The
    allowlist holds canonical hosts (one trailing dot stripped, lowercased, IDNA-encoded), so a
    reported host is canonicalised the same way before it is compared: the same name spelled
    with a trailing dot or in Unicode is the same host. A host that has no canonical form (an IP
    literal, an internal name) is never allowlisted and is reported as the parser read it."""
    allowed = {item["host"] for item in allowlist}
    refused: list[dict[str, Any]] = []
    for row in rows:
        url = row.get("url") if isinstance(row, dict) else None
        try:
            raw = urlsplit(url).hostname if isinstance(url, str) else None
        except ValueError:
            raw = None
        # A host with no canonical public form (an internal registered name, an IP literal)
        # compares as the parser read it, exactly as before.
        host = canonical_public_host(raw) or raw
        if host is None or host not in allowed:
            refused.append({"host": host, "reason": "not_allowlisted"})
        elif (url, row.get("sha256")) not in pins:
            refused.append({"host": host, "reason": "not_pinned"})
    return refused
