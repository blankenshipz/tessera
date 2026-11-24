"""The deploy check: one read-mostly report of what a deploy still needs (ADR 0098).

A deploy used to need a person for a handful of separate steps: size the backup cap, run
migrations, drop a config key a release removed, register a provider profile through the
validated path, start the executor and run a CPU smoke. ``tessera deploy-check`` reports all
of them in one machine-readable document, offers a receipted fix for the ones that are safe to
apply, and changes nothing unless the operator names the fix with ``--apply``.

This module holds the shared vocabulary (findings, the report, receipts); each area has its
own module (``deploy_config``, ``deploy_state``, ``deploy_smoke``). Nothing here reads a
secret, contacts a provider or spends anything; the smoke is a synthetic, networkless
container the operator opts into.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPORT_SCHEMA = "tessera.deploy-check.v1"
RECEIPT_SCHEMA = "tessera.deploy-fix-receipt.v1"
STATUSES = ("ok", "warn", "fail", "skipped")
MAX_RECEIPT_BYTES = 65536


@dataclass(frozen=True)
class Fix:
    """A change the check can make itself (``--apply <id>``) or the exact command that does."""

    fix_id: str
    description: str
    # True when ``--apply <fix_id>`` performs it; False when the operator runs ``command``.
    automatic: bool = False
    command: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.fix_id,
            "description": self.description,
            "automatic": self.automatic,
            "command": self.command,
        }


@dataclass(frozen=True)
class Finding:
    check_id: str
    area: str
    status: str
    summary: str
    detail: Mapping[str, Any] = field(default_factory=dict)
    fix: Fix | None = None

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unknown finding status: {self.status}")

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.check_id,
            "area": self.area,
            "status": self.status,
            "summary": self.summary,
            "detail": dict(self.detail),
            "fix": None if self.fix is None else self.fix.payload(),
        }


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_report(
    findings: Iterable[Finding], applied: Iterable[Mapping[str, Any]] = ()
) -> dict[str, Any]:
    """The report document. ``ready`` is true only when nothing failed (warnings and skipped
    checks do not fail a deploy, but they are listed)."""
    items = list(findings)
    counts = {status: sum(1 for item in items if item.status == status) for status in STATUSES}
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": utc_stamp(),
        "ready": counts["fail"] == 0,
        "counts": counts,
        "checks": [item.payload() for item in items],
        "applied": [dict(entry) for entry in applied],
    }


def exit_code(report: Mapping[str, Any]) -> int:
    return 0 if report.get("ready") else 1


def render_text(report: Mapping[str, Any]) -> str:
    """A short human summary of the same report."""
    marks = {"ok": "ok  ", "warn": "warn", "fail": "FAIL", "skipped": "skip"}
    lines = []
    for check in report["checks"]:
        lines.append(f"[{marks[check['status']]}] {check['id']}: {check['summary']}")
        fix = check.get("fix")
        if fix is not None:
            how = f"--apply {fix['id']}" if fix["automatic"] else (fix["command"] or "")
            lines.append(f"       fix: {fix['description']} ({how})")
    counts = report["counts"]
    lines.append(
        f"{'ready' if report['ready'] else 'NOT ready'}: {counts['ok']} ok, "
        f"{counts['warn']} warn, {counts['fail']} fail, {counts['skipped']} skipped"
    )
    return "\n".join(lines)


def write_private_json(path: Path, document: Mapping[str, Any], *, mode: int = 0o600) -> None:
    """Atomically write ``document`` to ``path`` as an owner-only (default 0600) file."""
    data = (json.dumps(document, sort_keys=True, indent=2) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".deploy-check-")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), mode & 0o700)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def require_writable_directory(directory: Path, what: str) -> Path:
    """An existing absolute directory this process can create a file in, proven by doing it.

    Fixes call this BEFORE they change anything, so an applied fix is never left without its
    receipt because the receipt directory was wrong."""
    if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
        raise ValueError(f"The {what} must be an existing absolute directory.")
    try:
        descriptor, probe = tempfile.mkstemp(dir=directory, prefix=".deploy-check-probe-")
    except OSError:
        raise ValueError(f"The {what} is not writable.") from None
    os.close(descriptor)
    os.unlink(probe)
    return directory


def require_report_path(path: Path) -> Path:
    """An absolute report file path whose directory accepts a new file (checked before any fix)."""
    if not path.is_absolute() or path.is_dir() or path.is_symlink():
        raise ValueError("--report-file must be an absolute path to a file.")
    require_writable_directory(path.parent, "--report-file directory")
    return path


def write_receipt(receipt_dir: Path, fix_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """Record one applied fix as a receipt file; returns the receipt (with its digest).

    The receipt names what changed by digest and key name, never by value, so it carries no
    path content or secret. The file name is derived from the receipt digest, so the same
    change cannot overwrite an earlier receipt."""
    require_writable_directory(receipt_dir, "receipt directory")
    document: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "fix": fix_id,
        "applied_at": utc_stamp(),
        **dict(body),
    }
    digest = sha256_hex(json.dumps(document, sort_keys=True).encode())
    document["receipt_sha256"] = digest
    if len(json.dumps(document)) > MAX_RECEIPT_BYTES:
        raise ValueError("The receipt is too large.")
    write_private_json(receipt_dir / f"deploy-fix-{fix_id}-{digest[:16]}.json", document)
    return document
