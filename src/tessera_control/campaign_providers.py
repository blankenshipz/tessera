"""Compute provider plugins for campaign stages (ADR 0040).

The runner core calls only this protocol. A provider never writes state outside the
runner connection under the runner lease, and every external effect goes through a
delegated step (``campaign_delegation.STEP_TABLE``) that the provider declares.
This module imports no vendor module at import time.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel

# Built-in kinds; a plugin may declare another lowercase slug (``route_*`` kinds name an
# ``execution_targets`` target, ``worker_grant`` runs through a worker grant).
ProviderKind = str
KNOWN_KINDS = ("worker_grant", "salad_route")
PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_OUTPUT_BYTES_LIMIT = 1 << 40
# Worker grants one principal may hold at once, and the largest scratch ceiling a provider may
# declare (the operator file accepts no more than either).
MAX_WORKER_SLOTS = 8
MAX_SCRATCH_BYTES = 64 * 1024**3
TrustClass = Literal["public", "private_code_approved", "private_derived"]
TRUST_CLASSES: tuple[TrustClass, ...] = ("public", "private_code_approved", "private_derived")
SettlementBasis = Literal[
    "local_zero",
    "unclaimed_zero",
    "human_charge",
    "human_reconcile",
    "guardian_bounded_hold",
    "provider_billed",
    "provider_absent_zero",
]
Outcome = Literal["running", "succeeded", "failed_transient", "failed_permanent", "unknown"]
PrepareResult = Literal["ready", "progress", "waiting"]


@dataclass(frozen=True)
class OutputLimits:
    max_files: int
    max_total_bytes: int
    max_file_bytes: int


@dataclass(frozen=True)
class ProviderDescriptor:
    provider_id: str
    kind: ProviderKind
    runtimes: frozenset[str]
    # execution_targets name for route-based providers; None for worker grants.
    target: str | None
    billable: bool
    third_party: bool
    delegable_steps: frozenset[str]
    output_limits: OutputLimits
    worker_principal_id: str
    # Grants a worker principal may hold at once (ADR 0048); the operator sets it to the
    # number of pollers sharing the principal. Only worker-grant providers read it.
    worker_slots: int = 1
    # The largest scratch a stage may request from this provider's worker (ADR 0059): the
    # operator states what its worker's own ceiling grants, 64 MiB unless raised.
    max_scratch_bytes: int = 64 * 1024 * 1024
    # ADR 0123: registry destinations (``host[/path]`` prefixes) an image-build stage on this
    # provider may push to. Operator-owned; empty means no pushing build is accepted.
    build_repositories: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlacementCapability:
    """What a provider is, for default placement (ADR 0088), reported per stage candidate.

    ``tier`` is ``local`` (capacity the operator hosts) or ``remote`` (rented capacity), and
    ``hardware`` is ``cpu`` or ``gpu``. A provider reports it from its own registration and the
    stage's configuration through the optional ``placement_capability(stage)`` method; placement
    reads only this, never a vendor name."""

    tier: Literal["local", "remote"]
    hardware: Literal["cpu", "gpu"]


def placement_capability_of(provider: Any, stage: Any) -> PlacementCapability | None:
    """The provider's own report for this stage candidate, else what its descriptor implies
    (a worker-grant provider is local CPU), else None (placement leaves it where it is)."""
    report = getattr(provider, "placement_capability", None)
    if callable(report):
        try:
            found = report(stage)
        except Exception:  # noqa: BLE001 - an unreadable stage config is not placed first
            return None
        return found if isinstance(found, PlacementCapability) else None
    if provider.descriptor.kind == "worker_grant":
        return PlacementCapability("local", "cpu")
    return None


@dataclass(frozen=True)
class GrantRequest:
    worker_principal_id: str
    ttl_seconds: int
    # Upper bound on grant expiry beyond release/ttl (e.g. approval expiry); None = none.
    expires_not_after: str | None = None


@dataclass(frozen=True)
class Observation:
    outcome: Outcome
    failure_class: str | None = None
    detail: str = ""
    evidence_kind: Literal["run_receipt", "salad_live_receipt"] | None = None
    evidence_digest: str | None = None
    manifest_id: str | None = None
    exit_code: int | None = None


@dataclass(frozen=True)
class Settlement:
    cents: int
    basis: SettlementBasis
    final: bool
    estimate_cents: int | None = None


class Waiting(Exception):  # noqa: N818 - a control-flow signal, like the runner's _Waiting
    """The provider cannot progress or settle yet; the runner retries next tick."""


class RunnerServices(Protocol):
    """The narrow runner facade a provider may use (implemented by CampaignRunner)."""

    # Per-runner scratch space keyed by provider id; it dies with the runner.
    provider_state: dict[str, Any]

    def now(self) -> Any: ...
    def connection(self) -> Any: ...  # context manager: runner_connection under the lease
    def step(
        self,
        ctx: Any,
        key: str,
        attempt: int,
        step: str,
        ordinal: int,
        build: Callable[[], tuple[dict[str, str], dict[str, Any]]],
    ) -> Any: ...
    def rows(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]: ...
    def one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None: ...
    def set_stage(self, ctx: Any, key: str, **fields: Any) -> None: ...
    def set_attempt(self, attempt_id: str, **fields: Any) -> None: ...
    def emit_once(self, ctx: Any, event_type: str, match: dict, payload: dict) -> None: ...
    def awaiting_human(self, ctx: Any, key: str, record: str, reason: str) -> None: ...


@runtime_checkable
class ComputeProvider(Protocol):
    descriptor: ProviderDescriptor
    config_model: type[BaseModel] | None  # validates StageV2.provider_config

    # submit/approve
    def validate_stage(self, stage: Any) -> None: ...  # pure; raise ValueError
    def check_stage(
        self, connection: sqlite3.Connection, project_id: str, stage: Any
    ) -> None: ...  # DB-aware; raise Conflict
    def execution_spec(self, stage: Any, input_digest: str) -> dict[str, Any]: ...

    # submit. ``prepare`` must be idempotent: once the provider's preparation of the stage
    # stands it returns "ready" without effects. The runner may call it before every
    # reservation of a placed stage (ADR 0060), not only once.
    def prepare(self, rt: RunnerServices, ctx: Any, key: str) -> PrepareResult: ...
    def grant_request(self, rt: RunnerServices, ctx: Any, stage: Any) -> GrantRequest: ...
    def after_admit(self, rt: RunnerServices, ctx: Any, attempt: Mapping[str, Any]) -> None: ...

    # poll: drive provider steps for an admitted attempt, then report
    def advance(
        self, rt: RunnerServices, ctx: Any, stage_row: Mapping, attempt: Mapping
    ) -> bool: ...
    def observe(self, rt: RunnerServices, attempt: Mapping[str, Any]) -> Observation: ...

    # cancel (reduce-only); True once the attempt's authority is reconciled
    def cancel(self, rt: RunnerServices, ctx: Any, key: str, attempt: Mapping) -> bool: ...

    # cost/settle
    def settle(self, rt: RunnerServices, attempt: Mapping[str, Any]) -> Settlement: ...
    def exposure_open(self, connection: sqlite3.Connection, campaign_id: str) -> bool: ...
    def after_tick(self, rt: RunnerServices, ctx: Any) -> None: ...

    # receipts
    def receipt_fields(
        self, connection: sqlite3.Connection, attempt: Mapping[str, Any]
    ) -> dict[str, Any]: ...


@runtime_checkable
class ProviderHooks(Protocol):
    """Optional runner hooks. A provider implements any it needs; the runner skips the rest.

    ``sweep(rt)``: tick-level upkeep. ``on_settled(connection, attempt_id, cents, stamp)``:
    bookkeeping that commits in the settlement transaction. ``experiment_ready(rt, ctx,
    stage, spec_stage)``: scaffolding before a hold (False after it ran a step)."""

    def sweep(self, rt: RunnerServices) -> None: ...
    def on_settled(
        self, connection: sqlite3.Connection, attempt_id: str, cents: int, stamp: str
    ) -> None: ...
    def experiment_ready(
        self, rt: RunnerServices, ctx: Any, stage: Any, spec_stage: Any
    ) -> bool: ...


class DefaultHooks:
    """Mixin with no-op optional hooks, for plugins that want none of them."""

    def sweep(self, rt: RunnerServices) -> None:
        return None

    def on_settled(
        self, connection: sqlite3.Connection, attempt_id: str, cents: int, stamp: str
    ) -> None:
        return None

    def experiment_ready(self, rt: RunnerServices, ctx: Any, stage: Any, spec_stage: Any) -> bool:
        return True


_PROVIDERS: dict[str, ComputeProvider] = {}
# Modules whose import registers the built-in providers.
BUILTIN_MODULES = (
    "tessera_control.campaign_provider_worker",
    "tessera_control.campaign_provider_salad",
)


def validate_descriptor(d: Any) -> None:
    """Refuse a malformed provider descriptor at registration (ValueError)."""
    if not isinstance(d, ProviderDescriptor):
        raise ValueError("provider descriptor required")
    if type(d) is not ProviderDescriptor:
        raise ValueError(
            f"provider descriptor must be exactly ProviderDescriptor, not {type(d).__name__}"
        )
    pid = d.provider_id
    if not isinstance(pid, str) or not PROVIDER_ID_RE.match(pid):
        raise ValueError(f"Provider id {pid!r} must match {PROVIDER_ID_RE.pattern}")
    bad = f"Provider {pid}: invalid descriptor"
    if not isinstance(d.kind, str) or not KIND_RE.match(d.kind):
        raise ValueError(f"{bad} (kind)")
    if (
        not isinstance(d.runtimes, frozenset)
        or not d.runtimes
        or not all(isinstance(r, str) and r for r in d.runtimes)
    ):
        raise ValueError(f"{bad} (runtimes)")
    if not isinstance(d.delegable_steps, frozenset) or not all(
        isinstance(step, str) for step in d.delegable_steps
    ):
        raise ValueError(f"{bad} (delegable_steps)")
    # A worker grant has no route target; every other kind names its execution target.
    if (d.kind == "worker_grant") != (d.target is None) or (
        d.target is not None and not (isinstance(d.target, str) and d.target)
    ):
        raise ValueError(f"{bad} (kind {d.kind!r} and target {d.target!r} do not match)")
    if not isinstance(d.billable, bool) or not isinstance(d.third_party, bool):
        raise ValueError(f"{bad} (billable and third_party must be booleans)")
    if d.billable and not d.third_party:
        raise ValueError(f"{bad} (a billable provider must declare third_party)")
    if d.kind == "salad_route" and d.third_party is not True:
        raise ValueError(f"{bad} (a salad_route provider must declare third_party=True)")
    limits = d.output_limits
    if not isinstance(limits, OutputLimits):
        raise ValueError(f"{bad} (output limits)")
    for name in ("max_files", "max_total_bytes", "max_file_bytes"):
        value = getattr(limits, name)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 < value <= (MAX_OUTPUT_BYTES_LIMIT)
        ):
            raise ValueError(f"{bad} (output limit {name} must be a positive integer)")
    if limits.max_file_bytes > limits.max_total_bytes:
        raise ValueError(f"{bad} (max_file_bytes exceeds max_total_bytes)")
    if not isinstance(d.worker_principal_id, str):
        raise ValueError(f"{bad} (worker principal)")
    slots = d.worker_slots
    if type(slots) is not int or not 1 <= slots <= MAX_WORKER_SLOTS:
        raise ValueError(f"{bad} (worker_slots must be an integer in 1..{MAX_WORKER_SLOTS})")
    scratch = d.max_scratch_bytes
    if type(scratch) is not int or not 0 < scratch <= MAX_SCRATCH_BYTES:
        raise ValueError(f"{bad} (max_scratch_bytes must be an integer in 1..{MAX_SCRATCH_BYTES})")


# Every method a provider must implement; tests keep this in step with ComputeProvider.
REQUIRED_PROVIDER_METHODS = (
    "validate_stage",
    "check_stage",
    "execution_spec",
    "prepare",
    "grant_request",
    "after_admit",
    "advance",
    "observe",
    "cancel",
    "settle",
    "exposure_open",
    "after_tick",
    "receipt_fields",
)
# Hooks the runner reads with getattr; each must be callable when present.
OPTIONAL_PROVIDER_HOOKS = ("sweep", "on_settled", "experiment_ready", "placement_capability")


def validate_provider_object(provider: Any) -> None:
    """Refuse a provider object that does not implement ComputeProvider (ValueError listing
    every problem found)."""
    problems: list[str] = []
    for name in REQUIRED_PROVIDER_METHODS:
        if not hasattr(provider, name):
            problems.append(f"missing method {name}")
        elif not callable(getattr(provider, name)):
            problems.append(f"{name} is not callable")
    if not hasattr(provider, "config_model"):
        problems.append("missing config_model")
    else:
        model = provider.config_model
        if model is not None and not (isinstance(model, type) and issubclass(model, BaseModel)):
            problems.append("config_model must be None or a pydantic BaseModel subclass")
    for name in OPTIONAL_PROVIDER_HOOKS:
        if hasattr(provider, name) and not callable(getattr(provider, name)):
            problems.append(f"optional hook {name} is not callable")
    if problems:
        raise ValueError("Provider object is not a ComputeProvider: " + "; ".join(problems))


def validate_provider(provider: Any) -> Any:
    """Run every registration check except the duplicate-id one; return the descriptor."""
    d = getattr(provider, "descriptor", None)
    validate_descriptor(d)
    validate_provider_object(provider)
    from .campaign_delegation import STEP_TABLE

    unknown = d.delegable_steps - frozenset(STEP_TABLE)
    if unknown:
        raise ValueError(f"Provider {d.provider_id} declares unknown steps: {sorted(unknown)}")
    return d


def register_provider(provider: ComputeProvider) -> ComputeProvider:
    d = validate_provider(provider)
    if d.provider_id in _PROVIDERS:
        raise ValueError(f"Provider already registered: {d.provider_id}")
    _PROVIDERS[d.provider_id] = provider
    return provider


def replace_provider(provider: ComputeProvider) -> ComputeProvider:
    """Swap in a provider for its id; the replacement is fully validated first, so a refusal
    leaves any existing provider registered and unchanged."""
    d = validate_provider(provider)
    _PROVIDERS[d.provider_id] = provider
    return provider


def unregister_provider(provider_id: str) -> None:
    """Tests only."""
    _PROVIDERS.pop(provider_id, None)


def get_provider(provider_id: str) -> ComputeProvider:
    try:
        return _PROVIDERS[provider_id]
    except (KeyError, TypeError):
        raise KeyError(f"Unknown compute provider: {provider_id!r}") from None


def provider_ids() -> tuple[str, ...]:
    return tuple(_PROVIDERS)


def load_builtin_providers(configure: Callable[[str], None] | None = None) -> None:
    """Import built-in provider modules; each registers from the operator file it is given."""
    for name in BUILTIN_MODULES:
        module = importlib.import_module(name)
        if configure is not None:
            configure(module.__name__)


@dataclass(frozen=True)
class ConfigSection:
    """Where a provider's entries live in the operator file and how they are checked (ADR 0081).

    A provider module (built in or a plugin) returns these from an optional module-level
    ``config_sections()``, so adding a vendor never edits the registration code or the runner.
    ``path`` locates a list of entries keyed by ``key_field``, or one object when ``key_field``
    is None. ``validate`` re-checks the whole document after a change and raises ValueError;
    ``describe`` returns the non-secret fields worth listing; ``worker`` names the principal the
    entry grants to (for the provider trust readout)."""

    name: str
    provider: str
    path: tuple[str, ...]
    key_field: str | None
    model: type[BaseModel]
    validate: Callable[[dict[str, Any]], None]
    describe: Callable[[dict[str, Any]], dict[str, Any]]
    worker: Callable[[dict[str, Any]], str | None] = lambda entry: None  # noqa: E731
    container: Callable[[], dict[str, Any]] | None = None
    # "organization": entries name their ``org_id`` and an organization's human may manage its
    # own; "deployment" (the default): shared by every organization, so only the deployment's
    # root owner may change them through the API.
    scope: Literal["organization", "deployment"] = "deployment"
    # The provider id an entry registers under, when it is not the section's (self-hosted: its id).
    provider_of: Callable[[dict[str, Any]], str] | None = None


ENTRY_POINT_GROUP = "tessera.campaign_providers"
PLUGIN_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
_PLUGIN_MODULES: dict[str, str] = {}


def _plugin_module(name: str) -> Any:
    """Resolve an operator-named plugin: an installed entry point first, else a dotted module.

    Nothing is loaded unless the operator file names it, so installing a package never
    enables a vendor by itself."""
    if not PLUGIN_NAME_RE.match(name):
        raise ValueError(f"Invalid provider plugin name {name!r}")
    for entry in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        if entry.name == name:
            return entry.load()
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as error:
        if error.name == name.split(".")[0] or error.name == name:
            raise ValueError(f"Provider plugin {name!r} is not installed") from None
        raise


def load_plugin(name: str, section: Any) -> None:
    """Load one plugin and let it register from its own config section.

    The plugin exposes ``register_from_config(section)`` (module attribute or the loaded
    entry-point object) and registers providers through ``register_provider``. It may not
    replace a provider another module registered."""
    target = _plugin_module(name)
    register = getattr(target, "register_from_config", None)
    if register is None and callable(target):
        register = target
    if register is None:
        raise ValueError(f"Provider plugin {name!r} has no register_from_config")
    before = set(_PROVIDERS)
    try:
        register(section)
    except BaseException:
        # A failed plugin leaves nothing half-registered behind.
        for provider_id in set(_PROVIDERS) - before:
            _PROVIDERS.pop(provider_id, None)
        raise
    added = set(_PROVIDERS) - before
    for provider_id in added:
        _PLUGIN_MODULES[provider_id] = name
    if not added and not any(v == name for v in _PLUGIN_MODULES.values()):
        raise ValueError(f"Provider plugin {name!r} registered no provider")


def plugin_of(provider_id: str) -> str | None:
    """The plugin name that registered the provider, or None for a built-in."""
    return _PLUGIN_MODULES.get(provider_id)


PROVIDERS_FILE_SCHEMA = "tessera.campaign-providers.v1"
MAX_PROVIDERS_FILE_BYTES = 262144


def load_providers_file(path: Any) -> dict[str, Any]:
    """Read an owner-only (0600, single-link, no symlink) ``--campaign-providers`` file."""
    from .salad_live_check import _private_bytes
    from .salad_workload_jwt import strict_json

    try:
        document = strict_json(_private_bytes(path, MAX_PROVIDERS_FILE_BYTES))
    except (OSError, ValueError):
        document = None
    if not isinstance(document, dict) or document.get("schema") != PROVIDERS_FILE_SCHEMA:
        raise ValueError(
            "Campaign providers must be an owner-only (0600) absolute JSON file holding "
            f"a {PROVIDERS_FILE_SCHEMA} document."
        )
    return document


def configure_providers(config: dict[str, Any] | None) -> None:
    """Load the built-in provider modules and register each from the operator document.

    Then install the operator's ``placement_preferences`` (ADR 0070); absent clears them."""
    from . import campaign_placement_preference

    section = (config or {}).get("placement_preferences")
    campaign_placement_preference.parse(section)  # refuse a malformed section before effects
    load_builtin_providers(lambda name: importlib.import_module(name).register_from_config(config))
    for name, section_ in sorted(((config or {}).get("plugins") or {}).items()):
        load_plugin(name, section_)
    campaign_placement_preference.configure(section, provider_ids())
