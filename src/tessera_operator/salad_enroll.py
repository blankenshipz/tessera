"""Administrator-only hidden-terminal entry; never an MCP secret-input surface."""

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


def main(argv=None):
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
