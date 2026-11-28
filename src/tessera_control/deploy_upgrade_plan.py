"""Operator plan file for ``tessera deploy-upgrade`` (checked upgrade).

A closed, owner-only (0600) absolute JSON file that names the state, backup and receipt
directories, the services to restart and how to check each one is healthy again. A plan
holds no secrets. Service commands are argv lists run without a shell. Every load failure
names the field location only and never echoes a value from the file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

PLAN_SCHEMA = "tessera.deploy-upgrade-plan.v1"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_Path = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
_Revision = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{40}$")]
_ServiceName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
_Argv = Annotated[
    list[Annotated[str, StringConstraints(min_length=1, max_length=512)]],
    Field(min_length=1, max_length=16),
]


class PlanError(ValueError):
    """The plan file is unusable; the message never contains a value from the file."""


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)


def _absolute(value: str) -> str:
    path = Path(value)
    if "\0" in value or not path.is_absolute() or ".." in path.parts:
        raise ValueError("absolute path required")
    return value


def _argv(value: list[str]) -> list[str]:
    if any("\0" in item for item in value):
        raise ValueError("argv item")
    first = value[0]
    if not first.startswith("/") and "/" in first:
        raise ValueError("argv command must be an absolute path or a bare name")
    return value


class HealthFile(_Closed):
    kind: Literal["file"]
    path: _Path
    max_age_seconds: Annotated[int, Field(ge=5, le=86400)]

    _path = field_validator("path")(_absolute)


class HealthHttp(_Closed):
    kind: Literal["http"]
    url: Annotated[str, StringConstraints(min_length=1, max_length=2048)]
    timeout_seconds: Annotated[int, Field(ge=1, le=60)] = 5

    @field_validator("url")
    @classmethod
    def _loopback_url(cls, value: str) -> str:
        if "?" in value or "#" in value or "\0" in value:
            raise ValueError("url")
        parts = urlsplit(value)
        try:
            port = parts.port
            host = parts.hostname
        except ValueError:
            raise ValueError("url") from None
        if (
            parts.scheme != "http"
            or host not in _LOOPBACK_HOSTS
            or port is None
            or "@" in parts.netloc
            or not parts.path.startswith("/")
        ):
            raise ValueError("url")
        return value


class ConfigCheck(_Closed):
    """How a service is started, so the preflight can run its own ``--check-config`` (ADR 0214).

    ``args`` are the service's arguments as its unit passes them, without ``--check-config``."""

    program: Literal["serve", "salad-executor", "salad-guardian"]
    args: Annotated[
        list[Annotated[str, StringConstraints(max_length=512)]], Field(max_length=64)
    ] = []

    @field_validator("args")
    @classmethod
    def _plain_args(cls, value: list[str]) -> list[str]:
        if any("\0" in item or item == "--check-config" for item in value):
            raise ValueError("argument")
        return value


class Service(_Closed):
    name: _ServiceName
    restart_argv: _Argv
    stop_argv: _Argv | None = None
    # False only when the operator declares the service never writes the control database;
    # otherwise a service without a stop_argv blocks execute (ADR 0099).
    writes_state: bool = True
    # A virtualenv directory this service runs from. deploy-upgrade reinstalls the deployed
    # release into it after the stop and before the restart (ADR 0161).
    venv: _Path | None = None
    health: Annotated[HealthFile | HealthHttp, Field(discriminator="kind")]
    # Optional: the program and arguments this service starts with. The preflight (and the
    # post-install smoke check) runs its ``--check-config`` under this release.
    config_check: ConfigCheck | None = None
    health_wait_seconds: Annotated[int, Field(ge=5, le=900)] = 120
    command_timeout_seconds: Annotated[int, Field(ge=5, le=300)] = 60

    _venv = field_validator("venv")(lambda value: None if value is None else _absolute(value))

    @field_validator("restart_argv", "stop_argv")
    @classmethod
    def _plain_argv(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _argv(value)


class UpgradePlan(_Closed):
    schema_id: Annotated[str, Field(alias="schema")]
    state_dir: _Path
    backup_dir: _Path
    receipt_dir: _Path
    max_backup_bytes: int | None = None
    source_revision: _Revision | None = None
    # Focused tests for the upgrade range (from_revision..source_revision), run in the preflight
    # from the repository checkout at repo_root (default: the working directory).
    from_revision: _Revision | None = None
    focused_tests: bool = False
    focused_tests_jobs: Annotated[int, Field(ge=1, le=4)] = 2
    repo_root: _Path | None = None
    # What is pip-installed into each service venv: a checkout or built wheel path (default:
    # repo_root). Required when any service declares a venv.
    install_source: _Path | None = None
    venv_install_timeout_seconds: Annotated[int, Field(ge=30, le=1800)] = 600
    executor_configs: Annotated[list[_Path], Field(max_length=4)] = []
    live_configs: Annotated[list[_Path], Field(max_length=4)] = []
    providers_file: _Path | None = None
    services: Annotated[list[Service], Field(min_length=1, max_length=8)]
    smoke_cpu: bool = False
    # How long the services are expected to stay stopped. An active execution lease that would
    # expire within this time lapses during the stop (the executor renews to 150 s and cannot
    # renew while stopped), so the run refuses. Recorded stops took 20 s to 3.5 min; 120 s
    # covers a typical stop-install-start and is below the 150 s lease, so it passes only when
    # every lease was just renewed. Set 150 or more to refuse whenever any lease is held.
    expected_downtime_seconds: Annotated[int, Field(ge=5, le=3600)] = 120

    @field_validator(
        "state_dir", "backup_dir", "receipt_dir", "providers_file", "repo_root", "install_source"
    )
    @classmethod
    def _absolute_path(cls, value: str | None) -> str | None:
        return None if value is None else _absolute(value)

    @field_validator("executor_configs", "live_configs")
    @classmethod
    def _absolute_paths(cls, value: list[str]) -> list[str]:
        for item in value:
            _absolute(item)
        return value

    @field_validator("max_backup_bytes")
    @classmethod
    def _backup_bound(cls, value: int | None) -> int | None:
        from .recovery import backup_byte_limit
        from .store import StoreError

        try:
            backup_byte_limit(value)
        except StoreError:
            raise ValueError("backup byte limit") from None
        return value

    @model_validator(mode="after")
    def _distinct_services(self) -> UpgradePlan:
        names = [service.name for service in self.services]
        if len(set(names)) != len(names):
            raise ValueError("service names must be distinct")
        return self

    @model_validator(mode="after")
    def _venv_source(self) -> UpgradePlan:
        if self.venvs and (self.install_source or self.repo_root) is None:
            raise ValueError("service venv needs install_source or repo_root")
        return self

    @model_validator(mode="after")
    def _focused_range(self) -> UpgradePlan:
        if self.focused_tests and (self.from_revision is None or self.source_revision is None):
            raise ValueError("focused_tests needs from_revision and source_revision")
        return self

    @property
    def venvs(self) -> tuple[str, ...]:
        """Distinct service venv directories, in service order."""
        return tuple(dict.fromkeys(s.venv for s in self.services if s.venv))

    @property
    def release_source(self) -> str | None:
        return self.install_source or self.repo_root

    @property
    def service_names(self) -> tuple[str, ...]:
        return tuple(service.name for service in self.services)


def _location(error: ValidationError) -> str:
    locations = []
    for item in error.errors(include_input=False, include_url=False):
        parts = item["loc"][:-1] if item["type"] == "extra_forbidden" else item["loc"]
        loc = ".".join(str(part) for part in parts) or "plan"
        if item["type"] == "extra_forbidden":
            loc += " (unknown key)"
        if loc not in locations:
            locations.append(loc)
    return ", ".join(locations[:8])


def load_plan(path: Path | str) -> UpgradePlan:
    """Closed, owner-only absolute plan file; failures name a field, never a value."""
    from .salad_live_check import _private_bytes
    from .salad_workload_jwt import strict_json

    try:
        raw = _private_bytes(path, 65536)
    except (OSError, ValueError):
        raise PlanError(
            "Upgrade plan must be an absolute, owner-only (0600) regular file of at most "
            "65536 bytes."
        ) from None
    try:
        document = strict_json(raw)
    except ValueError:
        raise PlanError("Upgrade plan is not strict JSON (duplicate keys are refused).") from None
    try:
        plan = UpgradePlan.model_validate(document)
    except ValidationError as error:
        raise PlanError(f"Upgrade plan is invalid at: {_location(error)}.") from None
    if plan.schema_id != PLAN_SCHEMA:
        raise PlanError(f"Upgrade plan schema must be {PLAN_SCHEMA}.")
    return plan


__all__ = [
    "PLAN_SCHEMA",
    "ConfigCheck",
    "HealthFile",
    "HealthHttp",
    "PlanError",
    "Service",
    "UpgradePlan",
    "load_plan",
]
