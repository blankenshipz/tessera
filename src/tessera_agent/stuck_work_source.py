"""Stuck-campaign findings as a work source for the standing agent loop.

The control plane's detector records findings (reason code, suggested next action, receipt
digest). This module only reads the open ones and offers them to the loop as candidate work items.
It takes no action on any campaign, calls no model and writes nothing: whatever the loop then does
with an item is bounded by its own policy (fixed model and tier, caps, kill switch) and recorded on
its own receipts.

A finding that was acknowledged or resolved is not offered. A finding that recurs carries a higher
occurrence count, so its item id is new work. Any error or malformed answer yields no items
(fail closed: the loop sees nothing rather than invented work), and only allowlisted fields are
copied from an answer.
"""

from __future__ import annotations

import re
from typing import Any

CODES = frozenset(
    {
        "campaign_blocked",
        "delegation_lapsed",
        "attempt_outcome_unknown",
        "stale_execution_lease",
        "runner_lease_stale",
        "no_progress",
    }
)
KIND = "campaign_stuck"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_ACTION = re.compile(r"[a-z0-9_]{1,100}\Z")
MAX_ITEMS = 100


class StuckWorkSource:
    """``control`` is any object with ``call(method, path, body=None, key=None) -> dict``."""

    def __init__(self, control: Any, project_id: str, limit: int = 50) -> None:
        if not isinstance(project_id, str) or not _SEGMENT.fullmatch(project_id):
            raise ValueError("project_id is not a plain identifier")
        if type(limit) is not int or not 1 <= limit <= MAX_ITEMS:
            raise ValueError(f"limit must be 1-{MAX_ITEMS}")
        self.control, self.project_id, self.limit = control, project_id, limit

    def observe(self) -> list[dict[str, Any]]:
        """The open findings as loop candidates; [] on any failure."""
        path = f"/projects/{self.project_id}/campaign-stuck?status=open&limit={self.limit}"
        try:
            answer = self.control.call("GET", path)
        except Exception:  # noqa: BLE001 - sanitized: no work rather than a crash
            return []
        findings = answer.get("findings") if isinstance(answer, dict) else None
        if not isinstance(findings, list):
            return []
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for finding in findings[: self.limit]:
            item = self._item(finding)
            if item is not None and item["id"] not in seen:
                seen.add(item["id"])
                items.append(item)
        return items

    __call__ = observe

    @staticmethod
    def _item(finding: Any) -> dict[str, Any] | None:
        if not isinstance(finding, dict) or finding.get("status") != "open":
            return None
        ident, code = finding.get("id"), finding.get("code")
        occurrences = finding.get("occurrences")
        campaign, action = finding.get("campaign_id"), finding.get("suggested_action")
        digest, stage = finding.get("receipt_sha256"), finding.get("stage_key")
        if (
            not isinstance(ident, str)
            or not _ID.fullmatch(ident)
            or code not in CODES
            or type(occurrences) is not int
            or occurrences < 1
            or not isinstance(campaign, str)
            or not _SEGMENT.fullmatch(campaign)
            or not isinstance(action, str)
            or not _ACTION.fullmatch(action)
            or not isinstance(digest, str)
            or not _DIGEST.fullmatch(digest)
            or not (stage is None or (isinstance(stage, str) and _SEGMENT.fullmatch(stage)))
        ):
            return None
        return {
            "id": f"{ident}:{occurrences}",
            "kind": KIND,
            "code": code,
            "suggested_action": action,
            "campaign_id": campaign,
            "stage_key": stage,
            "receipt_sha256": digest,
        }
