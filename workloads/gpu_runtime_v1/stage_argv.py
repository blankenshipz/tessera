"""Generic stage launcher (ADR 0076): run a reviewed argv the way a local-docker stage runs it.

Started by the runtime as ``python stage_argv.py WORK OUT -- ARGV...``. The argv is the stage's
own, carried in the hash-pinned manifest. The local-docker worker mounts a stage's input at
``/inputs``, its pinned public files at ``/inputs-public/<input>/<name>`` (ADR 0097) and its
outputs at ``/outputs``; here those are ``WORK/input``, ``WORK/files/<input>/<name>`` (where the
runtime put the verified fetches) and ``OUT``, so an argument that is exactly one of them, starts
with it plus ``/``, or is such a path after ``--name=`` is rewritten. Nothing else is touched and
no environment is added. The child runs in WORK with no stdin; this exits with the child's exit
code (1 when it died on a signal).
Standard library only.
"""

import os
import subprocess
import sys

NOT_FOUND = 127


def _map(value, mounts):
    for prefix, target in mounts:
        if value == prefix:
            return target
        if value.startswith(prefix + "/"):
            return target + value[len(prefix) :]
    return value


def rewrite(argv, work, out_dir):
    """The argv with the container mount points replaced by this run's directories."""
    mounts = (
        ("/inputs", os.path.join(work, "input")),
        ("/inputs-public", os.path.join(work, "files")),
        ("/outputs", out_dir),
    )
    mapped = []
    for token in argv:
        if token.startswith("--") and "=" in token:
            name, _, value = token.partition("=")
            mapped.append(f"{name}={_map(value, mounts)}")
        else:
            mapped.append(_map(token, mounts))
    return mapped


def main(args):
    if len(args) < 4 or args[2] != "--":
        return 2
    work, argv = args[0], rewrite(args[3:], args[0], args[1])
    try:
        code = subprocess.run(
            argv, cwd=work, stdin=subprocess.DEVNULL, check=False, start_new_session=False
        ).returncode
    except OSError:
        return NOT_FOUND
    return code if code > 0 else (1 if code < 0 else 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
