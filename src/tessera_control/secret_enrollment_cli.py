"""One-shot native helper. No argv/env secret inputs; stdout is closed metadata only.

The parent supplies authentication + metadata on its private inherited stdin pipe.
Secret entry is a hidden native macOS dialog, or --secret-fd for a private one-shot
pipe import. There is no general get/read command and no automatic retry.
"""

import argparse
import json
import os
import select
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

from .os_secret_store import MAX_SECRET_BYTES, SecretStoreError, platform_store, validate_value
from .protected_secrets import ProtectedRequest, ProtectedSecretStore


def private_channel(fd):
    info = os.fstat(fd)
    # Anonymous inherited channels, not paths, terminals or regular files. No
    # reopening a path or accepting a renderer-selected descriptor number.
    if info.st_uid != os.geteuid() or not (
        stat.S_ISFIFO(info.st_mode) or stat.S_ISSOCK(info.st_mode)
    ):
        raise SecretStoreError()


def bounded_read(fd, limit, *, newline=False, seconds=10):
    private_channel(fd)
    value = bytearray()
    deadline = time.monotonic() + seconds
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise SecretStoreError()
            part = os.read(fd, 1 if newline else min(4096, limit + 1 - len(value)))
            if not part or (newline and part == b"\n"):
                return value
            value.extend(part)
            if len(value) > limit:
                raise SecretStoreError()
    except BaseException:
        value[:] = b"\0" * len(value)
        raise


def native_prompt(state):
    if sys.platform != "darwin":
        raise SecretStoreError()
    # All substitutions come from closed, server-validated metadata. No secret
    # or renderer-supplied prompt text ever appears in argv/script/error output.
    message = (
        f"Tessera protected enrollment: {state['spec']['purpose']} / "
        f"{state['spec']['provider']} / {state['spec']['audience']}\\n"
        f"Project {state['project_id']} / version {state['version']}\\n"
        "Never enter a Tessera login token here. Provider activation remains disabled."
    )
    script = (
        'set answer to display dialog "' + message + '" default answer "" '
        'with hidden answer buttons {"Cancel", "Enroll"} default button "Enroll" '
        "giving up after 60\n"
        "if gave up of answer then error number -128\n"
        "return text returned of answer"
    )
    process = subprocess.Popen(
        ["/usr/bin/osascript", "-e", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"},
    )
    value = bytearray()
    try:
        value = bounded_read(process.stdout.fileno(), MAX_SECRET_BYTES + 1, seconds=65)
        if process.wait(timeout=2) != 0:
            raise SecretStoreError()
        # osascript adds one transport newline; embedded/newline credentials fail validation.
        if value.endswith(b"\n"):
            del value[-1:]
        validate_value(value)
        return value
    except BaseException:
        value[:] = b"\0" * len(value)
        raise
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


def emit(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def run(state_dir, secret_fd=None):
    raw = bounded_read(0, 8192, newline=True)
    value = bytearray()
    try:
        envelope = json.loads(raw)
        if set(envelope) != {"token", "request"} or not isinstance(envelope["token"], str):
            raise SecretStoreError()
        request = ProtectedRequest.model_validate(envelope["request"]).model_dump()
        store = ProtectedSecretStore(state_dir)
        actor = store.authenticate(envelope.pop("token"))
        backend = platform_store()
        if backend.status != "macos_keychain":
            raise SecretStoreError()
        if request["operation"] == "health":
            emit({"phase": "result", **store.health(actor, request, backend)})
            return
        if request["operation"] == "purge":
            state = store.purge(actor, request, backend)
        else:
            state = store.claim(actor, request)
            value = (
                bounded_read(secret_fd, MAX_SECRET_BYTES, seconds=60)
                if secret_fd is not None
                else native_prompt(state)
            )
            validate_value(value)
            # Parent must recheck current connection AND selected project after
            # the native prompt, before allowing any secret-store write.
            emit({"phase": "confirm"})
            confirmation = bounded_read(0, 32, newline=True)
            try:
                if confirmation != b'{"commit":true}':
                    raise SecretStoreError()
            finally:
                confirmation[:] = b"\0" * len(confirmation)
            state = store.finish(actor, request, value, backend)
        emit(
            {
                "phase": "result",
                "version": state["version"],
                "state": state["state"],
                "cleanup_pending": state["cleanup_pending"],
                "store": backend.status,
            }
        )
    finally:
        raw[:] = b"\0" * len(raw)
        value[:] = b"\0" * len(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--secret-fd", type=int)
    args = parser.parse_args()
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

        def interrupted(*unused):
            raise SecretStoreError()

        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGALRM, interrupted)
        signal.alarm(90)
        if args.secret_fd is not None and args.secret_fd < 3:
            raise SecretStoreError()
        # No keychain access without an explicit request and authenticated local state.
        run(args.state_dir, args.secret_fd)
        return 0
    except Exception:
        # Never echo validation input, exception text, native stderr or tracebacks.
        emit({"phase": "error", "reason": "protected_enrollment_failed_inspect_metadata"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
