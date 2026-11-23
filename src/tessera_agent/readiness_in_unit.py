"""Run the first-call readiness check in the installed reasoning unit's own context.

The check is executed as the service user inside a transient systemd unit that carries the unit's
``LoadCredential=`` lines and the listed sandbox properties (``SANDBOX_PROPERTIES`` and every
``ReadWritePaths=`` line, so a state directory outside ``/var/lib`` stays writable under
``ProtectSystem=strict``), so ``$CREDENTIALS_DIRECTORY`` holds what the real service gets and the
check runs under the unit's sandbox. It is not a copy of every directive: a directive outside that
list (for example ``PrivateDevices=``) is not replicated.

``Environment=`` values are NOT copied: transient-unit properties travel in the ``systemd-run``
command line, which other users can read. The check needs none of the unit's variables, so none is
passed; the run is refused when the unit sets a credential variable (``FORBIDDEN_ENVIRONMENT``).

A ``LoadCredential=`` whose absolute source is missing makes systemd fail the unit before Python
runs (243/CREDENTIALS), so such a source, and one that is a symlink or not a regular file, is left
out: the check inside then reports that credential as missing. No shell is involved, no value is
echoed in errors, and only the check's own JSON is passed through.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path

MAX_OUTPUT_BYTES = 64 * 1024
_MAX_UNIT_BYTES = 64 * 1024

# The check's documented exit codes: ready, ready except the key, not fully checked, not ready.
CHECK_EXIT_CODES = frozenset({0, 10, 11, 1})
DID_NOT_COMPLETE = "readiness: the in-unit check did not complete"

# Variables that must never be set for the service (start-up refuses them too).
FORBIDDEN_ENVIRONMENT = frozenset({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY"})

# [Service] directives copied into the transient unit, in this order, when the unit sets them.
# StateDirectoryMode travels with StateDirectory so the transient unit never asks systemd for the
# default 0755 on the service's state directory.
SANDBOX_PROPERTIES = (
    "UMask",
    "WorkingDirectory",
    "StateDirectory",
    "StateDirectoryMode",
    "ProtectSystem",
    "ProtectHome",
    "PrivateTmp",
    "NoNewPrivileges",
)


def unit_properties(unit_text: str) -> dict:
    """Parse the fields of an installed unit that the readiness check needs."""
    scalars: dict[str, str] = {}
    credentials: list[str] = []
    environment_names: list[str] = []
    read_write_paths: list[str] = []
    in_service = False
    for raw in unit_text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_service = line == "[Service]"
            continue
        if not in_service or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key == "LoadCredential":
            name, sep, path = value.partition(":")
            if sep and name and path:
                credentials.append(value)
        elif key == "Environment":
            # Only the variable names are kept, never the values.
            try:
                words = shlex.split(value)
            except ValueError:
                words = value.split()
            environment_names.extend(w.partition("=")[0] for w in words if "=" in w)
        elif key == "ReadWritePaths":
            if value:
                read_write_paths.append(value)
        else:
            scalars[key] = value
    if not scalars.get("User"):
        raise ValueError("unit has no User=")
    if not credentials:
        raise ValueError("unit has no LoadCredential=")
    exec_words = scalars.get("ExecStart", "").lstrip("@-+!:").split()
    state_dir = None
    if scalars.get("StateDirectory"):
        state_dir = "/var/lib/" + scalars["StateDirectory"].split()[0]
    elif scalars.get("WorkingDirectory", "").startswith("/"):
        state_dir = scalars["WorkingDirectory"]
    return {
        "user": scalars["User"],
        "group": scalars.get("Group") or None,
        "load_credentials": credentials,
        "credential_names": [c.partition(":")[0] for c in credentials],
        "python": exec_words[0] if exec_words else None,
        "state_dir": state_dir,
        "sandbox": [(name, scalars[name]) for name in SANDBOX_PROPERTIES if scalars.get(name)],
        "environment_names": environment_names,
        "read_write_paths": read_write_paths,
    }


def present_credentials(load_credentials, *, lstat=os.lstat) -> list[str]:
    """Keep the ``LoadCredential=`` lines systemd can load without failing the unit.

    A line whose source is absolute is kept only when the source is a regular file (checked with
    lstat, so a symlink is never followed). A missing source, a symlink, any other file type and a
    source that cannot be stat'd are left out, so the check reports that credential as missing.
    A source that is not absolute (a credential-store name) is kept as written.
    """
    kept = []
    for line in load_credentials:
        source = line.partition(":")[2]
        if not source.startswith("/"):
            kept.append(line)
            continue
        try:
            info = lstat(source)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            kept.append(line)
    return kept


def transient_argv(
    props: dict,
    *,
    state_dir,
    control_state_dir=None,
    control_url=None,
    project_id=None,
    lstat=os.lstat,
) -> list[str]:
    python = props.get("python")
    if not python:
        raise ValueError("unit has no ExecStart interpreter")
    argv = [
        "systemd-run",
        "--pipe",
        "--wait",
        "--collect",
        "--quiet",
        "-p",
        f"User={props['user']}",
    ]
    if props.get("group"):
        argv += ["-p", f"Group={props['group']}"]
    for name, value in props.get("sandbox", ()):
        argv += ["-p", f"{name}={value}"]
    for value in props.get("read_write_paths", ()):
        argv += ["-p", f"ReadWritePaths={value}"]
    for line in present_credentials(props["load_credentials"], lstat=lstat):
        argv += ["-p", f"LoadCredential={line}"]
    argv += [
        "--",
        python,
        "-m",
        "tessera_agent.first_call_readiness",
        "check",
        "--state-dir",
        str(state_dir),
    ]
    if control_state_dir is not None:
        argv += ["--control-state-dir", str(control_state_dir)]
    if control_url:
        argv += ["--control-url", control_url]
    if project_id:
        argv += ["--project-id", project_id]
    return argv


def _default_runner(argv: list[str]) -> tuple[int, str]:
    """Run ``argv``; output larger than ``MAX_OUTPUT_BYTES`` counts as not completed (-1)."""
    process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        argv,
        shell=False,
        env={"PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")},
        stdout=subprocess.PIPE,
        stderr=None,
    )
    with process:
        output = process.stdout.read(MAX_OUTPUT_BYTES + 1)
        if len(output) > MAX_OUTPUT_BYTES:
            process.kill()
            process.wait()
            return -1, ""
        code = process.wait()
    return code, output.decode("utf-8", "replace")


def _read_unit(unit_path) -> str:
    path = Path(unit_path)
    try:
        info = os.lstat(path)
    except OSError:
        raise ValueError("unit file is not readable") from None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("unit file must be a regular non-symlink file")
    if info.st_size > _MAX_UNIT_BYTES:
        raise ValueError("unit file is too large")
    return path.read_text(encoding="utf-8")


def _check_result(code, output) -> tuple[int, str]:
    """Pass the check's JSON report through only for its documented exit codes."""
    if code not in CHECK_EXIT_CODES or not isinstance(output, str):
        return 1, DID_NOT_COMPLETE
    if len(output.encode("utf-8", "replace")) > MAX_OUTPUT_BYTES:
        return 1, DID_NOT_COMPLETE
    try:
        report = json.loads(output)
    except ValueError:
        return 1, DID_NOT_COMPLETE
    if not isinstance(report, dict):
        return 1, DID_NOT_COMPLETE
    return code, output


def run_in_unit(
    unit_path,
    *,
    state_dir=None,
    control_state_dir=None,
    control_url=None,
    project_id=None,
    runner: Callable[[list[str]], tuple[int, str]] | None = None,
    lstat=os.lstat,
) -> tuple[int, str]:
    if runner is None and os.geteuid() != 0:
        raise ValueError("running in the unit context requires root")
    props = unit_properties(_read_unit(unit_path))
    if FORBIDDEN_ENVIRONMENT & set(props.get("environment_names", ())):
        raise ValueError("the unit sets a credential environment variable; remove it")
    state = state_dir or props["state_dir"]
    if not state:
        raise ValueError("no state directory given and the unit defines none")
    argv = transient_argv(
        props,
        state_dir=state,
        control_state_dir=control_state_dir,
        control_url=control_url,
        project_id=project_id,
        lstat=lstat,
    )
    try:
        code, output = (runner or _default_runner)(argv)
    except Exception:  # noqa: BLE001 - any runner failure is "did not complete", never echoed
        return 1, DID_NOT_COMPLETE
    return _check_result(code, output)
