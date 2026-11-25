"""Storage retention policy: plan (dry run), apply, receipts and a control-plane timer.

Nothing here decides what is safe to delete: the backup and artifact modules do, and they
keep everything referenced by a live campaign, lineage or an unreleased hold. This module
only runs them, lists the result first, applies only a plan whose digest the caller saw,
and writes every action to a private receipt (size and digest of each thing removed).
With no policy file, or no knob set, nothing is ever selected.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .storage_retention_models import (
    POLICY_FILE,
    Candidate,
    Plan,
    RetentionPolicy,
    receipt_dir,
)

log = logging.getLogger(__name__)


VERIFY_SUFFIX = ".verify.json"  # sweep receipts are findable without reading every receipt
VERIFY_KEEP = 14


class RetentionError(ValueError):
    """The policy or a requested apply is not acceptable; nothing was deleted."""


def _now() -> datetime:
    return datetime.now(UTC)


def load_policy(state_dir: Path) -> RetentionPolicy | None:
    """The operator's policy file, or ``None`` when there is none (then nothing is selected)."""
    path = state_dir / POLICY_FILE
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    try:
        return RetentionPolicy.model_validate_json(raw)
    except ValidationError as error:
        raise RetentionError(
            f"{POLICY_FILE} is invalid: {error.error_count()} problem(s)"
        ) from None


def policy_digest(policy: RetentionPolicy) -> str:
    body = json.dumps(policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def _artifact_backend(state_dir: Path) -> Any:
    from .project_artifacts import FilesystemProjectArtifactBackend
    from .store import _database_path

    return FilesystemProjectArtifactBackend(_database_path(state_dir).parent / "project-artifacts")


def build_plan(state_dir: Path, policy: RetentionPolicy | None, *, now: datetime | None = None):
    """Dry-run listing of everything the policy would delete or keep, with reasons."""
    now = now or _now()
    policy = policy or RetentionPolicy()
    candidates: list[Candidate] = []
    skipped: list[dict[str, str]] = []
    if policy.backup_root is not None:
        from .storage_retention_backups import plan_backups

        root = Path(policy.backup_root)
        found, bad = plan_backups(
            root, policy, now=now, free_bytes=lambda: shutil.disk_usage(root).free
        )
        skipped += bad
        candidates += found
    if policy.artifact_max_age_days is not None:
        from .storage_retention_artifacts import list_artifact_candidates

        found, bad = list_artifact_candidates(state_dir, policy, now=now)
        candidates += found
        skipped += bad
    if policy.reclaim_orphaned_large_artifacts:
        from .storage_retention_large import list_large_candidates

        found, bad = list_large_candidates(state_dir, policy, now=now)
        candidates += found
        skipped += bad
    if policy.reclaim_test_scratch:
        from .storage_retention_test_scratch import plan_test_scratch

        found, bad = plan_test_scratch(policy, now=now)
        candidates += found
        skipped += bad
    return Plan(now.isoformat(), policy_digest(policy), candidates, skipped)


def _reason(error: Exception) -> str:
    """Why a step failed: our own and the store's refusals are fixed text; anything else
    (an OS error can carry a path) is named by type only."""
    from .store import Conflict, NotFound, StoreError

    if isinstance(error, RetentionError | Conflict | NotFound | StoreError):
        return str(error)[:300]
    if type(error).__name__ in {
        "BackupRetentionError",
        "RetentionDeleteError",
        "LargeReclaimError",
        "ScratchError",
    }:
        return str(error)[:300]
    return type(error).__name__


def _write_receipt(directory: Path, name: str, receipt: dict[str, Any]) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    temporary = directory / f".{name}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(receipt, handle, sort_keys=True, indent=1)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, directory / name)


def run(
    state_dir: Path,
    *,
    mode: str,
    trigger: str,
    expect_plan_digest: str | None = None,
    now: datetime | None = None,
    delete_backup: Callable[..., dict] | None = None,
    delete_artifact: Callable[..., dict] | None = None,
    delete_large: Callable[..., dict] | None = None,
    delete_scratch: Callable[..., dict] | None = None,
) -> dict[str, Any]:
    """Plan, and in ``apply`` mode delete; every run leaves a receipt, rewritten per deletion.

    ``apply`` requires the digest of a plan the caller has seen (``expect_plan_digest``);
    if the fresh plan no longer matches, nothing is deleted. The timer passes the digest of
    its own fresh plan, which is allowed only when the policy sets ``auto_apply``.
    """
    if mode not in {"dry_run", "apply"} or trigger not in {"timer", "manual", "agent"}:
        raise RetentionError("mode is dry_run or apply; trigger is timer, manual or agent")
    policy = load_policy(state_dir)
    started = (now or _now()).isoformat()
    run_id = uuid.uuid4().hex
    plan = build_plan(state_dir, policy, now=now)
    receipt: dict[str, Any] = {
        "run_id": run_id,
        "started_at": started,
        "finished_at": None,
        "mode": mode,
        "trigger": trigger,
        "policy_digest": plan.policy_digest,
        "plan_digest": plan.digest(),
        "plan": plan.as_dict(),
        "deleted": [],
        "failed": [],
        "bytes_freed": 0,
        "status": "started",
    }
    moment = (now or _now()).astimezone(UTC)
    name = f"{moment.strftime('%Y%m%dT%H%M%S%f')}Z-{run_id}.json"
    directory = receipt_dir(state_dir)
    if mode == "apply":
        refusal = None
        if policy is None:
            refusal = "no retention policy is configured"
        elif trigger == "timer" and not policy.auto_apply:
            refusal = "auto_apply is not enabled"
        elif expect_plan_digest != plan.digest():
            refusal = "the plan changed since it was listed; list it again"
        if refusal is not None:
            # A refused apply is an action too: nothing was deleted, and the receipt says why.
            receipt.update(status="refused", reason=refusal, finished_at=_now().isoformat())
            _write_receipt(directory, name, receipt)
            raise RetentionError(refusal)
    _write_receipt(directory, name, receipt)
    if mode == "apply":
        from .storage_retention_artifacts import delete_artifact as artifact_delete
        from .storage_retention_backups import delete_backup as backup_delete

        remove_backup = delete_backup or backup_delete
        remove_artifact = delete_artifact or artifact_delete
        remove_large = delete_large
        backend = None
        if delete_backup is None and any(c.kind == "backup" for c in plan.deletions()):
            # Plan to delete gap: the bundles that must survive are re-hashed once more, right
            # before the first deletion; if any no longer restores, nothing is deleted.
            from .storage_retention_backups import reverify_kept

            try:
                assert policy is not None and policy.backup_root is not None
                reverify_kept(
                    Path(policy.backup_root), [c for c in plan.candidates if c.kind == "backup"]
                )
            except Exception as error:  # noqa: BLE001 - any failure refuses the whole apply
                reason = f"a kept bundle no longer verifies: {_reason(error)}"
                receipt.update(status="refused", reason=reason, finished_at=_now().isoformat())
                _write_receipt(directory, name, receipt)
                raise RetentionError(reason) from None
        for candidate in plan.deletions():
            try:
                if candidate.kind == "backup":
                    assert policy is not None and policy.backup_root is not None
                    removed = remove_backup(Path(policy.backup_root), candidate)
                elif candidate.kind == "test_scratch":
                    from .storage_retention_test_scratch import (
                        delete_test_scratch,
                        resolve_root,
                    )

                    assert policy is not None
                    remove_scratch = delete_scratch or delete_test_scratch
                    removed = remove_scratch(resolve_root(policy), candidate)
                elif candidate.kind == "large_artifact":
                    if remove_large is None:
                        from .storage_retention_large import delete_large as large_delete

                        remove_large = large_delete
                    removed = remove_large(state_dir, candidate, run_id=run_id)
                else:
                    backend = backend or _artifact_backend(state_dir)
                    removed = remove_artifact(state_dir, backend, candidate, run_id=run_id)
                receipt["deleted"].append(
                    {
                        "kind": candidate.kind,
                        "ref": candidate.ref,
                        "path": candidate.path,
                        "size_bytes": removed.get("size_bytes", candidate.size_bytes),
                        "sha256": removed.get("sha256", candidate.sha256),
                        "reasons": candidate.reasons,
                        "deleted_at": _now().isoformat(),
                        **({"companion": removed["companion"]} if "companion" in removed else {}),
                    }
                )
                receipt["bytes_freed"] += int(receipt["deleted"][-1]["size_bytes"])
            except Exception as error:  # noqa: BLE001 - one refusal must not stop the rest
                failure = {
                    "kind": candidate.kind,
                    "ref": candidate.ref,
                    "error": type(error).__name__,
                    "reason": _reason(error),
                }
                if getattr(error, "tombstoned", False):
                    failure["state_after"] = "tombstoned"  # reserved, not deleted
                receipt["failed"].append(failure)
            _write_receipt(directory, name, receipt)  # durable after every deletion
    receipt["finished_at"] = _now().isoformat()
    receipt["status"] = "failed" if receipt["failed"] else "complete"
    _write_receipt(directory, name, receipt)
    return receipt


class TimerHandle:
    def __init__(self, thread: threading.Thread, stop: threading.Event) -> None:
        self.thread, self.stop_event = thread, stop

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)


def verify(state_dir: Path, *, trigger: str = "manual", now: datetime | None = None):
    """Fully re-hash every backup with restore's validation and receipt the result.

    A plan reads only the bundles whose verdict matters, so a young bundle that rotted would
    never show there; this sweep is how it becomes visible (status reports it)."""
    from .storage_retention_backups import verify_all

    policy = load_policy(state_dir)
    if policy is None or policy.backup_root is None:
        raise RetentionError("no backup_root is configured")
    started = now or _now()
    run_id = uuid.uuid4().hex
    result = verify_all(Path(policy.backup_root))
    receipt = {
        "run_id": run_id,
        "started_at": started.isoformat(),
        "finished_at": _now().isoformat(),
        "mode": "verify",
        "trigger": trigger,
        "status": "failed" if "error" in result else "complete",
        **result,
    }
    moment = started.astimezone(UTC)
    directory = receipt_dir(state_dir)
    name = f"{moment.strftime('%Y%m%dT%H%M%S%f')}Z-{run_id}{VERIFY_SUFFIX}"
    _write_receipt(directory, name, receipt)
    # About one a day: keep the newest few, so sweeps do not pile up beside plan receipts.
    for old in sorted(directory.glob(f"*{VERIFY_SUFFIX}"), reverse=True)[VERIFY_KEEP:]:
        old.unlink(missing_ok=True)
    return receipt


def _verify_due(state_dir: Path, policy: RetentionPolicy, now: datetime) -> bool:
    if policy.backup_root is None:
        return False
    try:
        newest = sorted(receipt_dir(state_dir).glob(f"*{VERIFY_SUFFIX}"), reverse=True)[:3]
    except OSError:
        return True
    for path in newest:  # a damaged newest receipt falls back to the one before it
        try:
            done = datetime.fromisoformat(json.loads(path.read_text())["finished_at"])
            return (now - done).total_seconds() >= policy.backup_verify_interval_hours * 3600
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return True


def tick(state_dir: Path, last_dry_digest: str | None) -> str | None:
    """One timer pass. Reports a changed dry-run plan; applies only when ``auto_apply`` is set."""
    policy = load_policy(state_dir)
    if policy is None:
        return last_dry_digest
    try:
        if _verify_due(state_dir, policy, _now()):
            verify(state_dir, trigger="timer")
    except Exception as error:  # noqa: BLE001 - a failing sweep must never block the plan
        log.error("storage retention sweep failed: %s", type(error).__name__)
    plan = build_plan(state_dir, policy)
    if not plan.deletions():
        return None
    if policy.auto_apply:
        run(state_dir, mode="apply", trigger="timer", expect_plan_digest=plan.digest())
        return None
    if plan.digest() != last_dry_digest:
        run(state_dir, mode="dry_run", trigger="timer")
    return plan.digest()


def start_background(state_dir: Path) -> TimerHandle:
    """Control-plane timer; the interval is read from the policy each pass."""
    stop = threading.Event()

    def loop() -> None:
        digest: str | None = None
        while not stop.is_set():
            interval = 3600
            try:
                policy = load_policy(state_dir)
                interval = policy.timer_interval_seconds if policy else interval
                digest = tick(state_dir, digest)
            except Exception as error:  # noqa: BLE001 - keep the timer alive, log no payloads
                log.error("storage retention pass failed: %s", type(error).__name__)
            stop.wait(interval)

    thread = threading.Thread(target=loop, name="storage-retention", daemon=True)
    thread.start()
    return TimerHandle(thread, stop)


__all__ = [
    "RetentionError",
    "build_plan",
    "load_policy",
    "policy_digest",
    "run",
    "verify",
    "start_background",
    "tick",
]
