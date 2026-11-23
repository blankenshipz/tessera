"""Where the control state directory is, for owner-run commands that need it.

Resolution, in one place (no new file format, no built-in default path, because the deployment
layouts differ and a default could silently hit the wrong store):

1. an explicit ``--state-dir``;
2. the ``TESSERA_CONTROL_STATE_DIR`` environment variable (the shipped control unit sets it for
   the service; an owner shell exports the same value, since a unit's ``Environment=`` does not
   reach other shells);
3. the ``state_dir`` of the host's deploy plan (``--plan FILE``, the existing closed, owner-only
   upgrade plan, validated by ``deploy_upgrade_plan.load_plan``).

Every source that is given is resolved; if two disagree the command refuses (it mints credentials,
so a wrong store must never be picked silently), and if none is given it refuses and names the
three ways to give one. The result must be an existing directory that holds the control database.
Messages name sources, never values.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

ENV_VAR = "TESSERA_CONTROL_STATE_DIR"
DATABASE_NAME = "tessera.sqlite3"  # the same name store.py uses


class StateDirRefused(ValueError):
    """The state directory cannot be resolved safely."""


def _normal(value: str | Path, source: str) -> Path:
    text = os.fspath(value)
    if not text or "\x00" in text:
        raise StateDirRefused(f"The state directory from {source} is empty or invalid.")
    return Path(text).expanduser().resolve()


def resolve_state_dir(
    explicit: Path | str | None = None,
    *,
    plan: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """The one control state directory these sources agree on, or a closed refusal."""
    environment = os.environ if environ is None else environ
    found: list[tuple[str, Path]] = []
    if explicit is not None:
        found.append(("--state-dir", _normal(explicit, "--state-dir")))
    if environment.get(ENV_VAR):
        found.append((ENV_VAR, _normal(environment[ENV_VAR], ENV_VAR)))
    if plan is not None:
        from .deploy_upgrade_plan import PlanError, load_plan

        try:
            found.append(("--plan", _normal(load_plan(plan).state_dir, "--plan")))
        except PlanError as error:
            raise StateDirRefused(f"--plan is unusable: {error}") from None
    if not found:
        raise StateDirRefused(
            f"No state directory given: pass --state-dir, set {ENV_VAR}, or pass --plan FILE "
            "(a deploy plan that names state_dir)."
        )
    distinct = {path for _, path in found}
    if len(distinct) > 1:
        raise StateDirRefused(
            "The state directory sources disagree (" + ", ".join(name for name, _ in found) + "); "
            "give one, or make them name the same directory."
        )
    chosen = found[0][1]
    if not chosen.is_dir() or not (chosen / DATABASE_NAME).is_file():
        raise StateDirRefused(
            f"The resolved state directory (from {found[0][0]}) is not an existing control "
            "state directory (no database found)."
        )
    return chosen
