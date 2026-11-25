"""Read-only summary of the storage retention policy and its recent receipts.

Returns a small closed dict: no paths, no refs, no secrets. Malformed receipts are skipped and
counted, never raised, so a damaged file cannot hide the rest of the status.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .storage_retention_models import POLICY_FILE, RetentionPolicy, receipt_dir

RECEIPT_WINDOW = 30
_MAX_BYTES = 16 * 1024 * 1024


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _policy(state_dir: Path) -> RetentionPolicy | None:
    try:
        raw = (state_dir / POLICY_FILE).read_bytes()
        return RetentionPolicy.model_validate(json.loads(raw))
    except (OSError, ValueError):
        return None


def _summary(receipt: dict[str, Any]) -> dict[str, Any]:
    plan = receipt.get("plan")
    candidates = plan.get("candidates", []) if isinstance(plan, dict) else []
    planned = [c for c in candidates if isinstance(c, dict) and c.get("decision") == "delete"]
    deleted = receipt.get("deleted")
    failed = receipt.get("failed")
    return {
        "run_id": str(receipt["run_id"]),
        "finished_at": receipt.get("finished_at"),
        "mode": receipt["mode"],
        "trigger": receipt.get("trigger"),
        "deletions_planned": len(planned),
        "deleted_count": len(deleted) if isinstance(deleted, list) else 0,
        "bytes_freed": _int(receipt.get("bytes_freed")),
        "failed_count": len(failed) if isinstance(failed, list) else 0,
        "_reclaimable": sum(_int(c.get("size_bytes")) for c in planned),
        "_plan_digest": receipt.get("plan_digest"),
    }


def _read(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > _MAX_BYTES:
            return None
        receipt = json.loads(path.read_bytes())
        if not isinstance(receipt, dict) or receipt.get("mode") not in {
            "dry_run",
            "apply",
            "verify",
        }:
            return None
        if receipt["mode"] == "verify":
            unrestorable = receipt.get("unrestorable")
            return {
                "mode": "verify",
                "finished_at": receipt.get("finished_at"),
                "status": str(receipt.get("status"))[:20],
                "restorable": _int(receipt.get("restorable")),
                "unrestorable_count": len(unrestorable) if isinstance(unrestorable, list) else 0,
            }
        summary = _summary(receipt)
        plan = receipt.get("plan")
        skipped = plan.get("skipped") if isinstance(plan, dict) else None
        summary["_skipped"] = len(skipped) if isinstance(skipped, list) else 0
        return summary
    except (OSError, ValueError, KeyError, TypeError):
        return None


def retention_status(state_dir: Path) -> dict[str, Any]:
    policy = _policy(state_dir)
    try:
        names = sorted(
            (p for p in receipt_dir(state_dir).glob("*.json") if p.is_file()),
            key=lambda p: p.name,
            reverse=True,
        )[:RECEIPT_WINDOW]
    except OSError:
        names = []
    runs: list[dict[str, Any]] = []  # newest first
    unreadable = 0
    for path in names:
        summary = _read(path)
        if summary is None:
            unreadable += 1
        else:
            runs.append(summary)
    sweeps = [r for r in runs if r["mode"] == "verify"]
    runs = [r for r in runs if r["mode"] != "verify"]
    backups = {
        "last_verified_at": sweeps[0]["finished_at"] if sweeps else None,
        "restorable": sweeps[0]["restorable"] if sweeps else None,
        "unrestorable": sweeps[0]["unrestorable_count"] if sweeps else None,
        "last_sweep_status": sweeps[0]["status"] if sweeps else None,
        "skipped_in_last_plan": runs[0]["_skipped"] if runs else None,
    }
    pending = None
    for run in runs:  # the newest dry run counts only if no apply is newer
        if run["mode"] == "apply":
            break
        pending = {
            "plan_digest": run["_plan_digest"],
            "deletions_planned": run["deletions_planned"],
            "bytes_reclaimable": run["_reclaimable"],
        }
        break
    last_run = {k: v for k, v in runs[0].items() if not k.startswith("_")} if runs else None
    return {
        "configured": policy is not None,
        "auto_apply": policy.auto_apply if policy else False,
        "timer_interval_seconds": policy.timer_interval_seconds if policy else None,
        "last_run": last_run,
        "backups": backups,
        "test_scratch": {
            "enabled": bool(policy and policy.reclaim_test_scratch),
            "max_age_hours": policy.test_scratch_max_age_hours if policy else None,
        },
        "pending_dry_run": pending,
        "totals": {
            "receipts": len(runs),
            "apply_runs": sum(r["mode"] == "apply" for r in runs),
            "dry_runs": sum(r["mode"] == "dry_run" for r in runs),
            "deleted_count": sum(r["deleted_count"] for r in runs),
            "bytes_freed": sum(r["bytes_freed"] for r in runs),
            "failed_count": sum(r["failed_count"] for r in runs),
        },
        "unreadable_receipts": unreadable,
    }
