"""Shared records for the storage retention policy (backups and unreferenced artifacts).

The policy only ever *selects* things to delete; every selection is listed first (dry run),
and every deletion lands on a receipt with the size and digest of what was removed.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

POLICY_FILE = "storage-retention.json"  # operator-owned, inside the state directory
RECEIPT_DIR = "storage-retention"  # private receipts, one JSON file per run
MIN_KEEP = 1  # the newest verified backup is never selected, whatever the policy says


class RetentionPolicy(BaseModel):
    """Operator policy. Every knob is optional; with none set nothing is ever selected."""

    model_config = ConfigDict(extra="forbid", strict=True)

    backup_root: str | None = None  # directory holding one `tessera backup` bundle per child
    backup_keep_last: int | None = Field(default=None, ge=MIN_KEEP, le=10_000)
    backup_max_age_days: int | None = Field(default=None, ge=1, le=36_500)
    # Free space (bytes) on the backup_root filesystem below which the oldest backups beyond
    # the keep-last floor are selected, oldest first, until the threshold is met.
    disk_free_min_bytes: int | None = Field(default=None, ge=1)
    artifact_max_age_days: int | None = Field(default=None, ge=1, le=36_500)
    # Opt in: free the chunk bytes of large artifacts (for example weights archives) that no
    # manifest lists, whose lease is over and that are older than the operator's orphan age,
    # or whose every manifest was rejected. The quarantine row stays as evidence.
    reclaim_orphaned_large_artifacts: bool = False
    # Opt in: age out the repository's ``.pytest-tmp`` (one basetemp per test run). Each direct
    # child quiet for ``test_scratch_max_age_hours`` is selected; one changed within the last hour
    # is always kept. ``test_scratch_root`` defaults to ``<repository>/.pytest-tmp`` and must be a
    # directory literally named ``.pytest-tmp``; nothing outside it is ever classified.
    reclaim_test_scratch: bool = False
    test_scratch_max_age_hours: int = Field(default=24, ge=1, le=8760)
    test_scratch_root: str | None = None
    # The timer only reports (dry run) unless this is true; a person can always apply by hand.
    auto_apply: bool = False
    # How often the timer fully re-hashes every backup (restore's own validation) and receipts
    # which ones would no longer restore, including ones a plan never reads (young bundles).
    backup_verify_interval_hours: int = Field(default=24, ge=1, le=8760)
    timer_interval_seconds: int = Field(default=3600, ge=60, le=7 * 86_400)


@dataclass(frozen=True)
class Candidate:
    """One thing the policy would delete (or must keep), with why."""

    kind: Literal["backup", "artifact", "large_artifact", "test_scratch"]
    ref: str  # backup id, or artifact id
    path: str  # backup directory (relative to backup_root) or object key; no absolute paths
    size_bytes: int
    sha256: str  # backup: manifest snapshot digest; artifact: object digest
    created_at: str  # ISO-8601 UTC
    decision: Literal["delete", "keep"]
    reasons: list[str] = field(
        default_factory=list
    )  # e.g. ["max_age"], ["keep_last"], ["referenced"]
    project_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Plan:
    generated_at: str
    policy_digest: str
    candidates: list[Candidate]
    skipped: list[dict[str, str]] = field(default_factory=list)  # unreadable/unverified items

    def deletions(self) -> list[Candidate]:
        return [c for c in self.candidates if c.decision == "delete"]

    def digest(self) -> str:
        """Stable digest of exactly what would be deleted; apply must present it."""
        import hashlib

        body = json.dumps(
            [(c.kind, c.ref, c.size_bytes, c.sha256) for c in self.deletions()],
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(body.encode()).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "policy_digest": self.policy_digest,
            "plan_digest": self.digest(),
            "candidates": [c.as_dict() for c in self.candidates],
            "skipped": self.skipped,
        }


def receipt_dir(state_dir: Path) -> Path:
    return state_dir / RECEIPT_DIR
