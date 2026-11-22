"""Operator-configured program budget across campaigns (ADR 0045 section 3).

A program cap narrows admission and never widens anything: campaign caps, standing
approval caps, the project spend limit and a provider guardian's own cap all still apply.
The cap counts committed money, not only spent money: a running campaign counts its full
budget cap, a finished one what it held and settled. Every first admission of a campaign
(forward approval) and every standing-approval start must fit under every program cap that
covers its project. A configured-but-unusable program budget refuses everything
(fail closed). Refusal reasons never carry another tenant's totals.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from typing import Annotated, Any

from pydantic import Field, StringConstraints, ValidationError, model_validator

from .campaign_models import TERMINAL_CAMPAIGN, _Strict

PROGRAM_BUDGET_SCHEMA = "tessera.program-budget.v1"
MAX_PROGRAM_BUDGET_BYTES = 65536
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TERMINAL = tuple(sorted(TERMINAL_CAMPAIGN))
_MARKS = ",".join("?" for _ in _TERMINAL)

ProgramId = Annotated[str, StringConstraints(pattern=_ID.pattern)]


class Program(_Strict):
    id: ProgramId
    cap_cents: Annotated[int, Field(strict=True, ge=0, le=100_000_000)]
    # Absent: every project of this deployment counts towards (and is bound by) the cap.
    project_ids: (
        Annotated[
            list[Annotated[str, Field(min_length=1, max_length=200)]],
            Field(min_length=1, max_length=100),
        ]
        | None
    ) = None


class ProgramBudgetConfig(_Strict):
    schema_: Annotated[str, Field(alias="schema")]
    programs: Annotated[list[Program], Field(min_length=1, max_length=20)]

    @model_validator(mode="after")
    def valid(self) -> ProgramBudgetConfig:
        if self.schema_ != PROGRAM_BUDGET_SCHEMA:
            raise ValueError(f"schema must be {PROGRAM_BUDGET_SCHEMA}")
        if len({p.id for p in self.programs}) != len(self.programs):
            raise ValueError("program ids must be unique")
        return self

    model_config = {**_Strict.model_config, "populate_by_name": False}


_lock = threading.Lock()
_config: ProgramBudgetConfig | None = None
# Set when a configuration was requested but could not be used: everything is refused.
_broken = False


def load_program_budget_file(path: Any) -> ProgramBudgetConfig:
    """Read an owner-only (0600, single-link, no symlink) ``--program-budget`` file."""
    from .salad_live_check import _private_bytes
    from .salad_workload_jwt import strict_json

    try:
        document = strict_json(_private_bytes(path, MAX_PROGRAM_BUDGET_BYTES))
        return ProgramBudgetConfig.model_validate(document)
    except (OSError, ValueError, ValidationError):
        raise ValueError(
            "The program budget must be an owner-only (0600) absolute JSON file holding a "
            f"valid {PROGRAM_BUDGET_SCHEMA} document."
        ) from None


def configure(config: ProgramBudgetConfig | None) -> None:
    global _config, _broken
    with _lock:
        _config, _broken = config, False


def mark_broken() -> None:
    """A configuration was requested and failed to load: refuse every admission."""
    global _config, _broken
    with _lock:
        _config, _broken = None, True


def current() -> ProgramBudgetConfig | None:
    with _lock:
        return _config


def reset() -> None:
    configure(None)


def _committed(connection: sqlite3.Connection, program: Program) -> int:
    clause, params = "", ()
    if program.project_ids is not None:
        clause = f" AND project_id IN ({','.join('?' for _ in program.project_ids)})"
        params = tuple(program.project_ids)
    row = connection.execute(
        f"""SELECT COALESCE(SUM(CASE WHEN status IN ({_MARKS}) THEN held_cents + settled_cents
        ELSE MAX(budget_cap_cents, held_cents + settled_cents) END), 0)
        FROM campaigns WHERE status<>'submitted'{clause}""",
        (*_TERMINAL, *params),
    ).fetchone()
    return int(row[0])


def check_admission(connection: sqlite3.Connection, project_id: str, cap_cents: int) -> str | None:
    """None when admitting a campaign with ``cap_cents`` keeps every covering program under
    its cap, otherwise a refusal reason. Call inside the admission transaction."""
    with _lock:
        config, broken = _config, _broken
    if broken:
        return "program_budget_unavailable"
    if config is None:
        return None
    for program in config.programs:
        if program.project_ids is not None and project_id not in program.project_ids:
            continue
        if _committed(connection, program) + cap_cents > program.cap_cents:
            return f"program_budget_exhausted:{program.id}"
    return None
