"""``tessera credentials reenroll PROVIDER``: the owner re-enrolls an expired provider credential.

The secret comes ONLY from stdin (one pipe or file, never a terminal); no option, environment
variable or positional can carry it. Order: read the existing metadata (the reference must exist
and not be revoked), read the secret, VALIDATE it with a read-only provider call, and only then
store version previous+1 (or an explicit larger ``--version``). A rejected secret stores and bumps
nothing. The receipt and every message name a closed category, never the secret or a value derived
from it. Adding a provider is one ``register`` call with an adapter factory."""

from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import credential_health

RECEIPT_SCHEMA = "tessera.credential-reenroll-receipt.v1"
MAX_EXPIRY_HOURS = 8760
_GENERIC_NEXT = (
    "Next: point the provider profile at version {version} and restart the services "
    "that resolve this credential."
)


DEFAULT_EXPIRY_HOURS = 72
_RECEIPT_REASONS = {
    "stored_receipt_exists": "a receipt file with that name already exists and was left unchanged",
    "stored_receipt_link_unsupported": (
        "this filesystem supports neither hard links nor a rename that refuses to overwrite"
    ),
    "stored_receipt_unwritable": "the receipt directory could not be written",
}


class ReenrollStored(Exception):  # noqa: N818 - the version is stored, a later step failed
    """The new version was stored but the receipt could not be written."""

    def __init__(self, category: str, receipt: dict[str, Any], after_note: str = "") -> None:
        super().__init__(category)
        self.category, self.receipt, self.after_note = category, receipt, after_note


class ReenrollRefused(Exception):  # noqa: N818 - a refusal, named for the CLI contract
    """A closed category only; never a detail that could carry a credential."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


@dataclass(frozen=True)
class ReenrollContext:
    """What an adapter factory may use. ``transport`` is the provider-read hook (None: the
    vetted production transport); tests inject a fake so no network is ever touched."""

    credential_directory: str
    profiles_file: str | None = None
    organization: str | None = None
    transport: Callable[..., None] | None = None
    now: Callable[[], float] = time.time


@dataclass(frozen=True)
class ProfileInfo:
    pinned_version: int | None = None
    organization: str | None = None


@dataclass(frozen=True)
class ReenrollAdapter:
    provider_id: str
    # metadata(reference) -> the stored header dict (version, expires_at, revoked); raises
    # ReenrollRefused("reference_not_found") when missing. Never reads the secret value.
    metadata: Callable[[str], dict[str, Any]]
    # store(secret, reference, version, expires_at): the provider's install/enroll path; it
    # consumes and wipes ``secret``.
    store: Callable[[Any, str, int, float], None]
    # validate(secret, info): a READ-ONLY provider call; raises ReenrollRefused(category).
    validate: Callable[[Any, ProfileInfo], None]
    # profile(reference) -> the profile's pinned version / provider scope (profiles file).
    profile: Callable[[str], ProfileInfo] = lambda reference: ProfileInfo()
    next_step: Callable[[int], str] = lambda version: _GENERIC_NEXT.format(version=version)
    close: Callable[[], None] = lambda: None
    # Printed after every successful re-enrollment: what the owner still does at the provider.
    after_note: str = ""

    def health(self, reference: str, pinned_version: int | None, now: float) -> dict[str, Any]:
        try:
            metadata = self.metadata(reference)
        except ReenrollRefused:
            metadata = None
        return credential_health.evaluate(metadata, now, pinned_version=pinned_version)


_ADAPTERS: dict[str, Callable[[ReenrollContext], ReenrollAdapter]] = {}


def register(provider_id: str, factory: Callable[[ReenrollContext], ReenrollAdapter]) -> None:
    """The single registration a new provider needs."""
    _ADAPTERS[provider_id] = factory


def provider_ids() -> list[str]:
    return sorted(_ADAPTERS)


# -- salad ----------------------------------------------------------------------------------


SALAD_AFTER_NOTE = (
    "Replacing the key here does not revoke the previous one: after the services have restarted "
    "and resolve the new version, delete the old key in the Salad console."
)


def _salad_adapter(ctx: ReenrollContext) -> ReenrollAdapter:
    from tessera_operator import salad_enroll

    from .salad_secret_files import PrivateFileSecretResolver

    try:
        resolver = PrivateFileSecretResolver(ctx.credential_directory, now=ctx.now)
    except Exception:
        raise ReenrollRefused("credential_store_unavailable") from None

    def metadata(reference: str) -> dict[str, Any]:
        try:
            return resolver.read_metadata(reference)
        except Exception:
            raise ReenrollRefused("reference_not_found") from None

    def store(secret, reference, version, expires_at):
        try:
            salad_enroll.enroll_version(resolver, reference, version, secret, expires_at)
        except salad_enroll.Refused as error:
            raise ReenrollRefused(error.category) from None

    def validate(secret, info):
        organization = ctx.organization or info.organization
        if organization is None:
            raise ReenrollRefused("validation_target_unknown")
        read = ctx.transport
        if read is None:
            from .salad_http import check_management_credential as read
        try:
            read(organization, secret)
        except Exception as error:
            reason = getattr(error, "reason", None)
            # credential_rejected: the provider refused the key (401/403). provider_unavailable:
            # no usable answer (an outage, a gateway page, another status), not a verdict on the
            # key. organization_invalid: the organization name is malformed.
            known = {
                "credential_unavailable",
                "credential_rejected",
                "provider_unavailable",
                "organization_invalid",
                "malformed_response",
            }
            if reason == "credential_rejected":
                raise ReenrollRefused("validation_rejected") from None
            raise ReenrollRefused(
                f"validation_{reason}" if reason in known else "validation_failed"
            ) from None

    def profile(reference: str) -> ProfileInfo:
        if not ctx.profiles_file:
            return ProfileInfo()
        from .salad_campaign_profiles import load_profiles

        try:
            profiles = load_profiles(ctx.profiles_file).profiles
        except Exception:
            raise ReenrollRefused("profiles_unreadable") from None
        found = [p for p in profiles if p.credential_reference == reference]
        if not found:
            raise ReenrollRefused("profile_reference_unknown")
        if len({(p.credential_version, p.salad_organization) for p in found}) != 1:
            raise ReenrollRefused("profile_pin_ambiguous")
        return ProfileInfo(found[0].credential_version, found[0].salad_organization)

    return ReenrollAdapter(
        "salad",
        metadata,
        store,
        validate,
        profile,
        lambda version: salad_enroll.ROTATE_HINT.format(version=version),
        resolver.close,
        SALAD_AFTER_NOTE,
    )


register("salad", _salad_adapter)


# -- the flow -------------------------------------------------------------------------------


def _read_secret(stream):
    from tessera_operator import salad_enroll

    try:
        return salad_enroll.stdin_secret(stream)
    except salad_enroll.Refused as error:
        raise ReenrollRefused(error.category) from None
    except Exception:
        raise ReenrollRefused("secret_entry_failed") from None


# os.link() errnos meaning "this filesystem cannot hard-link" for a file we just created (link
# count 1, same directory): EPERM (e.g. FAT/exFAT, some FUSE mounts), ENOTSUP/EOPNOTSUPP (network
# and FUSE filesystems), EMLINK (a link-count limit of 1). EXDEV is NOT here: the temp file is in
# the same directory, so a cross-device answer is unexpected and is refused, not worked around.
# EEXIST is never here: an existing receipt is refused, never replaced.
_LINK_UNSUPPORTED_ERRNOS = frozenset({errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK})
_NOREPLACE_UNSUPPORTED_ERRNOS = frozenset(
    {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}
)
_AT_FDCWD = -100  # Linux
_RENAME_NOREPLACE = 1  # Linux renameat2 flag
_RENAME_EXCL = 0x4  # macOS renamex_np flag


class _NoReplaceUnsupported(Exception):
    """Neither a hard link nor a rename that refuses to overwrite is available here."""


def _rename_noreplace(source: Path, target: Path) -> None:
    """Atomically rename ``source`` to ``target``, failing with FileExistsError if ``target``
    exists. Raises _NoReplaceUnsupported where the platform or filesystem cannot do that; never
    falls back to a plain rename/replace, which would overwrite. A seam for tests."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:
        raise _NoReplaceUnsupported from None
    src, dst = os.fsencode(source), os.fsencode(target)
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        call = libc.renameat2
        call.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        call.restype = ctypes.c_int
        result = call(_AT_FDCWD, src, _AT_FDCWD, dst, _RENAME_NOREPLACE)
    elif sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        call = libc.renamex_np
        call.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        call.restype = ctypes.c_int
        result = call(src, dst, _RENAME_EXCL)
    else:
        raise _NoReplaceUnsupported
    if result == 0:
        return
    code = ctypes.get_errno()
    if code in _NOREPLACE_UNSUPPORTED_ERRNOS:
        raise _NoReplaceUnsupported
    raise OSError(code, os.strerror(code))  # EEXIST becomes FileExistsError


def _place_receipt(temp: Path, path: Path) -> bool:
    """Put ``temp`` at ``path`` without ever replacing an existing file; True if ``temp`` was
    moved (renamed) rather than linked. Raises ReenrollRefused with the precise category."""
    try:
        try:
            os.link(temp, path)
            return False
        except FileExistsError:
            raise
        except OSError as error:
            if error.errno not in _LINK_UNSUPPORTED_ERRNOS:
                raise
        _rename_noreplace(temp, path)
        return True
    except FileExistsError:
        raise ReenrollRefused("receipt_exists") from None
    except _NoReplaceUnsupported:
        raise ReenrollRefused("receipt_link_unsupported") from None
    except OSError:
        raise ReenrollRefused("receipt_unwritable") from None


def _write_receipt(directory: Path, receipt: dict[str, Any]) -> Path:
    """Write the receipt atomically: an owner-only temp file in the same directory, fsync, then a
    hard link (or, where hard links are unsupported, a no-replace rename) to the final name.
    Refuses with ``receipt_exists``, ``receipt_link_unsupported`` or ``receipt_unwritable``; a
    refusal leaves no final file written by this call and no temp file."""
    slug = re.sub(r"[^a-z0-9]+", "-", receipt["reference"].lower()).strip("-")
    path = (
        directory / f"credential-reenroll-{receipt['provider']}-{slug}-v{receipt['version']}.json"
    )
    payload = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    temp = directory / f".{path.name}.{os.getpid()}.{os.urandom(6).hex()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    created = moved = False
    try:
        try:
            fd = os.open(temp, flags, 0o600)
            created = True
            with os.fdopen(fd, "w") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            moved = _place_receipt(temp, path)
        finally:
            if created and not moved:  # never remove a name this call did not create
                try:
                    os.unlink(temp)
                except FileNotFoundError:
                    pass
    except OSError:
        raise ReenrollRefused("receipt_unwritable") from None
    try:  # best effort: make the new directory entry durable
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass
    return path


def reenroll(
    provider: str,
    ctx: ReenrollContext,
    reference: str,
    *,
    stdin=None,
    version: int | None = None,
    expires_in_hours: int = DEFAULT_EXPIRY_HOURS,
    receipt_directory: str | None = None,
) -> dict[str, Any]:
    """Run the flow; return ``{"receipt", "receipt_path", "finding", "next_step"}``."""
    factory = _ADAPTERS.get(provider)
    if factory is None:
        raise ReenrollRefused("unknown_provider")
    if not 1 <= expires_in_hours <= MAX_EXPIRY_HOURS:
        raise ReenrollRefused("bad_expiry")
    directory = Path(receipt_directory) if receipt_directory else None
    if directory is not None and not (directory.is_absolute() and directory.is_dir()):
        raise ReenrollRefused("receipt_directory_invalid")
    adapter = factory(ctx)
    try:
        previous = adapter.metadata(reference)  # before any secret is read
        if previous.get("revoked") is True:
            raise ReenrollRefused("reference_revoked")
        before_version = previous.get("version")
        if type(before_version) is not int or before_version >= 2**31 - 1:
            raise ReenrollRefused("version_exhausted")
        new_version = before_version + 1 if version is None else version
        if new_version <= before_version or new_version > 2**31 - 1:
            raise ReenrollRefused("version_not_greater")
        info = adapter.profile(reference)
        now = ctx.now()
        before = adapter.health(reference, info.pinned_version, now)
        secret = _read_secret(sys.stdin if stdin is None else stdin)
        try:
            adapter.validate(secret, info)  # a rejection stores and bumps nothing
        except BaseException:
            secret.zeroize()
            raise
        expires_at = now + expires_in_hours * 3600
        adapter.store(secret, reference, new_version, expires_at)
        pinned = info.pinned_version if info.pinned_version is not None else new_version
        after = adapter.health(reference, pinned, ctx.now())
    finally:
        adapter.close()
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "provider": provider,
        "reference": reference,
        "previous_version": before_version,
        "version": new_version,
        "expires_at": expires_at,
        "validation": "ok",
        "health_before": before["state"],
        "health_after": after["state"],
        "at": datetime.fromtimestamp(ctx.now(), UTC).isoformat(timespec="seconds"),
    }
    try:
        path = _write_receipt(directory, receipt) if directory is not None else None
    except ReenrollRefused as error:
        # The version IS stored: say so, and hand back the receipt so the record is not lost.
        raise ReenrollStored(f"stored_{error.category}", receipt, adapter.after_note) from None
    cleared = after["state"] == "ok"
    return {
        "after_note": adapter.after_note,
        "receipt": receipt,
        "receipt_path": None if path is None else str(path),
        "finding": "cleared" if cleared else after["state"],
        "reason": after["reason"],
        "next_step": (
            None
            if cleared
            else adapter.next_step(new_version)
            if after["state"] == "version_mismatch"
            else _expiry_advice(after["state"], new_version)
        ),
    }


def _expiry_advice(state: str, version: int) -> str:
    if state == "warning":
        return (
            "The stored credential is inside its expiry warning window: re-run with a longer "
            "--expires-in-hours (more than 24)."
        )
    return (
        f"Version {version} is stored, but this service reads it as {state}: check that this "
        "credential directory is the one the services use and that its clock is right."
    )


# -- CLI ------------------------------------------------------------------------------------


def add_parser(subcommands: argparse._SubParsersAction) -> None:
    group = subcommands.add_parser(
        "credentials", help="owner credential commands (secrets only ever on stdin)"
    )
    group.error = _no_echo_error(group)  # type: ignore[method-assign]
    actions = group.add_subparsers(dest="credentials_action", required=True)
    command = actions.add_parser(
        "reenroll",
        help="re-enroll the next version of a provider credential (secret on stdin)",
        description=(
            "Reads the new secret from stdin only (a pipe or file, never a terminal), validates "
            "it with a read-only provider call, then stores the next version. No option or "
            "environment variable carries the secret."
        ),
    )
    command.add_argument("provider")
    command.add_argument("--credential-directory", required=True)
    command.add_argument("--reference", required=True)
    command.add_argument("--version", type=int)
    command.add_argument(
        "--expires-in-hours",
        type=int,
        default=DEFAULT_EXPIRY_HOURS,
        help=f"lifetime of the stored credential (default {DEFAULT_EXPIRY_HOURS}; more than 24 "
        "so the health check reads ok, not warning)",
    )
    command.add_argument("--receipt-directory")
    command.add_argument("--profiles-file")
    command.add_argument("--organization", help="provider organization when no profile names it")
    command.error = _no_echo_error(command)  # type: ignore[method-assign]


def _no_echo_error(command: argparse.ArgumentParser):
    def error(message: str):
        # An unknown argument may accidentally contain a credential; never echo it.
        command.exit(2, "Credential re-enrollment arguments invalid; use --help.\n")

    return error


def run(arguments: argparse.Namespace, *, stdin=None, transport=None) -> int:
    try:
        result = reenroll(
            arguments.provider,
            ReenrollContext(
                arguments.credential_directory,
                arguments.profiles_file,
                arguments.organization,
                transport,
            ),
            arguments.reference,
            stdin=stdin,
            version=arguments.version,
            expires_in_hours=arguments.expires_in_hours,
            receipt_directory=arguments.receipt_directory,
        )
    except ReenrollRefused as error:
        print(f"Credential re-enrollment refused: {error.category}.", file=sys.stderr)
        return 2
    except ReenrollStored as stored:
        reason = _RECEIPT_REASONS.get(
            stored.category, _RECEIPT_REASONS["stored_receipt_unwritable"]
        )
        print(
            f"Credential re-enrollment: {stored.category}. The new version "
            f"{stored.receipt['version']} IS stored; only the receipt file was NOT written "
            f"({reason}). "
            "The receipt follows on stdout so the record is not lost.",
            file=sys.stderr,
        )
        print(json.dumps(stored.receipt, indent=2, sort_keys=True))
        if stored.after_note:
            print(stored.after_note, file=sys.stderr)
        return 3
    except Exception:  # noqa: BLE001 - never echo exception text near a secret
        print("Credential re-enrollment failed unexpectedly; no details emitted.", file=sys.stderr)
        return 1
    if result["receipt_path"] is None:
        print(json.dumps(result["receipt"], indent=2, sort_keys=True))
    else:
        print(f"receipt: {result['receipt_path']}")
    if result["after_note"]:
        print(result["after_note"], file=sys.stderr)
    if result["finding"] == "cleared":
        print("credential-expiry-health: CLEARED (state ok)", file=sys.stderr)
    else:
        print(
            f"credential-expiry-health: NOT CLEARED ({result['finding']}: {result['reason']})",
            file=sys.stderr,
        )
        print(result["next_step"], file=sys.stderr)
    return 0


def usage_line(provider: str = "<provider>") -> str:
    return (
        f"tessera credentials reenroll {provider} --credential-directory DIR --reference REF "
        "[--version N] [--expires-in-hours H] [--receipt-directory DIR] [--profiles-file FILE]"
        " < secret-file"
    )
