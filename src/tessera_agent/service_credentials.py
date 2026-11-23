"""Install a service-scoped credential reference without exposing the secret.

A service (for example the reasoning supervisor) reads a credential only from the file systemd's
``LoadCredential=`` places under ``$CREDENTIALS_DIRECTORY``. This module is the one validated way to
create those source files:

* ``install`` reads the secret from a stream only (stdin; no value, argv, environment or file-path
  input exists) and writes a root-owned 0600 file atomically, plus the companion reference file the
  service's start-up mapper compares with the profile's ``credential_ref``; both are written or
  neither. It verifies them and appends a hash-chained receipt holding the reference label and a
  fingerprint, never the value;
* it refuses when a general API-key environment variable would also supply the key (in the
  installer's environment, or set by the service's unit fragment, drop-in or environment file; the
  unit scan is best-effort, the service's own start-up check is the enforcement);
* ``withdraw`` removes those same two files (both or neither, the key file never opened) and
  appends a receipt that copies the reference and fingerprint from the latest install receipt;
  with nothing installed it records ``already_absent``;
* ``check`` re-validates the installed files and the unit's ``LoadCredential`` declarations;
* ``receipts verify`` and ``receipts archive-broken`` recover from a damaged receipt chain.

The service-start admission itself lives in ``service_startup`` (forbidden environment, the
reference file, the key file); this module installs what that admission reads and shares its key
shape (``model_destinations.API_KEY_SHAPE``) so an installed key is never one the runtime rejects.

Standard library only. Nothing here logs, prints or returns secret bytes; refusals carry a stable
code and a fixed sanitized message. A corrupt receipt chain blocks every further install
(``store_unavailable``) until it is archived with ``receipts archive-broken``.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from .model_destinations import API_KEY_SHAPE, FORBIDDEN_ENVIRONMENT

MAX_READ_BYTES = 4097
MAX_PROFILE_BYTES = 262144
RECEIPTS_NAME = "credential-install.receipts.jsonl"
DEFAULT_UNIT_DIRS = ("/etc/systemd/system", "/usr/lib/systemd/system")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ENV_NAME = re.compile(r"(?<![A-Za-z0-9_])([A-Z_][A-Z0-9_]*)=")
EXIT_REFUSED = 78

# A service and the credentials it may load. Plain data: a new service or vendor adds an entry.
# ``reference_file`` is the companion LoadCredential the start-up mapper compares with the
# profile's credential_ref.
SERVICES: dict[str, dict[str, Any]] = {
    "tessera-reasoning": {
        "credentials_dir": "/etc/tessera/credentials",
        "unit": "tessera-reasoning.service",
        "profile": "/etc/tessera/reasoning.json",
        "credentials": {
            "anthropic-api-key": {
                "file": "reasoning-anthropic-api-key",
                "forbidden_env": FORBIDDEN_ENVIRONMENT,
                "reference_file": {
                    "name": "model-credential",
                    "file": "reasoning-model-credential",
                },
            }
        },
    }
}

_MESSAGES = {
    "forbidden_environment": "A general API-key environment variable is set; the key must come "
    "only from the service credential.",
    "unit_environment_forbidden": "The service unit sets a general API-key environment variable.",
    "missing_credential": "A required service credential is missing.",
    "invalid_credential_shape": "The credential is not 1-512 printable ASCII characters "
    "without whitespace.",
    "unsafe_permissions": "The credential file or its directory has unsafe ownership, mode "
    "or type.",
    "unknown_service": "That service is not registered.",
    "unknown_credential": "That credential is not registered for the service.",
    "not_declared_in_unit": "The service unit does not declare LoadCredential for this credential "
    "and its reference file.",
    "exists": "A credential is already installed; use --replace.",
    "secret_not_provided": "No secret was provided on standard input.",
    "store_unavailable": "The credential store could not be written.",
    "invalid_label": "A label may use letters, digits, dot, underscore and hyphen (64 characters).",
    "reference_mismatch": "The service profile's credential_ref does not match the reference "
    "these labels build.",
    "receipts_intact": "The receipt chain is intact; there is nothing to archive.",
}


class CredentialRefused(ValueError):
    """A sanitized refusal. ``code`` is stable; the message never contains secret bytes."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        super().__init__(detail or _MESSAGES.get(code, code))


# -- registry -----------------------------------------------------------------------------------


def _service(service: str) -> dict[str, Any]:
    found = SERVICES.get(service)
    if found is None:
        raise CredentialRefused("unknown_service")
    return found


def _credential(service: str, name: str) -> dict[str, Any]:
    found = _service(service)["credentials"].get(name)
    if found is None:
        raise CredentialRefused("unknown_credential")
    return found


def _forbidden_names(service: str) -> tuple[str, ...]:
    names: list[str] = []
    for item in _service(service)["credentials"].values():
        for name in item["forbidden_env"]:
            if name not in names:
                names.append(name)
    return tuple(names)


def _expected_owner_uid(require_root: bool) -> int:
    """The uid that must own the store and its files: root for a service store, the caller for a
    user store (tests and non-root stores). One place, so a test can pin it."""
    return 0 if require_root else os.getuid()


# -- secret shape and fingerprint -------------------------------------------------------------


def fingerprint(secret: bytes) -> str:
    return "sha256:" + hashlib.sha256(secret).hexdigest()[:16]


def _normalize(raw: bytes) -> bytes:
    """One trailing newline allowed; the runtime's key shape (1-512 printable ASCII, no space)."""
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if not API_KEY_SHAPE.fullmatch(raw):
        raise CredentialRefused("invalid_credential_shape")
    return raw


def _check_environment(environ: Mapping[str, str], names: Iterable[str]) -> None:
    if any(name in environ for name in names):
        raise CredentialRefused("forbidden_environment")


# -- reading an installed file ------------------------------------------------------------------


def _read_private(path: Path, uid: int) -> bytes:
    """The bytes of a regular, owned, single-link, owner-only file; refuses anything else."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise CredentialRefused("missing_credential") from None
    except OSError:
        raise CredentialRefused("unsafe_permissions") from None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != uid
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
        raise CredentialRefused("unsafe_permissions")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise CredentialRefused("unsafe_permissions") from None
    try:
        raw = os.read(fd, MAX_READ_BYTES)
    except OSError:
        raise CredentialRefused("unsafe_permissions") from None
    finally:
        os.close(fd)
    return raw


def _read_secret_file(path: Path, uid: int) -> bytes:
    return _normalize(_read_private(path, uid))


# -- systemd unit inspection (names and paths only) ------------------------------------------


def _unit_files(service: str, unit_dirs: Iterable[str | os.PathLike]) -> list[Path]:
    unit = _service(service)["unit"]
    found: list[Path] = []
    for directory in unit_dirs:
        base = Path(directory)
        if (base / unit).is_file():
            found.append(base / unit)
        dropins = base / (unit + ".d")
        if dropins.is_dir():
            found.extend(sorted(dropins.glob("*.conf")))
    return found


def _lines(path: Path) -> list[str]:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    return [line.strip() for line in text.splitlines()]


def _unit_environment_names(files: list[Path]) -> set[str]:
    """Variable NAMES set by Environment= and EnvironmentFile= lines. Values are never kept.

    Best effort: it reads the unit fragment and its drop-ins under the given directories only
    (not /run, top-level ``service.d``, PassEnvironment= or ``systemctl set-environment``). The
    service's own start-up check is the enforcement."""
    names: set[str] = set()
    for file in files:
        for line in _lines(file):
            if line.startswith("Environment="):
                names.update(_ENV_NAME.findall(line[len("Environment=") :]))
            elif line.startswith("EnvironmentFile="):
                target = Path(line[len("EnvironmentFile=") :].lstrip("-").strip().strip("\"'"))
                if target.is_absolute() and target.is_file():
                    for entry in _lines(target):
                        match = re.match(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", entry)
                        if match and not entry.startswith("#"):
                            names.add(match.group(1))
    return names


def _declared_credentials(files: list[Path]) -> dict[str, str]:
    """LoadCredential= declarations as {id: path}, honouring an empty reset."""
    declared: dict[str, str] = {}
    for file in files:
        for line in _lines(file):
            if not line.startswith("LoadCredential="):
                continue
            value = line[len("LoadCredential=") :].strip()
            if not value:
                declared.clear()
            elif ":" in value:
                ident, _, path = value.partition(":")
                declared[ident.strip()] = path.strip()
    return declared


def unit_state(
    service: str, declarations: Iterable[tuple[str, Path]], unit_dirs: Iterable[Any]
) -> bool | None:
    """True when the unit declares every (id, path) LoadCredential, None when no unit file
    exists, False when the unit exists without (or with another path for) one of them. Refuses
    when the unit sets a forbidden environment variable."""
    files = _unit_files(service, unit_dirs)
    if not files:
        return None
    if _unit_environment_names(files) & set(_forbidden_names(service)):
        raise CredentialRefused("unit_environment_forbidden")
    declared = _declared_credentials(files)
    return all(declared.get(ident) == str(path) for ident, path in declarations)


def _declarations(item: Mapping[str, Any], name: str, directory: Path) -> list[tuple[str, Path]]:
    pairs = [(name, directory / item["file"])]
    companion = item.get("reference_file")
    if companion:
        pairs.append((companion["name"], directory / companion["file"]))
    return pairs


# -- install ---------------------------------------------------------------------------------


def _label(value: str) -> str:
    if not isinstance(value, str) or not _LABEL.fullmatch(value):
        raise CredentialRefused("invalid_label")
    return value


def _read_secret(stream: BinaryIO) -> bytes:
    try:
        if stream.isatty():
            typed = getpass.getpass("")
            raw = typed.encode("ascii") if isinstance(typed, str) else bytes(typed)
        else:
            raw = stream.read(MAX_READ_BYTES)
    except (OSError, ValueError, UnicodeError):
        raise CredentialRefused("secret_not_provided") from None
    if not raw:
        raise CredentialRefused("secret_not_provided")
    return _normalize(raw)


def _checked_directory(path: Path, require_root: bool) -> None:
    probe = Path(path.anchor)
    for part in path.parts[1:]:
        probe = probe / part
        try:
            info = os.lstat(probe)
        except OSError:
            raise CredentialRefused("store_unavailable") from None
        if stat.S_ISLNK(info.st_mode):
            raise CredentialRefused("unsafe_permissions")
    info = os.lstat(path)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != _expected_owner_uid(require_root)
        or info.st_mode & 0o022
    ):
        raise CredentialRefused("unsafe_permissions")


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _profile_reference(profile_path: str | os.PathLike | None) -> str | None:
    """The profile's ``credential_ref`` (a label), or None when no readable profile is given.
    Only that one field is read; nothing else of the profile is kept or shown."""
    if profile_path is None or not os.path.isfile(profile_path):
        return None
    try:
        with open(profile_path, "rb") as handle:
            data = json.loads(handle.read(MAX_PROFILE_BYTES))
    except (OSError, ValueError):
        return None
    ref = data.get("credential_ref") if isinstance(data, dict) else None
    return ref if isinstance(ref, str) else None


# Receipts ---------------------------------------------------------------------------------------


def _receipt_hash(body: Mapping[str, Any]) -> str:
    plain = {key: value for key, value in body.items() if key != "hash"}
    return hashlib.sha256(
        (body["previous_hash"] + json.dumps(plain, sort_keys=True, separators=(",", ":"))).encode()
    ).hexdigest()


def read_receipts(path: Path) -> list[dict[str, Any]]:
    rows = []
    if os.path.lexists(path):
        for line in Path(path).read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def verify_receipts(path: Path) -> dict[str, Any]:
    """Recompute the chain: ``{ok, count, broken_at, last_good_hash}``."""
    previous, count = "0" * 64, 0
    try:
        rows = read_receipts(path)
    except (OSError, ValueError):
        return {"ok": False, "count": 0, "broken_at": 0, "last_good_hash": previous}
    for index, row in enumerate(rows):
        if (
            not isinstance(row, dict)
            or row.get("previous_hash") != previous
            or row.get("hash") != _receipt_hash(row)
        ):
            return {"ok": False, "count": count, "broken_at": index, "last_good_hash": previous}
        previous, count = row["hash"], count + 1
    return {"ok": True, "count": count, "broken_at": None, "last_good_hash": previous}


def _append_receipt(path: Path, record: Mapping[str, Any]) -> dict[str, Any]:
    state = verify_receipts(path)
    if not state["ok"]:
        raise CredentialRefused(
            "store_unavailable",
            "The receipt chain is broken; run `tessera-service-credential receipts verify`, then "
            "`receipts archive-broken` to restart it.",
        )
    body = {**record, "previous_hash": state["last_good_hash"]}
    body["hash"] = _receipt_hash(body)
    created = not os.path.lexists(path)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, (json.dumps(body, sort_keys=True) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    if created:  # best effort: make the new directory entry durable
        try:
            _fsync_directory(path.parent)
        except OSError:
            pass
    return body


def archive_broken_receipts(path: Path, now: datetime | None = None) -> dict[str, Any]:
    """Move a broken receipt chain aside and start a new one whose first record names the archive
    and the last good hash. Refuses when the chain is intact."""
    path = Path(path)
    state = verify_receipts(path)
    if state["ok"]:
        raise CredentialRefused("receipts_intact")
    moment = now or datetime.now(UTC)
    archived = path.with_name(
        f"{path.name}.broken-{moment.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
    )
    os.replace(path, archived)
    os.chmod(archived, 0o600)
    _fsync_directory(path.parent)
    return _append_receipt(
        path,
        {
            "at": moment.isoformat(timespec="seconds"),
            "event": "chain_restarted",
            "archived_as": archived.name,
            "last_good_hash": state["last_good_hash"],
            "count_verified": state["count"],
        },
    )


# Writing -----------------------------------------------------------------------------------------


class _Placement:
    """One file this run puts in place, with what is needed to undo exactly this run's change."""

    def __init__(self, directory: Path, file: str, content: bytes) -> None:
        self.directory, self.file, self.content = directory, file, content
        self.target = directory / file
        self.token = secrets.token_hex(8)
        self.temp = directory / f".{file}.{self.token}.tmp"
        self.backup = directory / f".{file}.{self.token}.prev"
        self.existed = os.path.lexists(self.target)
        self.backed_up = False
        self.placed = False

    def stage(self, require_root: bool) -> None:
        fd = os.open(self.temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            if require_root:
                os.fchown(fd, 0, 0)
            os.write(fd, self.content)
            os.fsync(fd)
        finally:
            os.close(fd)

    def place(self) -> None:
        if self.existed:
            os.link(self.target, self.backup)  # fails if the name exists: only OUR backup is used
            self.backed_up = True
        os.replace(self.temp, self.target)
        self.placed = True
        _fsync_directory(self.directory)

    def rollback(self) -> None:
        _unlink(self.temp)
        if self.placed:
            if self.backed_up and os.path.lexists(self.backup):
                try:
                    os.replace(self.backup, self.target)
                except OSError:
                    pass
            elif not self.existed:
                _unlink(self.target)
        elif self.backed_up:
            _unlink(self.backup)

    def finish(self) -> None:
        if self.backed_up:
            _unlink(self.backup)


def _unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _reference_in_place(target: Path, content: bytes, require_root: bool) -> bool:
    """True when ``target`` is already exactly the wanted reference file: a regular, single-link
    file owned by the expected owner, with no group or other access, holding ``content``."""
    try:
        info = os.lstat(target)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_mode & 0o077
            or info.st_uid != _expected_owner_uid(require_root)
        ):
            return False
        return _read_private(target, _expected_owner_uid(require_root)) == content
    except (OSError, CredentialRefused, ValueError):
        return False


def install(
    service: str,
    name: str,
    stream: BinaryIO,
    *,
    credentials_dir: str | os.PathLike | None = None,
    unit_dirs: Iterable[str | os.PathLike] | None = None,
    environ: Mapping[str, str] | None = None,
    owner_label: str,
    reference_label: str,
    replace: bool = False,
    require_root: bool = True,
    receipts_path: str | os.PathLike | None = None,
    profile_path: str | os.PathLike | None = None,
) -> dict[str, Any]:
    """Install one credential (and its reference file) from ``stream``; return the receipt.

    Both files are written or neither. Nothing is read from ``stream`` until every other check has
    passed (environment, store, unit declarations, profile reference, existing files)."""
    item = _credential(service, name)
    environ = os.environ if environ is None else environ
    owner, reference_label = _label(owner_label), _label(reference_label)
    reference = f"{name}://{owner}/{reference_label}"
    _check_environment(environ, _forbidden_names(service))
    if require_root and os.geteuid() != 0:
        raise CredentialRefused("store_unavailable", "Installing for a service needs root.")
    directory = Path(credentials_dir or _service(service)["credentials_dir"])
    if not directory.is_absolute():
        raise CredentialRefused("unsafe_permissions")
    _checked_directory(directory, require_root)
    declarations = _declarations(item, name, directory)
    unit_dirs = DEFAULT_UNIT_DIRS if unit_dirs is None else tuple(unit_dirs)
    declared = unit_state(service, declarations, unit_dirs)
    if declared is False:
        raise CredentialRefused("not_declared_in_unit")
    profile_ref = _profile_reference(profile_path)
    if profile_ref is not None and profile_ref != reference:
        raise CredentialRefused(
            "reference_mismatch",
            f"The profile's credential_ref is {profile_ref!r}; use --owner and --reference "
            "that build exactly that reference.",
        )
    files = [(item["file"], None)]
    reference_target: Path | None = None
    if item.get("reference_file"):
        reference_target = directory / item["reference_file"]["file"]
        content = (reference + "\n").encode("ascii")
        # The reference is not a secret: if the reasoning installer (ADR 0119) already wrote
        # exactly this reference, keep it and install only the key. A different one still needs
        # --replace.
        if not _reference_in_place(reference_target, content, require_root):
            files.append((item["reference_file"]["file"], content))
    for file, _ in files:
        target = directory / file
        if os.path.lexists(target):
            if not replace:
                raise CredentialRefused("exists")
            info = os.lstat(target)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise CredentialRefused("unsafe_permissions")
    secret = _read_secret(stream)
    placements = [
        _Placement(directory, file, secret + b"\n" if content is None else content)
        for file, content in files
    ]
    replaced = any(p.existed for p in placements)
    try:
        for placement in placements:
            placement.stage(require_root)
        for placement in placements:
            placement.place()
        owner_uid = _expected_owner_uid(require_root)
        installed = _read_secret_file(placements[0].target, owner_uid)
        if fingerprint(installed) != fingerprint(secret):
            raise CredentialRefused("store_unavailable")
        for placement in placements[1:]:
            if _read_private(placement.target, owner_uid) != placement.content:
                raise CredentialRefused("store_unavailable")
        receipt = _append_receipt(
            Path(receipts_path or directory / RECEIPTS_NAME),
            {
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
                "service": service,
                "credential": name,
                "reference": reference,
                "installed_path": str(placements[0].target),
                "reference_path": None if reference_target is None else str(reference_target),
                "profile_checked": profile_ref is not None,
                "fingerprint": fingerprint(secret),
                "size": len(secret),
                "installed_by_uid": os.getuid(),
                "unit_declared": declared,
                "replaced": replaced,
            },
        )
    except CredentialRefused:
        for placement in reversed(placements):
            placement.rollback()
        raise
    except OSError:
        for placement in reversed(placements):
            placement.rollback()
        raise CredentialRefused("store_unavailable") from None
    for placement in placements:
        placement.finish()
    return receipt


# -- withdraw --------------------------------------------------------------------------------


class _Removal:
    """One installed file this run moves aside, with what is needed to put exactly it back.

    The file is never opened: it is inspected with ``lstat``, hard-linked to a fresh ``.prev``
    name (``link`` fails if that name exists, so only OUR backup is ever used), then unlinked."""

    def __init__(self, directory: Path, file: str) -> None:
        if Path(file).name != file or file in {"", ".", ".."}:
            raise CredentialRefused("unsafe_permissions")
        self.directory, self.file = directory, file
        self.target = directory / file  # a bare name in the checked (symlink-free) directory
        self.backup = directory / f".{file}.{secrets.token_hex(8)}.prev"
        try:
            info = os.lstat(self.target)
        except FileNotFoundError:
            self.identity = None
        except OSError:
            raise CredentialRefused("store_unavailable") from None
        else:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise CredentialRefused("unsafe_permissions")  # symlink, other type, hard link
            self.identity = (info.st_dev, info.st_ino)
        self.backed_up = False
        self.removed = False

    @property
    def present(self) -> bool:
        return self.identity is not None

    def move_aside(self) -> None:
        os.link(self.target, self.backup, follow_symlinks=False)
        self.backed_up = True
        info = os.lstat(self.backup)
        if (info.st_dev, info.st_ino) != self.identity or not stat.S_ISREG(info.st_mode):
            raise CredentialRefused("store_unavailable")  # the name changed since it was checked
        os.unlink(self.target)
        self.removed = True

    def restore(self) -> None:
        if self.removed:
            try:
                os.link(self.backup, self.target, follow_symlinks=False)
            except OSError:
                return  # never overwrite whatever now holds the name; the backup stays
            self.removed = False
        if self.backed_up:
            _unlink(self.backup)
            self.backed_up = False

    def finish(self) -> bool:
        if self.backed_up:
            try:
                os.unlink(self.backup)
            except FileNotFoundError:
                pass
            except OSError:
                return False
            self.backed_up = False
        return True


def _latest_install(rows: list[dict[str, Any]], service: str, name: str) -> dict[str, Any] | None:
    """The newest install receipt for this service credential in the (verified) chain."""
    for row in reversed(rows):
        if (
            isinstance(row, dict)
            and row.get("service") == service
            and row.get("credential") == name
            and row.get("action", "install") == "install"
        ):
            return row
    return None


def withdraw(
    service: str,
    name: str,
    *,
    credentials_dir: str | os.PathLike | None = None,
    owner_label: str,
    reason_label: str | None = None,
    require_root: bool = True,
    receipts_path: str | os.PathLike | None = None,
) -> dict[str, Any]:
    """Remove one installed credential and its companion reference file; return the receipt.

    The files are the ones ``install`` writes, named by the registry inside the checked credential
    directory (never a caller-supplied file path). Both present files are removed or neither, and
    the key file is never opened: the receipt copies the reference and fingerprint from the latest
    install receipt in the chain. Idempotent: with nothing installed the receipt's outcome is
    ``already_absent``. A broken receipt chain refuses before any file is touched."""
    item = _credential(service, name)
    owner = _label(owner_label)
    reason = None if reason_label is None else _label(reason_label)
    if require_root and os.geteuid() != 0:
        raise CredentialRefused("store_unavailable", "Withdrawing for a service needs root.")
    directory = Path(credentials_dir or _service(service)["credentials_dir"])
    if not directory.is_absolute():
        raise CredentialRefused("unsafe_permissions")
    _checked_directory(directory, require_root)
    receipts = Path(receipts_path or directory / RECEIPTS_NAME)
    state = verify_receipts(receipts)
    if not state["ok"]:
        raise CredentialRefused(
            "store_unavailable",
            "The receipt chain is broken; run `tessera-service-credential receipts verify`, then "
            "`receipts archive-broken` to restart it.",
        )
    installed = _latest_install(read_receipts(receipts), service, name)
    files = [item["file"]]
    if item.get("reference_file"):
        files.append(item["reference_file"]["file"])
    removals = [_Removal(directory, file) for file in files]  # every name checked before any move
    present = [removal for removal in removals if removal.present]
    try:
        for removal in present:
            removal.move_aside()
        if present:
            _fsync_directory(directory)
        receipt = _append_receipt(
            receipts,
            {
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
                "service": service,
                "credential": name,
                "action": "withdraw",
                "outcome": "removed" if present else "already_absent",
                "removed_paths": [str(removal.target) for removal in present],
                "reference": None if installed is None else installed.get("reference"),
                "fingerprint": None if installed is None else installed.get("fingerprint"),
                "install_receipt_hash": None if installed is None else installed.get("hash"),
                "withdrawn_by_uid": os.getuid(),
                "owner_label": owner,
                "reason_label": reason,
            },
        )
    except (OSError, CredentialRefused) as error:
        for removal in reversed(present):
            removal.restore()
        try:
            _fsync_directory(directory)
        except OSError:
            pass
        detail = str(error) if isinstance(error, CredentialRefused) else None
        raise CredentialRefused("store_unavailable", detail) from None
    finished = all([removal.finish() for removal in present])
    try:
        _fsync_directory(directory)
    except OSError:
        finished = False
    if not finished:
        raise CredentialRefused(
            "store_unavailable",
            "The credential was withdrawn and receipted, but a backup could not be deleted; "
            "remove the .prev file in the credential directory.",
        )
    return receipt


# -- check ------------------------------------------------------------------------------------


def check(
    service: str,
    *,
    credentials_dir: str | os.PathLike | None = None,
    unit_dirs: Iterable[str | os.PathLike] | None = None,
    environ: Mapping[str, str] | None = None,
    require_root: bool = True,
) -> dict[str, Any]:
    """Re-validate the installed files and the unit's declarations. Never raises for a bad
    state: ``ok`` is False with ``problems`` (stable codes). Names and fingerprints only."""
    environ = os.environ if environ is None else environ
    directory = Path(credentials_dir or _service(service)["credentials_dir"])
    uid = _expected_owner_uid(require_root)
    unit_dirs = DEFAULT_UNIT_DIRS if unit_dirs is None else tuple(unit_dirs)
    problems: list[str] = []
    credentials: list[dict[str, Any]] = []
    declared: dict[str, bool | None] = {}
    if any(name in environ for name in _forbidden_names(service)):
        problems.append("forbidden_environment")
    for name, item in _service(service)["credentials"].items():
        try:
            secret = _read_secret_file(directory / item["file"], uid)
            credentials.append(
                {"name": name, "fingerprint": fingerprint(secret), "size": len(secret)}
            )
            companion = item.get("reference_file")
            if companion:
                _read_private(directory / companion["file"], uid)
        except CredentialRefused as error:
            problems.append(f"{name}:{error.code}")
        try:
            state = unit_state(service, _declarations(item, name, directory), unit_dirs)
        except CredentialRefused as error:
            problems.append(error.code)
            state = False
        declared[name] = state
        if state is False and "not_declared_in_unit" not in problems:
            problems.append("not_declared_in_unit")
    return {
        "ok": not problems,
        "credentials": credentials,
        "unit_declared": declared,
        "problems": problems,
        "note": None
        if all(value is not None for value in declared.values())
        else "no unit file was found in the searched directories; the declaration was not checked",
    }


# -- command line ------------------------------------------------------------------------------


def _refuse(error: CredentialRefused) -> int:
    print(json.dumps({"refused": error.code, "message": str(error)}), file=sys.stderr)
    return EXIT_REFUSED


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tessera-service-credential",
        description="Install a service credential from standard input (the secret is never an "
        "argument or an environment variable), check it, or withdraw it with a receipt.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def common(sub: argparse.ArgumentParser, *, units: bool = False) -> None:
        sub.add_argument("--service", required=True)
        sub.add_argument("--credentials-dir")
        sub.add_argument(
            "--user-store",
            action="store_true",
            help="a store owned by the calling user instead of root (tests, non-root stores)",
        )
        if units:
            sub.add_argument("--unit-dir", action="append")

    install_cmd = commands.add_parser("install", help="read the secret from standard input")
    common(install_cmd, units=True)
    install_cmd.add_argument("--name", required=True)
    install_cmd.add_argument(
        "--owner", "--owner-label", dest="owner", required=True, help="reference owner label"
    )
    install_cmd.add_argument("--reference", required=True, help="reference label")
    install_cmd.add_argument("--replace", action="store_true")
    install_cmd.add_argument("--receipts")
    install_cmd.add_argument("--profile", help="service profile whose credential_ref must match")
    withdraw_cmd = commands.add_parser(
        "withdraw", help="remove an installed credential and its reference file, with a receipt"
    )
    common(withdraw_cmd)
    withdraw_cmd.add_argument("--name", required=True)
    withdraw_cmd.add_argument(
        "--owner-label", "--owner", dest="owner_label", required=True, help="reference owner label"
    )
    withdraw_cmd.add_argument("--reason", help="why (a label)")
    withdraw_cmd.add_argument("--receipts")
    check_cmd = commands.add_parser("check", help="re-validate the installed files and the unit")
    common(check_cmd, units=True)
    list_cmd = commands.add_parser("list", help="installed names, fingerprints and receipts")
    common(list_cmd)
    receipts_cmd = commands.add_parser("receipts", help="verify or restart the receipt chain")
    actions = receipts_cmd.add_subparsers(dest="receipts_command", required=True)
    verify_cmd = actions.add_parser("verify")
    common(verify_cmd)
    verify_cmd.add_argument("--receipts")
    archive_cmd = actions.add_parser("archive-broken")
    common(archive_cmd)
    archive_cmd.add_argument("--receipts")
    return parser


def _installed(service: str, directory: Path, uid: int) -> list[dict[str, Any]]:
    found = []
    for name, item in _service(service)["credentials"].items():
        path = directory / item["file"]
        if os.path.lexists(path):
            secret = _read_secret_file(path, uid)
            found.append(
                {"name": name, "path": str(path), "fingerprint": fingerprint(secret),
                 "size": len(secret)}
            )  # fmt: skip
    return found


def _profile_default(service: str, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    default = _service(service).get("profile")
    return default if default and os.path.isfile(default) else None


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _service(args.service)
        require_root = not args.user_store
        directory = Path(args.credentials_dir or SERVICES[args.service]["credentials_dir"])
        if args.command == "install":
            receipt = install(
                args.service,
                args.name,
                sys.stdin.buffer,
                credentials_dir=args.credentials_dir,
                unit_dirs=args.unit_dir,
                owner_label=args.owner,
                reference_label=args.reference,
                replace=args.replace,
                require_root=require_root,
                receipts_path=args.receipts,
                profile_path=_profile_default(args.service, args.profile),
            )
            print(json.dumps(receipt, indent=2))
            return 0
        if args.command == "withdraw":
            receipt = withdraw(
                args.service,
                args.name,
                credentials_dir=args.credentials_dir,
                owner_label=args.owner_label,
                reason_label=args.reason,
                require_root=require_root,
                receipts_path=args.receipts,
            )
            print(json.dumps(receipt, indent=2))
            return 0
        if args.command == "receipts":
            path = Path(args.receipts or directory / RECEIPTS_NAME)
            if args.receipts_command == "verify":
                result = verify_receipts(path)
                print(json.dumps(result, indent=2))
                return 0 if result["ok"] else EXIT_REFUSED
            if require_root and os.geteuid() != 0:
                raise CredentialRefused("store_unavailable", "Archiving receipts needs root.")
            print(json.dumps(archive_broken_receipts(path), indent=2))
            return 0
        if args.command == "list":
            installed = _installed(args.service, directory, _expected_owner_uid(require_root))
            receipts = read_receipts(directory / RECEIPTS_NAME)[-10:]
            print(json.dumps({"installed": installed, "receipts": receipts}, indent=2))
            return 0
        result = check(
            args.service,
            credentials_dir=args.credentials_dir,
            unit_dirs=args.unit_dir,
            require_root=require_root,
        )
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else EXIT_REFUSED
    except CredentialRefused as error:
        return _refuse(error)
    except (OSError, ValueError):
        return _refuse(CredentialRefused("store_unavailable"))


if __name__ == "__main__":
    raise SystemExit(main())
