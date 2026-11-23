"""Administrator-only credential enrollment; never an MCP or API secret-input surface.

``enroll`` (the default, no subcommand) creates version 1 from a hidden terminal prompt.
``rotate`` enrolls the next version of an existing reference (hidden prompt, or ``--secret-stdin``
for one pipe or file on stdin). ``renew`` extends the same key's expiry and takes no secret at all.
The command runs as the uid that owns the private 0700 credential directory (the backend enforces
it): that is the owner's action. No secret is ever an argument, an environment variable, an output,
a log line or an error text; a refusal names only a closed category."""

import argparse
import getpass
import json
import os
import sys
import time
import warnings

from tessera_control.salad_secret_files import PrivateFileSecretResolver
from tessera_control.salad_secrets import SecretBuffer


class PrivateParser(argparse.ArgumentParser):
    def error(self, message):
        # Unknown arguments may accidentally contain a credential; never echo them.
        self.exit(2, "Enrollment arguments invalid; use --help (no secret arguments).\n")


def hidden_secret():
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise ValueError("interactive terminal required")
    with warnings.catch_warnings():
        # getpass otherwise falls back to echoing input on unsupported terminals.
        warnings.simplefilter("error", getpass.GetPassWarning)
        value = getpass.getpass("Salad management key (hidden; local entry only): ")
    if not 1 <= len(value) <= 4096 or not all(33 <= ord(char) <= 126 for char in value):
        raise ValueError("credential unavailable")
    return SecretBuffer(bytearray(value.encode("ascii")))


MAX_SECRET = 4096
ROTATE_HINT = (
    "Next: set the profile's credential_version to {version} (for example "
    "`tessera providers update --section salad-profile --key <profile> "
    "--entry-json '{{\"credential_version\": {version}}}'`) and restart the control, guardian "
    "and executor services so they resolve the new version."
)


class Refused(Exception):
    """A closed category only; never a detail that could carry a credential."""

    def __init__(self, category):
        super().__init__(category)
        self.category = category


def stdin_secret(stream=None):
    """The new secret from one pipe or file on stdin (never a terminal): at most 4096 bytes, one
    trailing newline stripped, printable ASCII without spaces. Errors carry no input bytes."""
    stream = sys.stdin if stream is None else stream
    try:
        interactive = stream.isatty()
    except Exception:
        interactive = False
    if interactive:
        raise Refused("stdin_is_a_terminal")
    raw = getattr(stream, "buffer", stream).read(MAX_SECRET + 1)
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    data = bytearray(raw)
    try:
        if data.endswith(b"\n"):
            del data[-1:]
            if data.endswith(b"\r"):
                del data[-1:]
        if not 1 <= len(data) <= MAX_SECRET or not all(33 <= byte <= 126 for byte in data):
            raise Refused("bad_secret_shape")
        return SecretBuffer(data)
    except BaseException:
        for index in range(len(data)):
            data[index] = 0
        raise


def _existing(resolver, reference):
    """Metadata of an existing, unrevoked reference (a missing or revoked one is refused)."""
    try:
        metadata = resolver.metadata(reference)
    except Exception:
        raise Refused("reference_not_found") from None
    if metadata.revoked:
        raise Refused("reference_revoked")
    if metadata.version >= 2**31 - 1:
        raise Refused("version_exhausted")
    return metadata


def enroll_version(resolver, reference, version, secret, expires_at):
    """Enroll ``secret`` (consumed and wiped) as ``version`` of ``reference``: the one primitive
    shared by ``rotate`` and ``tessera credentials reenroll``. A failure names only a category."""
    try:
        with secret:
            resolver.enroll(reference, version, secret, expires_at=expires_at)
    except Exception:
        raise Refused("enrollment_rejected") from None


def rotate(args):
    if not 1 <= args.expires_in_hours <= 8760:
        raise Refused("bad_expiry")
    resolver = PrivateFileSecretResolver(args.credential_directory)
    try:
        previous = _existing(resolver, args.reference)  # before any secret is read
        try:
            secret = stdin_secret() if args.secret_stdin else hidden_secret()
        except Refused:
            raise
        except Exception:
            raise Refused("secret_entry_failed") from None
        version, expires_at = previous.version + 1, time.time() + args.expires_in_hours * 3600
        enroll_version(resolver, args.reference, version, secret, expires_at)
        print(
            json.dumps(
                {
                    "reference": args.reference,
                    "previous_version": previous.version,
                    "version": version,
                    "expires_at": expires_at,
                }
            )
        )
        print(ROTATE_HINT.format(version=version), file=sys.stderr)
    finally:
        resolver.close()


def renew(args):
    if not 1 <= args.expires_in_hours <= 24:
        raise Refused("bad_expiry")
    resolver = PrivateFileSecretResolver(args.credential_directory)
    try:
        previous = _existing(resolver, args.reference)
        expires_at = time.time() + args.expires_in_hours * 3600
        try:
            renewed = resolver.renew(
                args.reference, expected_version=previous.version, expires_at=expires_at
            )
        except Exception:
            raise Refused("renewal_rejected") from None
        print(
            json.dumps(
                {
                    "reference": args.reference,
                    "previous_version": previous.version,
                    "version": renewed.version,
                    "expires_at": renewed.expires_at,
                }
            )
        )
        print(ROTATE_HINT.format(version=renewed.version), file=sys.stderr)
    finally:
        resolver.close()


def subcommand_main(argv):
    parser = PrivateParser(description="Rotate or renew a Salad management credential reference")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("rotate", "renew"):
        sub = commands.add_parser(name)
        sub.add_argument("--credential-directory", required=True)
        sub.add_argument("--reference", required=True)
        sub.add_argument("--expires-in-hours", type=int, default=24)
        if name == "rotate":
            sub.add_argument("--secret-stdin", action="store_true")
    args = parser.parse_args(argv)
    try:
        {"rotate": rotate, "renew": renew}[args.command](args)
        return 0
    except Refused as error:
        print(f"Credential {args.command} refused: {error.category}.", file=sys.stderr)
        return 2
    except (Exception, KeyboardInterrupt):
        print(
            "Private rotation unavailable or cancelled; no credential details emitted.",
            file=sys.stderr,
        )
        return 2


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] in {"rotate", "renew"}:
        return subcommand_main(arguments)
    return enroll_main(arguments)


def enroll_main(argv=None):
    parser = PrivateParser(description="Enroll an existing Salad key from a hidden local terminal")
    parser.add_argument("--credential-directory", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--expires-in-hours", type=int, default=24)
    args = parser.parse_args(argv)
    resolver = None
    try:
        if not 1 <= args.expires_in_hours <= 8760:
            raise ValueError("bounded expiry required")
        # Validate paths/reference and refuse any existing entry BEFORE asking for input.
        resolver = PrivateFileSecretResolver(args.credential_directory)
        name = resolver._name(args.reference)
        try:
            os.stat(name, dir_fd=resolver._directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ValueError("create-only reference exists")
        expires_at = time.time() + args.expires_in_hours * 3600
        with hidden_secret() as secret:
            # Backend serializes enrollment and rejects version 1 over any existing
            # version >=1, including a competing enrollment after the precheck.
            resolver.enroll(args.reference, 1, secret, expires_at=expires_at)
        print(json.dumps({"reference": args.reference, "version": 1, "expires_at": expires_at}))
        return 0
    except (Exception, KeyboardInterrupt):
        print(
            "Private enrollment unavailable or cancelled; no credential details emitted.",
            file=sys.stderr,
        )
        return 2
    finally:
        if resolver is not None:
            resolver.close()


if __name__ == "__main__":
    raise SystemExit(main())
