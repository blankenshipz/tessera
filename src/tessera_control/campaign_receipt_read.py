"""Read a stored campaign receipt only after its digest verifies.

A campaign receipt is written once, with ``receipt_digest = canonical_digest(receipt)`` over the
receipt object (the digest is not part of its own input). Every reader of ``campaign_receipts``
loads the row through :func:`load_verified_receipt`, which recomputes that digest over the
stored ``receipt_json`` and refuses on any mismatch: no partial data, and an error that carries
a stable code and nothing from the stored body.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from .store import Conflict, canonical_digest

RECEIPT_DIGEST_MISMATCH = "receipt_digest_mismatch"


class ReceiptDigestMismatch(Conflict):
    """The stored receipt does not match its recorded digest (409, stable ``code``)."""

    code = RECEIPT_DIGEST_MISMATCH

    def __init__(self) -> None:
        super().__init__(
            f"Campaign receipt failed digest verification ({RECEIPT_DIGEST_MISMATCH})."
        )


@dataclass(frozen=True)
class VerifiedReceipt:
    receipt: dict[str, Any]
    digest: str
    created_at: str


def verify_receipt_row(receipt_json: Any, receipt_digest: Any) -> dict[str, Any]:
    """The parsed receipt when ``canonical_digest`` of it equals the recorded digest."""
    if not isinstance(receipt_json, str) or not isinstance(receipt_digest, str):
        raise ReceiptDigestMismatch()
    try:
        receipt = json.loads(receipt_json)
    except (ValueError, TypeError, RecursionError):
        raise ReceiptDigestMismatch() from None
    if not isinstance(receipt, dict):
        raise ReceiptDigestMismatch()
    try:
        recomputed = canonical_digest(receipt)
    except (ValueError, TypeError, RecursionError):
        raise ReceiptDigestMismatch() from None
    if recomputed != receipt_digest:
        raise ReceiptDigestMismatch()
    return receipt


def load_verified_receipt(
    connection: sqlite3.Connection, campaign_id: str
) -> VerifiedReceipt | None:
    """The campaign's terminal receipt, verified, or None when it has none.

    The caller has already checked that ``campaign_id`` is a campaign of the caller's org and
    project; this reads only the receipt row. Raises :class:`ReceiptDigestMismatch` when the
    stored body does not hash to the stored digest.
    """
    row = connection.execute(
        "SELECT receipt_json, receipt_digest, created_at FROM campaign_receipts "
        "WHERE campaign_id=?",
        (campaign_id,),
    ).fetchone()
    if row is None:
        return None
    receipt = verify_receipt_row(row["receipt_json"], row["receipt_digest"])
    return VerifiedReceipt(receipt, row["receipt_digest"], row["created_at"])
