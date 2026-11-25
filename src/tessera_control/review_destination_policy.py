"""Operator-attested review destination policy (ADR 0084 section 2, ADR 0047 amendment).

A review job names a model destination. Without operator configuration the control plane keeps
``NoDestinationsPolicy`` and refuses every submit. An operator enables destinations with an
owner-held file (``tessera serve --review-destinations FILE``, env fallback
``TESSERA_REVIEW_DESTINATIONS``) in this form::

    {
      "schema": "tessera.review-destinations.v1",
      "destinations": {
        "<destination id>": {
          "kind": "model_api",
          "data_classes": ["public", "private_derived"],
          "tiers": ["haiku", "sonnet", "opus"],
          "max_budget_cents": 500,
          "allowed_license_labels": ["unlabelled", "cc-by"]
        }
      }
    }

``kind`` and ``data_classes`` are required; ``tiers``, ``max_budget_cents`` and
``allowed_license_labels`` are optional. No destination or model is named in this module.

Fail closed, in every case:

* A destination id must be registered in the code-reviewed model destination registry
  (``tessera_agent.model_destinations``) with a non-empty model-runtime host allowlist that
  Tessera's own transport enforces. An unregistered id, or a registered one Tessera does not
  reach through its own allowlist, is refused when the file is loaded, so the file can never
  name a public-internet or unregistered destination.
* ``kind`` is a closed set (``model_api``); anything else is refused at load.
* A data class reaches a destination only when that destination lists it. There is no
  wildcard and no default class: ``private_code_approved`` and ``private_derived`` must each be
  listed explicitly. A class this module does not know is refused, in the file and at check.
* Absent ``tiers`` admits every tier (the job API's own tier rules, such as human approval of
  the highest tier, still apply); present, only the listed tiers.
* Absent ``max_budget_cents`` leaves only the API cap; present, a larger job budget is refused.
* Licence labels (for example ``non-commercial``) are restrictive by default: a destination
  receives only ``unlabelled`` inputs unless it lists the labels it may receive, and
  ``unknown-lineage`` can never be listed. ``check`` does not receive labels; callers use
  ``license_labels_refusal``.

A file that is a symlink, not a regular file, not owned by the service user, group- or
world-writable, oversized or malformed is refused at load (``ReviewDestinationsRefused``, a
ValueError), and ``tessera serve`` exits 2, as for the provider trust file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = "tessera.review-destinations.v1"
ENVIRONMENT_VARIABLE = "TESSERA_REVIEW_DESTINATIONS"
MAX_FILE_BYTES = 64 * 1024
MAX_DESTINATIONS = 16
# The API caps a job budget at 10000 cents; a policy cap above that would never bind.
MAX_BUDGET_CENTS = 10000
# A model API reached only through Tessera's own enforced model-runtime allowlist.
KIND_MODEL_API = "model_api"
KINDS = (KIND_MODEL_API,)
# Data classes known to the platform (ADR 0039, ADR 0040 section 4). Anything else is refused.
DATA_CLASSES = ("public", "private_code_approved", "private_derived")
_DESTINATION_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TIER = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_REQUIRED_KEYS = frozenset({"kind", "data_classes"})
_OPTIONAL_KEYS = frozenset({"tiers", "max_budget_cents", "allowed_license_labels"})
MAX_TIERS = 16
MAX_LICENSE_LABELS = 16

# Reason codes, all from review_jobs.REFUSAL_REASONS.
DESTINATION_NOT_ENROLLED = "destination_not_enrolled"
TIER_EXCEEDS_POLICY = "tier_exceeds_policy"
DATA_CLASS_NOT_ALLOWED = "data_class_not_allowed_for_destination"
BUDGET_EXCEEDS_POLICY = "budget_exceeds_policy"


class ReviewDestinationsRefused(ValueError):
    """The review destinations file is unreadable, too permissive or malformed."""


def _registered(destination_id: str) -> bool:
    """Registered in the model destination registry and reached only through Tessera's own
    enforced model-runtime allowlist (a registry entry without hosts is not admitted)."""
    from tessera_agent.model_destinations import DESTINATIONS

    found = DESTINATIONS.get(destination_id)
    return found is not None and bool(found.model_runtime_hosts)


def _unique_strings(value: Any, limit: int, pattern=None, allowed=None) -> bool:
    return (
        isinstance(value, list)
        and 1 <= len(value) <= limit
        and all(isinstance(item, str) for item in value)
        and len(set(value)) == len(value)
        and (pattern is None or all(pattern.fullmatch(item) for item in value))
        and (allowed is None or all(item in allowed for item in value))
    )


def _canonical(value: Any) -> dict[str, Any]:
    """Validate a destinations document and return its canonical form."""
    from .license_labels import UNKNOWN_LINEAGE, valid_label

    if not isinstance(value, dict) or set(value) != {"schema", "destinations"}:
        raise ReviewDestinationsRefused(
            "review destinations file must have exactly the keys schema and destinations"
        )
    if value["schema"] != SCHEMA:
        raise ReviewDestinationsRefused(f"review destinations schema must be {SCHEMA}")
    destinations = value["destinations"]
    if not isinstance(destinations, dict) or len(destinations) > MAX_DESTINATIONS:
        raise ReviewDestinationsRefused(
            f"review destinations must be an object of at most {MAX_DESTINATIONS} entries"
        )
    canonical: dict[str, Any] = {}
    for destination_id, entry in sorted(destinations.items()):
        if not isinstance(destination_id, str) or not _DESTINATION_ID.fullmatch(destination_id):
            raise ReviewDestinationsRefused("review destination id is invalid")
        if not _registered(destination_id):
            raise ReviewDestinationsRefused(
                f"review destination {destination_id!r} is not a registered model destination "
                "with an enforced model-runtime allowlist"
            )
        if (
            not isinstance(entry, dict)
            or not _REQUIRED_KEYS <= set(entry)
            or set(entry) - _REQUIRED_KEYS - _OPTIONAL_KEYS
        ):
            raise ReviewDestinationsRefused(
                f"review destination {destination_id!r} must have kind and data_classes "
                f"(and may have {sorted(_OPTIONAL_KEYS)})"
            )
        if entry["kind"] not in KINDS:
            raise ReviewDestinationsRefused(
                f"review destination {destination_id!r} kind must be one of {list(KINDS)}"
            )
        if not _unique_strings(entry["data_classes"], len(DATA_CLASSES), allowed=DATA_CLASSES):
            raise ReviewDestinationsRefused(
                f"review destination {destination_id!r} data_classes must be 1 or more unique "
                f"values of {list(DATA_CLASSES)}"
            )
        item: dict[str, Any] = {
            "kind": entry["kind"],
            "data_classes": [c for c in DATA_CLASSES if c in entry["data_classes"]],
        }
        if "tiers" in entry:
            if not _unique_strings(entry["tiers"], MAX_TIERS, pattern=_TIER):
                raise ReviewDestinationsRefused(
                    f"review destination {destination_id!r} tiers must be 1 to {MAX_TIERS} "
                    "unique lowercase tier names"
                )
            item["tiers"] = sorted(entry["tiers"])
        if "max_budget_cents" in entry:
            cap = entry["max_budget_cents"]
            if type(cap) is not int or not 0 <= cap <= MAX_BUDGET_CENTS:
                raise ReviewDestinationsRefused(
                    f"review destination {destination_id!r} max_budget_cents must be an "
                    f"integer from 0 to {MAX_BUDGET_CENTS}"
                )
            item["max_budget_cents"] = cap
        if "allowed_license_labels" in entry:
            labels = entry["allowed_license_labels"]
            if (
                not _unique_strings(labels, MAX_LICENSE_LABELS)
                or not all(valid_label(label) for label in labels)
                or UNKNOWN_LINEAGE in labels
            ):
                raise ReviewDestinationsRefused(
                    f"review destination {destination_id!r} allowed_license_labels must be 1 "
                    f"to {MAX_LICENSE_LABELS} unique lowercase label slugs "
                    f"(not {UNKNOWN_LINEAGE!r})"
                )
            item["allowed_license_labels"] = sorted(labels)
        canonical[destination_id] = item
    return {"schema": SCHEMA, "destinations": canonical}


def _digest(document: dict[str, Any]) -> str:
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ReviewDestinationPolicy:
    """A loaded, validated policy; ``digest`` is the sha256 of its canonical document."""

    digest: str
    document: dict[str, Any]

    def check(self, destination_id, tier, data_classes, budget_cents) -> str | None:
        """None admits; otherwise a ``review_jobs.REFUSAL_REASONS`` code. Malformed arguments
        refuse rather than raise."""
        entry = (
            self.document["destinations"].get(destination_id)
            if isinstance(destination_id, str)
            else None
        )
        if entry is None:
            return DESTINATION_NOT_ENROLLED
        if not isinstance(tier, str) or ("tiers" in entry and tier not in entry["tiers"]):
            return TIER_EXCEEDS_POLICY
        if isinstance(data_classes, str | bytes) or not isinstance(data_classes, Iterable):
            return DATA_CLASS_NOT_ALLOWED
        classes = list(data_classes)
        # No inputs means nothing was classified: refuse rather than admit by default.
        if not classes or not all(
            isinstance(c, str) and c in DATA_CLASSES and c in entry["data_classes"] for c in classes
        ):
            return DATA_CLASS_NOT_ALLOWED
        if type(budget_cents) is not int or budget_cents < 0:
            return BUDGET_EXCEEDS_POLICY
        if "max_budget_cents" in entry and budget_cents > entry["max_budget_cents"]:
            return BUDGET_EXCEEDS_POLICY
        return None

    def license_labels_refusal(self, destination_id, labels) -> str | None:
        """None when every effective licence label of the inputs may go to the destination.

        Without ``allowed_license_labels`` a destination receives only ``unlabelled`` inputs;
        ``unknown-lineage`` is never admitted. Not called by ``check`` (its interface carries no
        labels); a caller that knows the inputs' labels calls it after ``check``."""
        from .license_labels import UNLABELLED

        entry = (
            self.document["destinations"].get(destination_id)
            if isinstance(destination_id, str)
            else None
        )
        if entry is None:
            return DESTINATION_NOT_ENROLLED
        allowed = frozenset(entry.get("allowed_license_labels", (UNLABELLED,)))
        if isinstance(labels, str | bytes) or not isinstance(labels, Iterable):
            return DATA_CLASS_NOT_ALLOWED
        found = list(labels) or [UNLABELLED]
        if not all(isinstance(label, str) and label in allowed for label in found):
            return DATA_CLASS_NOT_ALLOWED
        return None

    def show(self) -> dict[str, Any]:
        """The canonical document and its digest (no secrets: the file holds none)."""
        return {"digest": self.digest, **self.document}


def policy_from_document(value: Any) -> ReviewDestinationPolicy:
    document = _canonical(value)
    return ReviewDestinationPolicy(_digest(document), document)


def load_review_destination_policy(path: Path | str) -> ReviewDestinationPolicy:
    """Check permissions and parse; raises ReviewDestinationsRefused.

    The same file checks as the provider trust file: refuses a symlink, a file that is not a
    regular file owned by the service user, a group- or world-writable file, a file replaced
    while it was opened, and an oversized or malformed file."""
    path = Path(path)
    try:
        info = os.lstat(path)
    except OSError:
        raise ReviewDestinationsRefused("review destinations file cannot be read") from None
    if stat.S_ISLNK(info.st_mode):
        raise ReviewDestinationsRefused("review destinations file must not be a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise ReviewDestinationsRefused("review destinations file must be a regular file")
    if info.st_uid != os.geteuid():
        raise ReviewDestinationsRefused(
            "review destinations file must be owned by the service user"
        )
    if info.st_mode & 0o022:
        raise ReviewDestinationsRefused(
            "review destinations file must not be group- or world-writable"
        )
    try:
        # O_NOFOLLOW closes the window between lstat and open.
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise ReviewDestinationsRefused("review destinations file cannot be read") from None
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ReviewDestinationsRefused("review destinations file changed while it was read")
        if opened.st_size > MAX_FILE_BYTES:
            raise ReviewDestinationsRefused("review destinations file is too large")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_FILE_BYTES:
        raise ReviewDestinationsRefused("review destinations file is too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ReviewDestinationsRefused("review destinations file is not valid JSON") from None
    return policy_from_document(value)


class EnvironmentReviewDestinationPolicy:
    """The policy named by ``TESSERA_REVIEW_DESTINATIONS``, for a runner's ``--policy
    module:attribute`` option (a class taking no arguments). Raises ReviewDestinationsRefused
    when the variable is unset or the file is refused, so the runner does not start."""

    def __init__(self) -> None:
        path = os.environ.get(ENVIRONMENT_VARIABLE)
        if not path:
            raise ReviewDestinationsRefused(f"{ENVIRONMENT_VARIABLE} is not set")
        self._policy = load_review_destination_policy(path)
        self.digest = self._policy.digest

    def check(self, destination_id, tier, data_classes, budget_cents):
        return self._policy.check(destination_id, tier, data_classes, budget_cents)

    def license_labels_refusal(self, destination_id, labels):
        return self._policy.license_labels_refusal(destination_id, labels)
