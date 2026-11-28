"""Stored-state compatibility check for ``deploy-check`` (ADR 0214).

Read-only: before a deploy, the stored rows the runner will read are parsed with the code
about to run, using the same functions the runtime uses, so a stored spec or record the new
code cannot read is found before it blocks a live campaign.

- ``state.stored_specs``: every non-terminal campaign's ``spec_json`` parses and re-digests to
  its stored ``spec_digest`` (``campaign_runner.verify_stored_spec``, the function the
  runner's load uses). A Salad stage's workload contract is part of the spec
  (``WorkloadContractV1``), so it is validated here.
- ``state.stored_attempts``: every Salad attempt record still read at runtime (its campaign is
  not terminal, or the attempt is not settled) has a ``guardian_final_json`` that validates as
  the runtime's ``_GuardianFinal`` and a ``live_receipt_json`` that decodes to a JSON object
  (the runtime reads it with ``json.loads``).

The database is opened with a read-only URI and nothing is written. Findings name campaign or
attempt ids and closed reasons only, never stored content. Each check reads at most
``MAX_ROWS`` rows and says so when capped.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections import Counter
from pathlib import Path
from typing import Any

from .deploy_check import Finding
from .migrations import LATEST_SCHEMA_VERSION
from .store import DATABASE_NAME

AREA = "state"
CHECK_IDS = ("state.stored_specs", "state.stored_attempts")
MAX_ROWS = 5000
MAX_LISTED = 20
_TERMINAL = ("succeeded", "stopped", "failed", "cancelled")
_AFTER_MIGRATE = (
    "the stored-state check needs the current schema; deploy-upgrade runs deploy-check "
    "again after it migrates."
)


def _all(status: str, summary: str) -> list[Finding]:
    return [Finding(check_id, AREA, status, summary) for check_id in CHECK_IDS]


def _connect(state_dir: Path) -> sqlite3.Connection | None:
    database = state_dir / DATABASE_NAME
    try:
        status = os.lstat(database)
    except OSError:
        return None
    if not stat.S_ISREG(status.st_mode):
        return None
    connection = sqlite3.connect(database.absolute().as_uri() + "?mode=ro", uri=True, timeout=1)
    connection.row_factory = sqlite3.Row
    return connection


def check_stored_state(state_dir: Path) -> list[Finding]:
    try:
        connection = _connect(Path(state_dir))
    except sqlite3.Error:
        return _all("skipped", "The control database could not be opened read-only.")
    if connection is None:
        return _all("skipped", "No control database; the schema check reports it.")
    try:
        try:
            row = connection.execute(
                "SELECT schema_version FROM schema_meta WHERE singleton = 1"
            ).fetchone()
        except sqlite3.Error:
            row = None
        version = None if row is None else row[0]
        if type(version) is not int:
            return _all("skipped", "No readable schema version; the schema check reports it.")
        if version != LATEST_SCHEMA_VERSION:
            return _all(
                "skipped",
                f"Schema v{version} is not current (v{LATEST_SCHEMA_VERSION}): {_AFTER_MIGRATE}",
            )
        return [_check_specs(connection), _check_attempts(connection)]
    except sqlite3.Error as error:
        return _all("fail", f"The stored state could not be read ({type(error).__name__}).")
    finally:
        connection.close()


def _result(
    check_id: str, noun: str, checked: int, capped: bool, bad: list[tuple[str, str]]
) -> Finding:
    detail: dict[str, Any] = {"checked": checked, "max_rows": MAX_ROWS, "capped": capped}
    cap_note = f" Only the first {MAX_ROWS} were read." if capped else ""
    if bad:
        detail["failed"] = len(bad)
        detail["reasons"] = dict(sorted(Counter(reason for _, reason in bad).items()))
        detail["listed"] = [
            {"id": identifier, "reason": reason} for identifier, reason in bad[:MAX_LISTED]
        ]
        return Finding(
            check_id,
            AREA,
            "fail",
            f"{len(bad)} of {checked} stored {noun} cannot be read by this code.{cap_note}",
            detail,
        )
    return Finding(
        check_id,
        AREA,
        "warn" if capped else "ok",
        f"All {checked} stored {noun} read with this code.{cap_note}",
        detail,
    )


def _check_specs(connection: sqlite3.Connection) -> Finding:
    from .campaign_runner import verify_stored_spec

    marks = ",".join("?" for _ in _TERMINAL)
    rows = connection.execute(
        f"""SELECT id, spec_json, spec_digest FROM campaigns WHERE status NOT IN ({marks})
        ORDER BY created_at, id LIMIT ?""",
        (*_TERMINAL, MAX_ROWS + 1),
    ).fetchall()
    capped = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    bad: list[tuple[str, str]] = []
    for row in rows:
        verdict = verify_stored_spec(row["spec_json"], row["spec_digest"])
        if verdict.reason is not None:
            bad.append((str(row["id"]), verdict.reason))
    return _result("state.stored_specs", "campaign specs", len(rows), capped, bad)


def _check_attempts(connection: sqlite3.Connection) -> Finding:
    from pydantic import ValidationError

    from .campaign_salad_commands import _GuardianFinal

    marks = ",".join("?" for _ in _TERMINAL)
    rows = connection.execute(
        f"""SELECT sa.attempt_id, sa.guardian_final_json, sa.live_receipt_json
        FROM campaign_salad_attempts sa
        JOIN campaign_stage_attempts a ON a.id = sa.attempt_id
        JOIN campaigns c ON c.id = a.campaign_id
        WHERE (c.status NOT IN ({marks}) OR sa.phase != 'settled')
        AND (sa.guardian_final_json IS NOT NULL OR sa.live_receipt_json IS NOT NULL)
        ORDER BY sa.attempt_id LIMIT ?""",
        (*_TERMINAL, MAX_ROWS + 1),
    ).fetchall()
    capped = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    bad: list[tuple[str, str]] = []
    for row in rows:
        final = row["guardian_final_json"]
        if final is not None:
            try:
                _GuardianFinal.model_validate_json(final)
            except (ValidationError, ValueError, TypeError):
                bad.append((str(row["attempt_id"]), "guardian_final_unreadable"))
                continue
        receipt = row["live_receipt_json"]
        if receipt is not None:
            try:
                decoded = json.loads(receipt)
            except (ValueError, TypeError):
                decoded = None
            if not isinstance(decoded, dict):
                bad.append((str(row["attempt_id"]), "live_receipt_unreadable"))
    return _result("state.stored_attempts", "Salad attempt records", len(rows), capped, bad)
