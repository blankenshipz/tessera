"""The automation identity an owner-run helper acts under.

A CLI or helper that holds the owner credential is automation, not a person at a console. It says so
on every request: ``X-Tessera-Automation`` (a label) and, when it acts under a named owner
delegation, ``X-Tessera-Delegation`` (a reference). Both are labels: they grant nothing, they only
make the control plane record the action as delegated automation instead of an unlabelled owner
call. Nothing here reads, prints or stores a credential value.

Label precedence: the ``--automation-label`` flag, then ``TESSERA_AUTOMATION_LABEL``, then
``tessera-cli:<command>``. The delegation reference comes only from ``--delegation-ref`` or
``TESSERA_DELEGATION_REF``; it is never invented.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .approval_trust import (
    AUTOMATION_HEADER,
    DELEGATION_HEADER,
    valid_automation_label,
)

LABEL_ENV = "TESSERA_AUTOMATION_LABEL"
DELEGATION_ENV = "TESSERA_DELEGATION_REF"
DEFAULT_LABEL_PREFIX = "tessera-cli:"


class IdentityError(ValueError):
    """A label or delegation reference is not acceptable; the message never echoes the value."""


@dataclass(frozen=True)
class OwnerIdentity:
    label: str
    delegation_ref: str | None = None


def _checked(value: str, what: str) -> str:
    if not valid_automation_label(value):
        raise IdentityError(
            f"The {what} must be a non-empty printable label of at most 128 characters "
            "with no leading or trailing space."
        )
    return value


def resolve_identity(
    arguments: Any, command: str, environ: Mapping[str, str] | None = None
) -> OwnerIdentity:
    """The identity for one command: flag, then environment, then the default label."""
    env = os.environ if environ is None else environ
    label = getattr(arguments, "automation_label", None) or env.get(LABEL_ENV)
    if not label:
        label = DEFAULT_LABEL_PREFIX + command
    reference = getattr(arguments, "delegation_ref", None) or env.get(DELEGATION_ENV) or None
    return OwnerIdentity(
        _checked(label, "automation label"),
        _checked(reference, "delegation reference") if reference else None,
    )


def identity_headers(identity: OwnerIdentity | None) -> dict[str, str]:
    if identity is None:
        return {}
    headers = {AUTOMATION_HEADER: identity.label}
    if identity.delegation_ref:
        headers[DELEGATION_HEADER] = identity.delegation_ref
    return headers


def owner_request_headers(
    authorization: str, identity: OwnerIdentity, key: str | None = None
) -> dict[str, str]:
    """Headers for an external owner-run helper (for example a snapshot upload script)."""
    headers = {"Authorization": authorization, "Content-Type": "application/json"}
    if key:
        headers["Idempotency-Key"] = key
    return headers | identity_headers(identity)
