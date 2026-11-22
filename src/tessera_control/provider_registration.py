"""Validated registration of compute providers and their profiles (ADR 0081).

The operator file (``--campaign-providers``, ``tessera.campaign-providers.v1``) was edited by
hand. This module is the one validated way to add, update and list its entries, shared by the
``tessera providers`` command and the control API (and so the MCP tools). It knows no vendor:
each provider module, built in or a plugin, describes where its entries live and how they are
checked through ``config_sections()`` (``campaign_providers.ConfigSection``). A vendor therefore
adds a section and never touches this module or the runner.

What a change must pass before a byte is written:

* the section is one a loaded provider declares (an unknown provider, section or plugin is
  refused: nothing is registered for a destination Tessera cannot name);
* the entry validates against that provider's own closed model (image allowlist as digests,
  resource shape, cost caps, credential *reference*, custody host);
* the whole resulting document validates again (unique names, reserved ids, schema);
* the entry holds no secret: no secret-like key name (a credential reference is a name, not a
  key) and no credential-shaped or high-entropy value;
* the file has not changed since the digest the caller read (optimistic concurrency).

The write is atomic and owner-only, and appends to a hash-chained audit file next to it.
Nothing here registers a provider in a running process: ``serve`` reads the file at start, so a
change applies at the next start. Provider trust (which data classes a worker may receive) stays
the operator's own policy file; a result only reports what the current setting would admit and
the exact entry to add.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib
import importlib.metadata
import json
import os
import re
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from . import campaign_providers as providers
from .campaign_providers import ConfigSection
from .salad_workload_contract import (
    _BASE64ISH,
    _CREDENTIAL_PREFIXES,
    _HEX,
    _SECRET_WORDS,
    _entropy,
)

FILE_SCHEMA = providers.PROVIDERS_FILE_SCHEMA
AUDIT_SUFFIX = ".audit.jsonl"
PLUGIN_SECTION = "plugin"
ACTIONS = ("add", "update")
# Field names that hold a *reference* (a name resolved elsewhere), never a secret value.
REFERENCE_KEYS = frozenset({"credential_reference", "credential_version"})
MAX_ENTRY_BYTES = 65536
# A generated identifier (``project_<hex>``, ``worker-<hex>``) is a name, not a token.
_ID_VALUE = re.compile(r"^[A-Za-z][A-Za-z0-9]*[_.:-][0-9a-fA-F-]{16,}$")
# Whole words of a field name that mean a secret value (a name like ``quoted_passage`` is not one).
SECRET_NAME_WORDS = frozenset(_SECRET_WORDS) | {"PASSWORD", "CREDENTIALS", "APIKEY"}


class RegistrationRefused(ValueError):
    """A change was refused; the message says what to fix. Nothing was written."""


@dataclass(frozen=True)
class Change:
    action: str
    section: str
    key: str | None
    document: dict[str, Any]
    before_digest: str
    after_digest: str
    changed_fields: tuple[str, ...]
    entry: dict[str, Any]


def digest_of(document: Mapping[str, Any]) -> str:
    raw = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def empty_document() -> dict[str, Any]:
    return {"schema": FILE_SCHEMA}


# -- sections --


def _module_sections(module: Any) -> tuple[ConfigSection, ...]:
    hook = getattr(module, "config_sections", None)
    return tuple(hook()) if callable(hook) else ()


def resolve_plugin(name: str, allow_module: bool) -> Any:
    """The plugin module/object for ``name``, or RegistrationRefused.

    An installed entry point always resolves. A bare dotted module resolves only when
    ``allow_module`` (the offline CLI, run by the file's owner) or when it is already imported in
    this process: an API caller must not make the running service import arbitrary code."""
    if not providers.PLUGIN_NAME_RE.match(name):
        raise RegistrationRefused(f"Invalid provider plugin name {name!r}.")
    try:
        for entry in importlib.metadata.entry_points(group=providers.ENTRY_POINT_GROUP):
            if entry.name == name:
                return entry.load()
        if allow_module or name in sys.modules:
            return importlib.import_module(name)
    except Exception as error:  # a plugin's own import error is a refusal, never a crash
        raise RegistrationRefused(
            f"Provider plugin {name!r} failed to load: {type(error).__name__}"
        ) from None
    raise RegistrationRefused(
        f"Provider plugin {name!r} is not an installed entry point"
        + ("" if allow_module else " (the API registers only installed plugins)")
        + "."
    )


def sections(
    document: Mapping[str, Any] | None = None, allow_module: bool = False
) -> dict[str, ConfigSection]:
    """Every section a loaded provider declares: the built-in modules, plus each plugin the
    document names (resolved like ``--campaign-providers`` does; nothing else is imported)."""
    import importlib

    found: dict[str, ConfigSection] = {}
    for name in providers.BUILTIN_MODULES:
        for section in _module_sections(importlib.import_module(name)):
            found[section.name] = section
    for plugin in sorted((document or {}).get("plugins") or {}):
        try:
            module = resolve_plugin(plugin, allow_module)
        except RegistrationRefused:
            continue  # reported when that plugin's own section is asked for
        for section in _module_sections(module):
            if section.name in found:
                raise RegistrationRefused(f"Plugin {plugin!r} redefines section {section.name!r}")
            found[section.name] = section
    return found


def known_section_names(
    document: Mapping[str, Any] | None = None, allow_module: bool = False
) -> list[str]:
    return sorted([*sections(document, allow_module), PLUGIN_SECTION])


# -- secrets --


_SAFE_PATH = re.compile(r"^[A-Za-z0-9_.\[\]-]{0,64}$")
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_USERINFO = re.compile(r"://[^/\s@]*:[^/\s@]+@")
_BEARER = re.compile(r"^\s*Bearer\s+\S{8,}", re.IGNORECASE)
_JWT = re.compile(r"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$")
_WORDS_LOWER = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)+$")  # a human-readable name, not a token
SECRET_NAME_SUBSTRINGS = ("PASSPHRASE", "PASSWD", "PASSWORD", "APIKEY", "ACCESSKEY", "PRIVATEKEY")
# A field about the *amount* of something (max_tokens, token_budget) is not a token value.
QUANTITY_WORDS = frozenset({"MAX", "MIN", "BUDGET", "LIMIT", "COUNT", "TTL", "SECONDS", "CENTS"})


def _secret_name(key: str) -> bool:
    if key in REFERENCE_KEYS:
        return False
    spaced = _CAMEL.sub(r"\1_\2", key).upper()
    words = {w for w in re.split(r"[^A-Z0-9]+", spaced) if w}
    joined = re.sub(r"[^A-Z0-9]+", "", spaced)
    if any(sub in joined for sub in SECRET_NAME_SUBSTRINGS):
        return True
    hit = words & SECRET_NAME_WORDS
    return bool(hit) and not (hit == {"TOKEN"} and words & QUANTITY_WORDS)


def secret_findings(value: Any, where: str = "") -> list[str]:
    """Paths of anything that looks like a secret in a config value (names only, never values)."""
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            path = f"{where}.{key}" if where else str(key)
            if _secret_name(str(key)):
                found.append(f"{path}: secret-like field name")
            found += secret_findings(item, path)
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            found += secret_findings(item, f"{where}[{index}]")
    elif isinstance(value, str):
        if value.startswith(_CREDENTIAL_PREFIXES):
            found.append(f"{where}: credential-shaped value")
        elif _USERINFO.search(value) or _BEARER.match(value) or _JWT.fullmatch(value):
            found.append(f"{where}: credential-shaped value")
        elif (
            _BASE64ISH.fullmatch(value)
            and not _HEX.fullmatch(value)
            and not _ID_VALUE.fullmatch(value)
            and not _WORDS_LOWER.fullmatch(value)
            and _entropy(value) >= 4.0
        ):
            found.append(f"{where}: high-entropy token-like value")
    return found


# -- document navigation --


def _get(document: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = document
    for part in path:
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _set(document: dict[str, Any], path: tuple[str, ...], value: Any, section: ConfigSection):
    node = document
    for index, part in enumerate(path[:-1]):
        child = node.get(part)
        if child is None:
            child = node[part] = (
                section.container() if index == 0 and section.container else {}
            ) or {}
        node = child
    node[path[-1]] = value


def entries_of(document: Mapping[str, Any], section: ConfigSection) -> list[dict[str, Any]]:
    value = _get(document, section.path)
    if value is None:
        return []
    return [dict(value)] if section.key_field is None else [dict(v) for v in value]


def list_document(document: Mapping[str, Any], allow_module: bool = False) -> list[dict[str, Any]]:
    """Non-secret view of every entry: section, provider, key and the section's own summary."""
    rows: list[dict[str, Any]] = []
    for section in sections(document, allow_module).values():
        for entry in entries_of(document, section):
            key = None if section.key_field is None else entry.get(section.key_field)
            rows.append(
                {
                    "section": section.name,
                    "provider": section.provider_of(entry)
                    if section.provider_of
                    else section.provider,
                    "scope": section.scope,
                    "key": key,
                    "worker_principal_id": section.worker(entry),
                    "org_id": entry.get("org_id"),
                    **section.describe(entry),
                }
            )
    for name, config in sorted((document.get("plugins") or {}).items()):
        rows.append(
            {
                "section": PLUGIN_SECTION,
                "provider": name,
                "key": name,
                "fields": sorted(config) if isinstance(config, Mapping) else [],
            }
        )
    return rows


# -- validation and change --


def _model_entry(model: type[BaseModel], entry: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return model.model_validate(dict(entry)).model_dump(mode="json", by_alias=True)
    except ValidationError as error:
        first = error.errors()[0] if error.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()))
        if not _SAFE_PATH.fullmatch(where):
            where = "(field)"  # a user-supplied field name is never echoed back
        raise RegistrationRefused(
            f"Invalid entry {where or '(root)'}: {str(first.get('msg', ''))[:200]}"
        ) from None


def _dumped(model: type[BaseModel], entry: Mapping[str, Any]) -> dict[str, Any]:
    """An existing entry in the same shape an update is compared in (defaults filled in)."""
    try:
        return model.model_validate(dict(entry)).model_dump(mode="json", by_alias=True)
    except ValidationError:
        return dict(entry)


def _check_secrets(entry: Mapping[str, Any]) -> None:
    findings = secret_findings(entry)
    if findings:
        raise RegistrationRefused(
            "The entry looks like it holds a secret ("
            + "; ".join(findings[:4])
            + "). Configuration holds credential references (credential-ref:NAME), never values."
        )
    if len(json.dumps(entry)) > MAX_ENTRY_BYTES:
        raise RegistrationRefused("The entry is too large.")


def _plugin_change(
    document: dict[str, Any],
    action: str,
    key: str | None,
    entry: Mapping[str, Any],
    allow_module: bool,
) -> tuple[dict[str, Any], str, tuple[str, ...]]:
    if not key or not providers.PLUGIN_NAME_RE.match(key):
        raise RegistrationRefused("A plugin section needs the plugin's name as its key.")
    target = resolve_plugin(key, allow_module)
    # The same two checks ``serve`` makes when it loads the plugin, so a file written here loads.
    if getattr(target, "register_from_config", None) is None and not callable(target):
        raise RegistrationRefused(
            f"Plugin {key!r} has no register_from_config; serve would refuse it."
        )
    if _module_sections(target):
        raise RegistrationRefused(
            f"Plugin {key!r} declares its own config sections; use those, not the plugin section."
        )
    plugins = dict(document.get("plugins") or {})
    exists = key in plugins
    if action == "add" and exists:
        raise RegistrationRefused(f"Plugin {key!r} is already configured; use update.")
    if action == "update" and not exists:
        raise RegistrationRefused(f"Plugin {key!r} is not configured; use add.")
    merged = {**plugins.get(key, {}), **entry} if action == "update" else dict(entry)
    check = getattr(target, "validate_config", None)
    if callable(check):
        try:
            check(merged)
        except ValueError as error:
            raise RegistrationRefused(
                f"Plugin {key!r} refused its config: {str(error)[:200]}"
            ) from None
    plugins[key] = merged
    after = {**document, "plugins": plugins}
    return after, key, tuple(sorted(entry))


def propose(
    document: Mapping[str, Any],
    action: str,
    section: str,
    key: str | None,
    entry: Mapping[str, Any],
    allow_module: bool = False,
) -> Change:
    """Validate one add or update against ``document`` and return the resulting document.

    Pure: nothing is written or registered. Raises ``RegistrationRefused``."""
    if action not in ACTIONS:
        raise RegistrationRefused(f"Unknown action {action!r}; use one of {list(ACTIONS)}.")
    if not isinstance(entry, Mapping):
        raise RegistrationRefused("The entry must be a JSON object.")
    base = {**document}
    if key is not None and (not _KEY.fullmatch(key) or secret_findings({"name": key})):
        raise RegistrationRefused("The key must be a plain name (letters, digits, . _ : -).")
    _check_secrets(entry)
    if section == PLUGIN_SECTION:
        after, key, changed = _plugin_change(base, action, key, entry, allow_module)
        stored = after["plugins"][key]
    else:
        known = sections(document, allow_module)
        spec = known.get(section)
        if spec is None:
            raise RegistrationRefused(
                f"Unknown section {section!r}; known sections: "
                f"{known_section_names(document, allow_module)}."
            )
        after, key, stored, changed = _section_change(base, spec, action, key, entry)
    _check_secrets(stored)
    for item in sections(after, allow_module).values():
        try:
            item.validate(after)
        except (ValueError, ValidationError) as error:
            raise RegistrationRefused(
                f"The resulting document is invalid: {str(error)[:300]}"
            ) from None
    return Change(
        action, section, key, after, digest_of(document), digest_of(after), changed, dict(stored)
    )


def _section_change(
    document: dict[str, Any],
    spec: ConfigSection,
    action: str,
    key: str | None,
    entry: Mapping[str, Any],
) -> tuple[dict[str, Any], str | None, dict[str, Any], tuple[str, ...]]:
    current = entries_of(document, spec)
    if spec.key_field is None:  # one object
        exists = bool(current)
        if action == "add" and exists:
            raise RegistrationRefused(f"{spec.name} is already configured; use update.")
        if action == "update" and not exists:
            raise RegistrationRefused(f"{spec.name} is not configured; use add.")
        merged = {**current[0], **entry} if exists else dict(entry)
        stored = _model_entry(spec.model, merged)
        before = _dumped(spec.model, current[0]) if exists else {}
        changed = tuple(sorted(k for k in stored if before.get(k) != stored[k]))
        after = {**document}
        _set(after, spec.path, stored, spec)
        return after, None, stored, changed
    name = key if key is not None else entry.get(spec.key_field)
    if not isinstance(name, str) or not name:
        raise RegistrationRefused(f"The entry needs its {spec.key_field!r}.")
    if spec.key_field in entry and entry[spec.key_field] != name:
        raise RegistrationRefused(f"{spec.key_field} cannot be changed; add a new entry instead.")
    index = next((i for i, e in enumerate(current) if e.get(spec.key_field) == name), None)
    if action == "add" and index is not None:
        candidate = _model_entry(spec.model, {**entry, spec.key_field: name})
        if candidate == _dumped(spec.model, current[index]):
            return document, name, candidate, ()  # already applied: a retry changes nothing
        raise RegistrationRefused(f"{spec.name} {name!r} already exists; use update.")
    if action == "update" and index is None:
        raise RegistrationRefused(f"{spec.name} {name!r} does not exist; use add.")
    merged = {**current[index], **entry} if index is not None else {**entry, spec.key_field: name}
    stored = _model_entry(spec.model, merged)
    previous = _dumped(spec.model, current[index]) if index is not None else {}
    changed = tuple(sorted(k for k in stored if previous.get(k) != stored[k]))
    items = list(current)
    if index is None:
        items.append(stored)
    else:
        items[index] = stored
    after = {**document}
    parent = spec.path[:-1]
    # Rebuild the parent chain without sharing structure with the caller's document.
    node = after
    for part in parent:
        node[part] = dict(node.get(part) or (spec.container() if spec.container else {}))
        node = node[part]
    node[spec.path[-1]] = items
    return after, name, stored, changed


# -- trust readout --


def trust_entry_hint(provider_id: str, worker: str | None, cls: str = "private_derived") -> str:
    """What to merge into the provider trust file to admit ``cls`` (docs/provider-trust.md)."""
    who = worker or "<worker principal id>"
    return (
        f'To admit it, make the provider trust file\'s "providers" entry for "{provider_id}" '
        f'list "{cls}" under data_classes and "{who}" under worker_principal_ids (merge into the '
        "existing entry; do not replace it, or other listed workers lose access), then "
        "restart serve."
    )


def trust_readout(policy: Any, change: Change, spec: ConfigSection | None) -> dict[str, Any]:
    """What the current provider trust setting admits for the entry's worker. Read only: provider
    trust is operator policy and this never edits it."""
    worker = None if spec is None else spec.worker(change.entry)
    if worker is None or policy is None:
        return {"worker_principal_id": worker, "checked": False}
    provider = spec.provider_of(change.entry) if spec.provider_of else spec.provider
    classes = sorted(policy.allowed(provider, worker))
    out: dict[str, Any] = {
        "worker_principal_id": worker,
        "checked": True,
        "provider": provider,
        "admitted_classes": classes,
        "policy_digest": policy.digest,
    }
    out["note"] = (
        "Evaluated against the providers registered now; a provider added here registers at the "
        "next serve start."
    )
    if provider not in providers.provider_ids() and policy.document.get("mode") == (
        "all_registered_providers"
    ):
        listed = worker in policy.document["providers"].get(provider, {}).get(
            "worker_principal_ids", []
        )
        out["after_restart"] = (
            "every data class (mode all_registered_providers; the worker is listed)"
            if listed
            else "public only until the worker is listed under this provider"
        )
    if "private_derived" not in classes:
        out["private_derived"] = "not admitted"
        out["hint"] = trust_entry_hint(provider, worker)
    return out


# -- file --


def read_document(path: Path) -> tuple[dict[str, Any], str, bool]:
    """(document, digest, existed). A missing file is an empty document; anything else must be
    the owner-only file ``serve`` itself would accept."""
    if not os.path.lexists(path):
        document = empty_document()
        return document, digest_of(document), False
    document = providers.load_providers_file(str(path))
    return document, digest_of(document), True


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _check_path(path: Path) -> None:
    """The rules ``serve`` applies when it reads the file, applied before a file is created:
    absolute, no ``..``, no symlink in any ancestor, and a parent only the owner can write."""
    if not path.is_absolute() or ".." in path.parts:
        raise RegistrationRefused("The providers file path must be absolute with no '..'.")
    probe = Path(path.anchor)
    for part in path.parent.parts[1:]:
        probe = probe / part
        if probe.is_symlink():
            raise RegistrationRefused(
                f"A directory on the providers file path is a symlink: {probe}."
            )
    try:
        parent = path.parent.stat()
    except OSError:
        raise RegistrationRefused("The providers file's directory does not exist.") from None
    if parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not parent.st_mode & stat.S_ISVTX:
        raise RegistrationRefused("The providers file's directory is group- or world-writable.")
    if path.is_symlink():
        raise RegistrationRefused("The providers file is a symlink.")


def _audit_records(audit: Path) -> list[dict[str, Any]]:
    rows = []
    if audit.exists():
        for line in audit.read_text().splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    raise RegistrationRefused("The audit file has a corrupt line.") from None
    return rows


def _record_hash(body: Mapping[str, Any]) -> str:
    plain = {k: v for k, v in body.items() if k != "hash"}
    return hashlib.sha256(
        (body["previous_hash"] + json.dumps(plain, sort_keys=True, separators=(",", ":"))).encode()
    ).hexdigest()


def verify_audit(path: Path) -> dict[str, Any]:
    """Recompute the chain: ``{ok, records, last_hash, broken_at}``. A deleted or truncated tail
    is not detectable from the file alone; the last hash is returned for external anchoring."""
    rows = _audit_records(Path(str(path) + AUDIT_SUFFIX))
    previous = "0" * 64
    for index, row in enumerate(rows):
        if row.get("previous_hash") != previous or row.get("hash") != _record_hash(row):
            return {"ok": False, "records": len(rows), "last_hash": previous, "broken_at": index}
        previous = row["hash"]
    return {"ok": True, "records": len(rows), "last_hash": previous, "broken_at": None}


def _open_private(path: Path, flags: int) -> int:
    """Open (creating 0600) without following a final symlink; force owner-only mode."""
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CREAT, 0o600)
    os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
    return fd


def append_audit(path: Path, record: Mapping[str, Any]) -> dict[str, Any]:
    """Append one hash-chained record (each carries the hash of the one before it)."""
    audit = Path(str(path) + AUDIT_SUFFIX)
    state = verify_audit(path)
    if not state["ok"]:
        raise RegistrationRefused("The audit chain is broken; refusing to extend it.")
    body = {**record, "previous_hash": state["last_hash"]}
    body["hash"] = _record_hash(body)
    fd = _open_private(audit, os.O_WRONLY | os.O_APPEND)
    try:
        os.write(fd, (json.dumps(body, sort_keys=True) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    return body


def _lock(fd: int, seconds: float = 5.0) -> None:
    """An exclusive lock, bounded: a stuck holder is a refusal, never a stalled service."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise RegistrationRefused(
                    "The providers file is locked by another change; try again."
                ) from None
            time.sleep(0.05)


def _write_atomic(path: Path, raw: bytes) -> None:
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), stat.S_IRUSR | stat.S_IWUSR)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def apply_change(
    path: Path,
    action: str,
    section: str,
    key: str | None,
    entry: Mapping[str, Any],
    *,
    actor: str,
    expected_digest: str | None = None,
    policy: Any = None,
    dry_run: bool = False,
    allow_module: bool = False,
    authorize: Callable[[dict[str, Any]], str | None] | None = None,
) -> dict[str, Any]:
    """Validate and, unless ``dry_run``, atomically write one change to the providers file.

    Holds an exclusive lock for the read-validate-write, refuses when the file no longer has
    ``expected_digest`` (the digest the caller listed), and appends the audit record after the
    write. The audit chain is verified before anything is written, so a broken one refuses the
    change instead of leaving a changed file with no record. Returns a result with the digests,
    the changed fields and the trust readout."""
    path = Path(path)
    _check_path(path)
    try:
        lock = _open_private(path.parent / (path.name + ".lock"), os.O_RDWR)
    except OSError as error:
        raise RegistrationRefused(f"Cannot open the lock file: {type(error).__name__}.") from None
    try:
        _lock(lock)
        document, digest, existed = read_document(path)
        if expected_digest is not None and expected_digest != digest:
            raise RegistrationRefused(
                "The providers file changed since it was read "
                f"(digest {digest[:12]}, expected {expected_digest[:12]}); list it again."
            )
        if authorize is not None:
            # Decided on the document read under the lock, so who owns an entry cannot change
            # between the check and the write.
            reason = authorize(document)
            if reason:
                raise RegistrationRefused(reason)
        change = propose(document, action, section, key, entry, allow_module)
        if change.before_digest == change.after_digest and existed:
            return {
                "action": action,
                "section": section,
                "key": change.key,
                "changed_fields": [],
                "before_digest": change.before_digest,
                "after_digest": change.after_digest,
                "written": False,
                "noop": True,
                "applies": "already in the file",
                "trust": {"checked": False},
            }
        raw = (json.dumps(change.document, indent=2, sort_keys=True) + "\n").encode()
        if len(raw) > providers.MAX_PROVIDERS_FILE_BYTES:
            raise RegistrationRefused(
                f"The resulting file would be {len(raw)} bytes; serve reads at most "
                f"{providers.MAX_PROVIDERS_FILE_BYTES}."
            )
        if not verify_audit(path)["ok"]:
            raise RegistrationRefused("The audit chain is broken; refusing to change the file.")
        spec = sections(change.document, allow_module).get(section)
        result = {
            "action": action,
            "section": section,
            "key": change.key,
            "changed_fields": list(change.changed_fields),
            "before_digest": change.before_digest,
            "after_digest": change.after_digest,
            "written": False,
            "applies": "at the next serve start (the file is read at start)",
            "trust": trust_readout(policy, change, spec),
        }
        if dry_run:
            return result
        try:
            _write_atomic(path, raw)
        except OSError as error:
            raise RegistrationRefused(f"Cannot write the file: {type(error).__name__}.") from None
        result["written"] = True
        try:
            record = append_audit(
                path,
                {
                    "at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "actor": actor,
                    "action": action,
                    "section": section,
                    "key": change.key,
                    "changed_fields": list(change.changed_fields),
                    "before_digest": change.before_digest,
                    "after_digest": change.after_digest,
                    "created_file": not existed,
                },
            )
            result["audit_hash"] = record["hash"]
        except (OSError, RegistrationRefused) as error:
            # The file is already changed: say so, never report a refusal for a written change.
            result["audit_error"] = f"{type(error).__name__}: {str(error)[:120]}"
        return result
    finally:
        os.close(lock)


def read_audit(path: Path, limit: int = 100) -> list[dict[str, Any]]:
    return _audit_records(Path(str(path) + AUDIT_SUFFIX))[-limit:]
