"""Neutral registry of remote compute targets.

A new compute vendor registers one ``TargetSpec`` here; the generic routing core
(``remote_execution``, ``salad_server``) never branches on a vendor name. The
optional ``admission_hook`` runs inside the live-authority transaction on
create/start and returns evidence (for example a budget binding) or ``None``.
This module imports no vendor module at import time.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from pydantic import AfterValidator

# (db, live, proposal, operation) -> dict | None
AdmissionHook = Callable[..., "dict | None"]


@dataclass(frozen=True)
class TargetSpec:
    name: str
    admission_hook: AdmissionHook | None = None
    # A billable (paid-provider) target must carry an admission hook; the routing
    # core refuses create/start for a billable target without one (fail closed).
    billable: bool = False
    # Compute operated by a third party (a rented GPU marketplace, for example). Inputs
    # reach any target only as the provider trust setting allows (ADR 0040 section 4,
    # ``data_class.require_provider_input``) at route admission and at ticket issue.
    third_party_compute: bool = False
    # The provider id the operator's trust setting (ADR 0040 section 4) knows this target
    # by. Defaults to the target name; both Salad targets use ``salad``.
    provider_id: str | None = None

    def __post_init__(self) -> None:
        if self.provider_id is None:
            object.__setattr__(self, "provider_id", self.name)


_TARGETS: dict[str, TargetSpec] = {}


def register_target(spec: TargetSpec) -> TargetSpec:
    if not isinstance(spec, TargetSpec) or not isinstance(spec.name, str) or not spec.name:
        raise ValueError("TargetSpec with a non-empty name required")
    if spec.billable and spec.admission_hook is None:
        raise ValueError(f"Billable target requires an admission hook: {spec.name}")
    if spec.name in _TARGETS:
        raise ValueError(f"Target already registered: {spec.name}")
    _TARGETS[spec.name] = spec
    return spec


def target_names() -> tuple[str, ...]:
    return tuple(_TARGETS)


def get_target(name: str) -> TargetSpec:
    try:
        return _TARGETS[name]
    except (KeyError, TypeError):
        raise KeyError(f"Unknown remote target: {name!r}") from None


def _validate_target(value: str) -> str:
    if value not in _TARGETS:
        raise ValueError(f"unknown remote target; expected one of {sorted(_TARGETS)}")
    return value


# Validated string: accepts only registered names, serializes as a plain string.
Target = Annotated[str, AfterValidator(_validate_target)]


def _salad_container_admission(db, live, proposal, operation):
    from .experiment_budgets import salad_budget_binding

    return salad_budget_binding(db, live, proposal, operation)


register_target(TargetSpec("kubernetes"))
register_target(
    TargetSpec(
        "salad_container",
        admission_hook=_salad_container_admission,
        billable=True,
        third_party_compute=True,
        provider_id="salad",
    )
)
register_target(
    TargetSpec(
        "salad_inference",
        admission_hook=_salad_container_admission,
        billable=True,
        third_party_compute=True,
        provider_id="salad",
    )
)


def require_admission_hook(name: str) -> AdmissionHook:
    """Hook for a paid path; a billable target without one is refused (fail closed)."""
    spec = get_target(name)
    if spec.admission_hook is None:
        raise ValueError(f"Target {name!r} has no admission hook; paid execution refused.")
    return spec.admission_hook
