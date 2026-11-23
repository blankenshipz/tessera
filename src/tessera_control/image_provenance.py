"""Runtime image provenance recorded on a stage attempt's receipt.

When a stage is dispatched the platform records which image the attempt was granted: the image
reference, its immutable digest, the base images when build metadata names them, and the total
compressed layer size when the provider adapter can read the registry manifest. The record is a
closed set of fields; a size that cannot be read is ``null`` with one reason from a closed list,
never free text. Nothing here reads a network, a credential or a registry itself: the manifest
read is an optional provider hook (``image_pull_size_bytes``), waited on for at most
``SIZE_READ_TIMEOUT_SECONDS``, and its absence, failure or timeout is recorded as a reason.
"""

from __future__ import annotations

import re
import threading
from typing import Any

IMAGE_PROVENANCE_EVENT = "campaign.image_provenance"

SIZE_UNAVAILABLE = "size_unavailable"
ADAPTER_CANNOT_READ_MANIFEST = "adapter_cannot_read_manifest"
SIZE_REASONS = (SIZE_UNAVAILABLE, ADAPTER_CANNOT_READ_MANIFEST)

# The receipt section's closed field set.
FIELDS = (
    "image_ref",
    "image_digest",
    "base_images",
    "compressed_size_bytes",
    "size_reason",
)

# Upper bound on one image size read (the provider's ``image_pull_size_bytes`` hook). A read
# still running then is abandoned and recorded as ``size_unavailable``; it never stalls the tick.
SIZE_READ_TIMEOUT_SECONDS = 10.0
_MAX_SIZE = 1 << 42  # 4 TiB: anything larger is not a believable image size
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:-]{0,254}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _name(value: Any) -> str | None:
    """A well-formed image name (optionally ``:tag``), or ``None``.

    Never accepts ``@`` (user information or a digest), a scheme, whitespace, or a registry part
    whose ``:`` is followed by anything but a port number, so ``user:pw/img`` is refused too."""
    if not isinstance(value, str) or "@" in value or "//" in value or not _REF.match(value):
        return None
    parts = value.split("/")
    host, rest, last = parts[0], parts[1:-1], parts[-1]
    if len(parts) > 1 and ":" in host:
        _, port = host.split(":", 1)
        if not port.isdigit():
            return None
    if any(":" in part for part in rest) or last.count(":") > 1:
        return None
    return value


def _digest(value: Any) -> str | None:
    return value if isinstance(value, str) and _DIGEST.match(value) else None


def split_ref(value: Any) -> tuple[str | None, str | None]:
    """``(name, digest)`` of an image reference; a part that is not well formed is ``None``.

    A reference with ``@`` must have exactly one, followed by a well-formed digest; otherwise the
    whole reference is refused, so user information (``user:pw@host/img``) never reaches a
    receipt. The name never carries a scheme, user information or whitespace."""
    if not isinstance(value, str):
        return None, None
    if "@" not in value:
        return _name(value), None
    name, _, digest = value.partition("@")
    if _digest(digest) is None:
        return None, None
    return _name(name), digest


def _base_images(spec_stage: Any) -> list[dict[str, str | None]]:
    """Base images the approved image-build plan pins, or none for any other stage."""
    plan = getattr(spec_stage, "build", None)
    if not isinstance(plan, dict):
        return []
    rows: list[dict[str, str | None]] = []
    for item in plan.get("base_images") or []:
        name, digest = split_ref(item)
        if name is not None and digest is not None:
            rows.append({"ref": name, "digest": digest})
    return rows


def _bounded_read(reader: Any, spec_stage: Any, timeout: float) -> Any:
    """``reader(spec_stage)`` on a daemon thread, waited on for at most ``timeout`` seconds.

    A read that has not returned by then is abandoned (``TimeoutError``): the runner tick goes on
    and the late result is discarded. Python cannot kill the thread, so the reader should still
    bound its own network calls."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = reader(spec_stage)
        except Exception as exc:  # noqa: BLE001 - re-raised on the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=run, name="image-pull-size-read", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError("image size read exceeded its time bound")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _size(provider: Any, spec_stage: Any) -> tuple[int | None, str | None]:
    reader = getattr(provider, "image_pull_size_bytes", None)
    if not callable(reader):
        return None, ADAPTER_CANNOT_READ_MANIFEST
    try:
        size = _bounded_read(reader, spec_stage, SIZE_READ_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - a manifest read never blocks dispatch or leaks its error
        return None, SIZE_UNAVAILABLE
    if type(size) is int and 0 < size <= _MAX_SIZE:
        return size, None
    return None, SIZE_UNAVAILABLE


def image_provenance(spec_stage: Any, provider: Any) -> dict[str, Any]:
    """The closed provenance record for the stage's image as dispatched."""
    name, digest = split_ref(getattr(spec_stage, "image", None))
    size, reason = _size(provider, spec_stage)
    return {
        "image_ref": name,
        "image_digest": digest,
        "base_images": _base_images(spec_stage),
        "compressed_size_bytes": size,
        "size_reason": reason,
    }


def closed(payload: dict[str, Any]) -> dict[str, Any]:
    """Only the closed fields of a recorded event, re-validated; anything else is dropped."""
    # Name and digest are re-validated separately: rebuilding ``name@digest`` would turn a
    # missing name into the string "None".
    name, digest = _name(payload.get("image_ref")), _digest(payload.get("image_digest"))
    bases = []
    for item in payload.get("base_images") or []:
        if isinstance(item, dict):
            ref, dig = _name(item.get("ref")), _digest(item.get("digest"))
            if ref is not None and dig is not None:
                bases.append({"ref": ref, "digest": dig})
    size = payload.get("compressed_size_bytes")
    if not (type(size) is int and 0 < size <= _MAX_SIZE):
        size = None
    reason = payload.get("size_reason")
    if size is None and reason not in SIZE_REASONS:
        reason = SIZE_UNAVAILABLE
    return {
        "image_ref": name,
        "image_digest": digest,
        "base_images": bases,
        "compressed_size_bytes": size,
        "size_reason": None if size is not None else reason,
    }
