"""Deploy check, config area: find and drop config keys a release removed (ADR 0098).

The executor and live-check configs are closed: a key the loader no longer knows makes it
refuse the whole file. After an upgrade that is the one config edit a deploy needs, so the
check names it and ``apply_drop_removed_keys`` makes it, and only it, with a receipt.

Only the one named file is read. No path named inside it is opened and no value from it is
printed or recorded; findings and receipts carry key names, field locations and digests.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .campaign_salad_executor import CONFIG_SCHEMA, ExecutorConfig
from .deploy_check import (
    Finding,
    Fix,
    require_writable_directory,
    sha256_hex,
    write_private_json,
    write_receipt,
)
from .salad_live_check import SOURCE_PIN_SHAPE, installed_source_pin, is_source_pin

AREA = "config"
MAX_CONFIG_BYTES = 65536
LIVE_CONFIG_SCHEMA = "tessera.salad.live-config.v1"

_REMOVED_CANCEL = "removed by ADR 0075: the executor's worker credential cancels its own routes"
REMOVED_KEYS: dict[str, dict[str, str]] = {
    CONFIG_SCHEMA: {"cancel_credential_file": _REMOVED_CANCEL},
    LIVE_CONFIG_SCHEMA: {"cancel_credential_file": _REMOVED_CANCEL},
}

# The live config is validated by hand-written code in ``salad_live_check._config``; this copy
# is checked against that code in the tests so the two cannot drift.
LIVE_CONFIG_KEYS = frozenset(
    {
        "schema",
        "state_dir",
        "route_id",
        "task_id",
        "org_id",
        "project_id",
        "proposal_digest",
        "policy",
        "worker_credential_file",
        "credential_directory",
        "guardian_socket",
        "guardian_uid",
        "custody",
        "approved_max_cost_cents",
        "max_seconds",
        "max_polls",
        "source_revision",
    }
)


def _executor_keys() -> frozenset[str]:
    keys: set[str] = set()
    for name, info in ExecutorConfig.model_fields.items():
        keys.add(name)
        if info.alias:
            keys.add(info.alias)
    return frozenset(keys)


def known_keys(schema: object) -> frozenset[str] | None:
    if schema == CONFIG_SCHEMA:
        return _executor_keys()
    if schema == LIVE_CONFIG_SCHEMA:
        return LIVE_CONFIG_KEYS
    return None


def _strict_loads(raw: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def reject(_: str) -> Any:
        raise ValueError("invalid constant")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=reject)


def _read(path: Path, *, owner_only: bool = False) -> bytes:
    """Read one regular, non-symlink file; raises ValueError for anything else."""
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError("The config path must be absolute.")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise ValueError("The config file cannot be opened as a regular file.") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("The config path must be a regular file.")
        if info.st_size > MAX_CONFIG_BYTES:
            raise ValueError("The config file is too large.")
        if owner_only and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ValueError("The config file must be owner-only and owned by this user.")
        data = os.read(descriptor, MAX_CONFIG_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(data) > MAX_CONFIG_BYTES:
        raise ValueError("The config file is too large.")
    return data


def _parse(raw: bytes) -> dict[str, Any]:
    try:
        document = _strict_loads(raw)
    except (ValueError, RecursionError):
        raise ValueError("The config file is not strict JSON.") from None
    if not isinstance(document, dict):
        raise ValueError("The config file must hold a JSON object.")
    return document


def _validation_detail(error: ValidationError) -> list[dict[str, str]]:
    return [
        {"loc": ".".join(str(part) for part in item["loc"]), "type": item["type"]}
        for item in error.errors(include_input=False, include_url=False)
    ]


def check_config(path: Path, *, label: str | None = None) -> list[Finding]:
    name = label if label is not None else Path(str(path)).name
    check_id = f"config.{name}.keys"

    def fail(summary: str, detail: dict[str, Any] | None = None, fix: Fix | None = None):
        return [Finding(check_id, AREA, "fail", summary, detail or {}, fix)]

    try:
        document = _parse(_read(path))
    except ValueError as error:
        return fail(str(error))
    schema = document.get("schema")
    known = known_keys(schema)
    if known is None:
        return fail("The config schema is missing or not one this release knows.")
    table = REMOVED_KEYS.get(schema, {})  # type: ignore[arg-type]
    removed = sorted(key for key in document if key in table)
    unknown = sorted(key for key in document if key not in known and key not in table)
    if removed:
        return fail(
            "The config names keys a release removed; the loader refuses the file.",
            {"removed_keys": removed, "notes": {key: table[key] for key in removed}},
            Fix(
                "drop_removed_keys",
                "Drop the removed keys from the config file (receipted).",
                automatic=True,
            ),
        )
    if unknown:
        return fail(
            "The config names keys this release does not know; the loader refuses the file.",
            {"unknown_keys": unknown},
            Fix(
                "edit_unknown_keys",
                "Remove the unknown keys by hand; they are never dropped automatically.",
                automatic=False,
                command="edit the file to drop the key",
            ),
        )
    findings = [Finding(check_id, AREA, "ok", "The config names only keys this release knows.")]
    if schema == CONFIG_SCHEMA:
        pin = check_source_pin(document, name)
        findings.append(pin)
        errors = _model_errors(document, refreshable=pin.fix is REFRESH_SOURCE_PIN)
        if errors:
            findings.append(
                Finding(
                    f"config.{name}.valid",
                    AREA,
                    "fail",
                    "The config does not validate against the executor config model.",
                    {"errors": errors},
                )
            )
    return findings


# A well-formed pin that stands in for a refreshable source_revision, so the rest of the config
# (field and model-level checks the pin's error would otherwise hide) is still validated.
_PLACEHOLDER_PIN = "0" * 40 + "+sha256:" + "0" * 64


def _model_errors(document: dict[str, Any], *, refreshable: bool) -> list[dict[str, str]]:
    """The executor config model's errors. When the source pin finding already carries the
    refresh fix, an error on ``source_revision`` alone is left to that finding; every other
    error, alone or alongside it, is still reported and still blocks."""
    try:
        ExecutorConfig.model_validate(document)
    except ValidationError as error:
        errors = _validation_detail(error)
    else:
        return []
    if not refreshable or any(item["loc"] != "source_revision" for item in errors):
        return errors
    try:
        ExecutorConfig.model_validate(dict(document, source_revision=_PLACEHOLDER_PIN))
    except ValidationError as rest:
        return errors + _validation_detail(rest)
    return []


def apply_drop_removed_keys(path: Path, receipt_dir: Path) -> dict[str, Any] | None:
    # The receipt directory is proven writable before the config is touched: an applied fix
    # always has its receipt.
    require_writable_directory(receipt_dir, "receipt directory")
    raw = _read(path, owner_only=True)
    document = _parse(raw)
    schema = document.get("schema")
    table = REMOVED_KEYS.get(schema) if isinstance(schema, str) else None
    if not table:
        return None
    removed = sorted(key for key in document if key in table)
    if not removed:
        return None
    result = {key: value for key, value in document.items() if key not in table}
    write_private_json(path, result)
    after = (json.dumps(result, sort_keys=True, indent=2) + "\n").encode()
    return write_receipt(
        receipt_dir,
        "drop_removed_keys",
        {
            "config_schema": schema,
            "removed_keys": removed,
            "before_sha256": sha256_hex(raw),
            "after_sha256": sha256_hex(after),
            "notes": {key: table[key] for key in removed},
        },
    )


REFRESH_SOURCE_PIN = Fix(
    "refresh_source_pin",
    "Pin source_revision to the installed source (receipted).",
    automatic=True,
    command="tessera deploy-check --apply refresh_source_pin --source-commit <40-hex commit>",
)


def check_source_pin(document: dict[str, Any], name: str) -> Finding:
    """Whether the executor's ``source_revision`` is the pin the live check accepts now: the
    exact shape and the digest of the installed source. Values are never reported."""
    check_id = f"config.{name}.source_pin"
    value = document.get("source_revision")
    if not is_source_pin(value):
        return Finding(
            check_id,
            AREA,
            "fail",
            "source_revision is not the pin the live check requires; every route would stop "
            "at the live-check start.",
            {"required_shape": SOURCE_PIN_SHAPE},
            REFRESH_SOURCE_PIN,
        )
    try:
        current = installed_source_pin(value.split("+sha256:")[0])
    except ValueError as error:
        return Finding(check_id, AREA, "fail", str(error))
    if value != current:
        return Finding(
            check_id,
            AREA,
            "fail",
            "source_revision does not pin the installed source; every route would stop at the "
            "live-check start.",
            {},
            REFRESH_SOURCE_PIN,
        )
    return Finding(check_id, AREA, "ok", "source_revision pins the installed source.")


def apply_refresh_source_pin(
    path: Path, receipt_dir: Path, commit: str, *, root: Path | None = None
) -> dict[str, Any] | None:
    """Rewrite an executor config's ``source_revision`` to ``commit`` pinned to the installed
    source (``root`` defaults to it), atomically and owner-only, with a receipt. Refuses a
    source that cannot be measured before anything is written; ``None`` when already pinned."""
    require_writable_directory(receipt_dir, "receipt directory")
    pin = installed_source_pin(commit, root)
    raw = _read(path, owner_only=True)
    mode = stat.S_IMODE(path.stat().st_mode)
    document = _parse(raw)
    if document.get("schema") != CONFIG_SCHEMA:
        raise ValueError("refresh_source_pin applies only to an executor config.")
    if document.get("source_revision") == pin:
        return None
    result = dict(document, source_revision=pin)
    write_private_json(path, result, mode=mode)
    after = (json.dumps(result, sort_keys=True, indent=2) + "\n").encode()
    return write_receipt(
        receipt_dir,
        "refresh_source_pin",
        {
            "config_schema": CONFIG_SCHEMA,
            "before_sha256": sha256_hex(raw),
            "after_sha256": sha256_hex(after),
            "source_revision": pin,
        },
    )
