"""Idempotent, checked installer for the reasoning service's first-call readiness checklist.

``plan`` is read-only. ``apply`` runs only the steps that are not already ``present`` and then
runs the first-call readiness check (ADR 0109), returning its report and exit code unchanged.

What it owns: the service account, ``/etc/tessera`` (0755), the credentials directory (0700), the
state directory (service user, 0700), the prompt file, the profile, the ``model-credential``
reference file (a reference, not a secret) and the systemd unit. What it never does: create,
open, read or stat the API key or the agent token, create ``reasoning.admitted`` (admission is a
human act), or enable/start the service. The API key belongs to ``tessera-service-credential
install`` (ADR 0093); the agent token to the operator's enrollment.

Every file is written through a temp file created exclusively in the same directory with its final
mode, fsynced, renamed over the target and the directory fsynced (the idiom of
``service_credentials``). Symlinks and non-directories at managed paths are refused. Nothing
returned or raised carries a profile value: only names, modes, owners, sizes and numeric caps.
"""

from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
import pwd
import re
import secrets
import stat
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

MAX_PROMPT_BYTES = 8192
MAX_JSON_BYTES = 64 * 1024
REFERENCE_FILE = "reasoning-model-credential"
KEY_FILE = "reasoning-anthropic-api-key"
TOKEN_FILE = "reasoning-agent-token"
ADMITTED_FILE = "reasoning.admitted"
CAP_KEYS = ("max_cost_cents_per_call", "daily_cost_cap_cents", "daily_token_cap")
_CAP_FLAGS = {
    "max_cost_cents_per_call": "--max-cost-cents-per-call",
    "daily_cost_cap_cents": "--daily-cost-cap-cents",
    "daily_token_cap": "--daily-token-cap",
}
_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")
_SAFE_PATH = re.compile(r"/[A-Za-z0-9._/+@=,-]*\Z")

# The shipped unit (deploy/remote-agent-platform/tessera-reasoning.service) with the values this
# command substitutes marked @LIKE_THIS@. A test pins the default rendering to that file.
_UNIT_TEMPLATE = """\
[Unit]
Description=Tessera admitted standalone reasoning supervisor (manual start only)
After=network-online.target
Wants=network-online.target
ConditionPathExists=@ADMITTED@
StartLimitIntervalSec=120
StartLimitBurst=3

[Service]
Type=simple
User=@USER@
Group=@USER@
UMask=0077
@STATE_DIRECTORY@StateDirectoryMode=0700
WorkingDirectory=@STATE@
LoadCredential=profile:@PROFILE@
LoadCredential=prompt:@PROMPT@
LoadCredential=agent-token:@TOKEN@
# The admission marker, by reference only: start-up reads it from $CREDENTIALS_DIRECTORY to refuse a
# malformed or expired recorded admission (exit 78). It holds no secret and is never in env or argv.
LoadCredential=admission:@ADMITTED@
# Default provider: Anthropic Messages API, called directly by Tessera's own client (ADR 0082).
# anthropic-api-key is a root-owned 0600 operator file holding the API key. Tessera reads it from
# $CREDENTIALS_DIRECTORY at call time, keeps it in memory only and sends it only to the allowlisted
# API host; it never reaches the environment, argv, logs, journal or receipts. model-credential is a
# private file naming the anthropic-api-key:// reference and must equal the profile's credential_ref.
# No API-key environment variable is accepted: startup refuses ANTHROPIC_API_KEY,
# ANTHROPIC_AUTH_TOKEN and CLAUDE_API_KEY. The harness password is loaded only by the OpenAI
# drop-in. For an OpenAI profile install the openai drop-in.
LoadCredential=model-credential:@REFERENCE@
LoadCredential=anthropic-api-key:@KEY@
ExecStart=@PYTHON@ -m tessera_agent.service_startup reasoning --state-dir @STATE@
ExecStop=@PYTHON@ -m tessera_agent.service_startup reasoning-stop --state-dir @STATE@
# One-shot: exit 0 is a completed run (or a run identity already completed), so it is never
# restarted. Only a crash (exit 1, a signal) restarts, to resume the journal without replay;
# 2 (halted, blocked or stopped), 77 (refused) and 78 (configuration error) never restart.
Restart=on-failure
RestartPreventExitStatus=2 77 78
RestartSec=5
TimeoutStopSec=40
KillMode=control-group
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=
ReadWritePaths=@STATE@
# Deliberately no [Install]/WantedBy: installing/enabling control cannot start inference.
"""  # noqa: E501


class InstallRefused(ValueError):
    """A clear operator reason. Never a secret and never a value taken from the profile."""


@dataclass(frozen=True)
class Options:
    root: Path | None = None
    user: str = "tessera-reasoning"
    config_dir: Path = Path("/etc/tessera")
    credentials_dir: Path = Path("/etc/tessera/credentials")
    profile_path: Path = Path("/etc/tessera/reasoning.json")
    prompt_path: Path = Path("/etc/tessera/reasoning-prompt.txt")
    state_dir: Path = Path("/var/lib/tessera-reasoning")
    unit_path: Path = Path("/etc/systemd/system/tessera-reasoning.service")
    python: str = "/opt/tessera/venv/bin/python"
    profile_template: Path | None = None
    prompt_source: Path | None = None
    max_cost_cents_per_call: int | None = None
    daily_cost_cap_cents: int | None = None
    daily_token_cap: int | None = None
    control_state_dir: Path | None = None
    control_url: str | None = None
    project_id: str | None = None
    replace: bool = False
    # A fresh run request: a newly generated run_id (and the installed prompt's digest) is written
    # into the profile, so the next manual start is a new one-shot run. The old run stays closed.
    new_run: bool = False

    @property
    def reference_path(self) -> Path:
        return Path(self.credentials_dir) / REFERENCE_FILE


@dataclass(frozen=True)
class Step:
    name: str
    state: str  # 'present' | 'missing' | 'differs'
    action: str  # plain words of what apply would do
    detail: dict[str, Any] = field(default_factory=dict)


class Accounts(Protocol):
    def lookup(self, user: str) -> tuple[int, int] | None: ...

    def create(self, user: str) -> None: ...

    def chown(self, path: Path, uid: int, gid: int) -> None: ...


Runner = Callable[[Sequence[str]], int]


def _default_runner(argv: Sequence[str]) -> int:
    # Fixed argv, never a shell. Output is not captured into any result.
    return subprocess.run(  # noqa: S603
        list(argv), shell=False, check=False, stdin=subprocess.DEVNULL, capture_output=True
    ).returncode


class SystemAccounts:
    """The real account database; creation goes through the injectable runner."""

    def __init__(self, runner: Runner | None = None) -> None:
        self._runner = runner or _default_runner

    def lookup(self, user: str) -> tuple[int, int] | None:
        try:
            entry = pwd.getpwnam(user)
        except KeyError:
            return None
        return entry.pw_uid, entry.pw_gid

    def create(self, user: str) -> None:
        argv = [
            "useradd",
            "--system",
            "--user-group",
            "--no-create-home",
            "--shell",
            "/usr/sbin/nologin",
            user,
        ]
        if self._runner(argv) != 0:
            raise InstallRefused("creating the service account failed (useradd)")

    def chown(self, path: Path, uid: int, gid: int) -> None:
        os.chown(path, uid, gid, follow_symlinks=False)


# -- option handling ---------------------------------------------------------------------------


def _check_options(options: Options) -> None:
    if not _USER.fullmatch(options.user):
        raise InstallRefused("the service user name is not a plain account name")
    paths = {
        "config_dir": options.config_dir,
        "credentials_dir": options.credentials_dir,
        "profile_path": options.profile_path,
        "prompt_path": options.prompt_path,
        "state_dir": options.state_dir,
        "unit_path": options.unit_path,
        "python": options.python,
    }
    for name, value in paths.items():
        text = str(value)
        if not _SAFE_PATH.fullmatch(text) or ".." in text.split("/") or "//" in text:
            raise InstallRefused(f"{name} must be a plain absolute path (no spaces or '..')")
    # The unit has ProtectHome=yes and PrivateTmp=yes: an interpreter there is unreachable.
    if str(options.python).split("/")[1:2] in (["home"], ["root"], ["run"], ["tmp"]):
        raise InstallRefused(
            "python must be outside /home, /root, /run and /tmp: the unit cannot reach it"
        )
    for key in CAP_KEYS:
        value = getattr(options, key)
        if value is not None and type(value) is not int:
            raise InstallRefused(f"{_CAP_FLAGS[key]} must be an integer")


def _real(options: Options, path: Path | str) -> Path:
    """Where a (virtual, absolute) path lives on this host: the root option prefixes it."""
    path = Path(path)
    if options.root is None:
        return path
    return Path(options.root) / str(path).lstrip("/")


def render_unit(options: Options) -> str:
    """The shipped unit with this installation's names substituted (unprefixed paths)."""
    _check_options(options)
    state = Path(options.state_dir)
    credentials = Path(options.credentials_dir)
    state_directory = ""
    if state.parent == Path("/var/lib"):
        state_directory = f"StateDirectory={state.name}\n"
    values = {
        "@ADMITTED@": str(Path(options.config_dir) / ADMITTED_FILE),
        "@STATE_DIRECTORY@": state_directory,
        "@USER@": options.user,
        "@STATE@": str(state),
        "@PROFILE@": str(options.profile_path),
        "@PROMPT@": str(options.prompt_path),
        "@TOKEN@": str(credentials / TOKEN_FILE),
        "@REFERENCE@": str(credentials / REFERENCE_FILE),
        "@KEY@": str(credentials / KEY_FILE),
        "@PYTHON@": options.python,
    }
    text = _UNIT_TEMPLATE
    for token, value in values.items():
        text = text.replace(token, value)
    return text


# -- filesystem helpers ------------------------------------------------------------------------


def _lstat(path: Path, *, want: str) -> os.stat_result | None:
    """lstat a managed path: None when absent; refuse a symlink or the wrong kind."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise InstallRefused(f"cannot inspect {path}") from None
    if stat.S_ISLNK(info.st_mode):
        raise InstallRefused(f"{path} is a symlink; refusing to manage it")
    if want == "dir" and not stat.S_ISDIR(info.st_mode):
        raise InstallRefused(f"{path} exists and is not a directory")
    if want == "file" and not stat.S_ISREG(info.st_mode):
        raise InstallRefused(f"{path} exists and is not a regular file")
    return info


def _check_ancestors(options: Options, path: Path) -> None:
    """Refuse when an existing parent of a managed path is group/world writable."""
    boundary = Path(options.root) if options.root is not None else Path("/")
    parent = path.parent
    while parent != boundary and boundary in parent.parents:
        try:
            info = os.stat(parent)
        except FileNotFoundError:
            parent = parent.parent
            continue
        except OSError:
            raise InstallRefused(f"cannot inspect {parent}") from None
        if not stat.S_ISDIR(info.st_mode):
            raise InstallRefused(f"{parent} is not a directory")
        if info.st_mode & 0o022:
            raise InstallRefused(f"{parent} is group or world writable; refusing to use it")
        parent = parent.parent


def _read_regular(path: Path, limit: int, what: str) -> bytes:
    """Read an operator-named or managed regular file without following a symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise InstallRefused(f"{what} cannot be opened as a regular file") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise InstallRefused(f"{what} is not a regular file")
        if info.st_size > limit:
            raise InstallRefused(f"{what} is larger than {limit} bytes")
        return os.read(fd, limit + 1)
    finally:
        os.close(fd)


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class _Context:
    def __init__(self, options: Options, accounts: Accounts, runner: Runner) -> None:
        self.options, self.accounts, self.runner = options, accounts, runner

    def owner(self, path: Path, info: os.stat_result) -> tuple[int, int]:
        hook = getattr(self.accounts, "owner", None)  # optional: lets tests simulate ownership
        return hook(path) if hook else (info.st_uid, info.st_gid)

    def make_dir(self, path: Path, mode: int, uid: int, gid: int) -> None:
        """Create a directory (and missing parents as root 0755) with exactly this mode/owner."""
        boundary = Path(self.options.root) if self.options.root is not None else Path("/")
        missing = []
        probe = path
        while probe != boundary and boundary in probe.parents and not os.path.lexists(probe):
            missing.append(probe)
            probe = probe.parent
        for directory in reversed(missing):
            leaf = directory == path
            os.mkdir(directory, 0o700)
            os.chmod(directory, mode if leaf else 0o755)
            self.accounts.chown(directory, uid if leaf else 0, gid if leaf else 0)
        if missing:
            _fsync_directory(path.parent)

    def fix_dir(self, path: Path, mode: int, uid: int, gid: int) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
        self.accounts.chown(path, uid, gid)

    def write_file(self, path: Path, content: bytes, mode: int, uid: int, gid: int) -> None:
        directory = path.parent
        self.make_dir(directory, 0o755, 0, 0)
        temp = directory / f".{path.name}.{secrets.token_hex(8)}.tmp"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        try:
            try:
                os.fchmod(fd, mode)
                os.write(fd, content)
                os.fsync(fd)
            finally:
                os.close(fd)
            self.accounts.chown(temp, uid, gid)
            os.replace(temp, path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise
        _fsync_directory(directory)


# -- the steps ---------------------------------------------------------------------------------


@dataclass
class _Item:
    step: Step
    run: Callable[[], str]  # returns a short result word


def _same_meta(ctx: _Context, path: Path, info, mode: int, owner: tuple[int, int] | None) -> bool:
    if stat.S_IMODE(info.st_mode) != mode:
        return False
    return owner is None or ctx.owner(path, info) == owner


def _dir_item(
    ctx: _Context, name: str, virtual: Path, mode: int, owner: Callable[[], tuple[int, int] | None]
) -> _Item:
    path = _real(ctx.options, virtual)
    _check_ancestors(ctx.options, path)
    info = _lstat(path, want="dir")
    expected = owner()
    detail = {"path": str(virtual), "mode": f"{mode:04o}"}

    def run() -> str:
        uid, gid = owner() or (0, 0)
        if info is None:
            ctx.make_dir(path, mode, uid, gid)
            return "created"
        ctx.fix_dir(path, mode, uid, gid)
        return "updated"

    if info is None:
        return _Item(
            Step(name, "missing", f"create the directory {virtual} mode {mode:04o}", detail), run
        )
    if _same_meta(ctx, path, info, mode, expected):
        return _Item(Step(name, "present", "nothing to do", detail), lambda: "unchanged")
    return _Item(Step(name, "differs", f"set mode {mode:04o} and owner on {virtual}", detail), run)


def _read_json_object(path: Path, what: str) -> dict:
    raw = _read_regular(path, MAX_JSON_BYTES, what)
    try:
        value = json.loads(raw)
    except ValueError:
        raise InstallRefused(f"{what} is not valid JSON") from None
    if not isinstance(value, dict):
        raise InstallRefused(f"{what} is not a JSON object")
    return value


def _validate(profile: dict, what: str) -> None:
    """The real start-up validator. Its messages are fixed codes, never profile values."""
    # Imported here: the validator's module needs the mcp extra, which `plan`-less callers
    # (for example a CLI that only prints help) must not require at import time.
    from .remote_reasoning_runtime import validate_profile

    try:
        validate_profile(profile)
    except (ValueError, KeyError, TypeError) as error:
        code = re.search(r"\((anthropic_[a-z_]+)\)", str(error))
        suffix = f" ({code.group(1)})" if code else ""
        raise InstallRefused(f"{what} does not pass profile validation{suffix}") from None


def _serialize(profile: dict) -> bytes:
    return (json.dumps(profile, indent=2) + "\n").encode()


def _file_item(
    ctx: _Context,
    name: str,
    virtual: Path,
    content: bytes | None,
    mode: int,
    *,
    overwrite_needs_replace: bool,
    action_word: str,
    detail: dict[str, Any] | None = None,
) -> _Item:
    """A managed root-owned file. ``content`` None means: keep whatever content is there."""
    path = _real(ctx.options, virtual)
    _check_ancestors(ctx.options, path)
    info = _lstat(path, want="file")
    detail = {"path": str(virtual), "mode": f"{mode:04o}", **(detail or {})}
    if info is None:
        if content is None:
            raise InstallRefused(f"{name}: nothing to write")  # callers guard against this

        def create() -> str:
            ctx.write_file(path, content, mode, 0, 0)
            return "created"

        return _Item(
            Step(name, "missing", f"{action_word} {virtual} mode {mode:04o}", detail), create
        )
    current = _read_regular(path, MAX_JSON_BYTES, name)
    wanted = current if content is None else content
    if current != wanted:
        detail["overwrite"] = True
        if overwrite_needs_replace and not ctx.options.replace:
            detail["refused_without_replace"] = True

        def overwrite() -> str:
            ctx.write_file(path, wanted, mode, 0, 0)
            return "updated"

        return _Item(
            Step(name, "differs", f"replace {virtual} (only with replace)", detail), overwrite
        )
    if _same_meta(ctx, path, info, mode, (0, 0)):
        return _Item(Step(name, "present", "nothing to do", detail), lambda: "unchanged")

    def fix() -> str:
        ctx.write_file(path, wanted, mode, 0, 0)
        return "updated"

    return _Item(
        Step(name, "differs", f"set mode {mode:04o} and root owner on {virtual}", detail), fix
    )


def _prompt_bytes(ctx: _Context) -> tuple[bytes | None, bytes | None]:
    """(bytes to write or None to keep, the bytes the prompt file will hold)."""
    options = ctx.options
    existing = None
    path = _real(options, options.prompt_path)
    if _lstat(path, want="file") is not None:
        existing = _read_regular(path, MAX_PROMPT_BYTES, "the prompt file")
    if options.prompt_source is None:
        if existing is None:
            raise InstallRefused(
                "no prompt exists yet: supply the prompt text file with --prompt-source"
            )
        return None, existing
    data = _read_regular(Path(options.prompt_source), MAX_PROMPT_BYTES, "the prompt source")
    if not data:
        raise InstallRefused("the prompt source is empty")
    return data, data


def _desired_profile(ctx: _Context, prompt_final: bytes) -> tuple[dict, dict | None, dict]:
    """(profile to hold, the existing profile or None, cap and new-run changes)."""
    options = ctx.options
    path = _real(options, options.profile_path)
    _check_ancestors(options, path)
    existing_info = _lstat(path, want="file")
    caps = {k: getattr(options, k) for k in CAP_KEYS if getattr(options, k) is not None}
    if existing_info is not None:
        current = _read_json_object(path, "the existing profile")
        _validate(current, "the existing profile")
        desired = copy.deepcopy(current)
        changes = {}
        limits = desired["limits"]
        for key, value in caps.items():
            if limits.get(key) != value:
                changes[key] = {"from": limits.get(key), "to": value}
                limits[key] = value
        if options.new_run:
            _new_run(desired, prompt_final)
            changes["run_id"] = {"from": "current", "to": "new"}
        if changes:
            _validate(desired, "the profile with the requested changes")
        return desired, current, changes
    if options.profile_template is None:
        raise InstallRefused(
            "no profile exists yet: supply the operator-written profile with --profile-template"
        )
    desired = _read_json_object(Path(options.profile_template), "the profile template")
    limits = desired.get("limits")
    if limits is None:
        limits = {}
    if not isinstance(limits, dict):
        raise InstallRefused("the profile template's limits is not an object")
    desired["limits"] = limits
    for key, value in caps.items():
        limits[key] = value
    absent = [_CAP_FLAGS[k] for k in CAP_KEYS if k not in limits]
    if absent:
        raise InstallRefused("the profile template has no spend caps: supply " + ", ".join(absent))
    desired["prompt_sha256"] = hashlib.sha256(prompt_final).hexdigest()
    if options.new_run:
        _new_run(desired, prompt_final)
    _validate(desired, "the profile built from the template")
    return desired, None, {k: {"from": None, "to": v} for k, v in caps.items()}


def _new_run(profile: dict, prompt_final: bytes) -> None:
    """Point the profile at a new run identity bound to the installed prompt (never printed)."""
    profile["run_id"] = "run-" + secrets.token_hex(8)
    profile["prompt_sha256"] = hashlib.sha256(prompt_final).hexdigest()


def _profile_item(
    ctx: _Context, profile: dict, existing: dict | None, changes: dict, prompt_matches: bool
) -> _Item:
    """The profile: new from the template, or an existing one where only the caps change (and,
    with ``new_run``, the run identity and prompt digest)."""
    virtual = ctx.options.profile_path
    path = _real(ctx.options, virtual)
    detail = {
        "path": str(virtual),
        "mode": "0600",
        "caps": {key: value for key, value in changes.items() if key in CAP_KEYS},
        "new_run": "run_id" in changes,
        "prompt_sha256_matches_prompt": prompt_matches,
    }
    content = _serialize(profile)

    def write(data: bytes) -> Callable[[], str]:
        def run() -> str:
            ctx.write_file(path, data, 0o600, 0, 0)
            return "created" if existing is None else "updated"

        return run

    if existing is None:
        return _Item(
            Step("profile", "missing", f"write the validated profile {virtual} mode 0600", detail),
            write(content),
        )
    if changes:
        action = "update only " + ", ".join(
            f"limits.{key}" if key in CAP_KEYS else "run_id and prompt_sha256 (a new run)"
            for key in changes
        )
        return _Item(Step("profile", "differs", action, detail), write(content))
    info = _lstat(path, want="file")
    if _same_meta(ctx, path, info, 0o600, (0, 0)):
        return _Item(Step("profile", "present", "nothing to do", detail), lambda: "unchanged")
    raw = _read_regular(path, MAX_JSON_BYTES, "the existing profile")
    return _Item(
        Step("profile", "differs", "set mode 0600 and root owner on the profile", detail),
        write(raw),
    )


def _service_user_item(ctx: _Context) -> _Item:
    user = ctx.options.user
    found = ctx.accounts.lookup(user)
    detail = {"user": user}
    if found is not None:
        return _Item(Step("service_user", "present", "nothing to do", detail), lambda: "unchanged")

    def run() -> str:
        ctx.accounts.create(user)
        if ctx.accounts.lookup(user) is None:
            raise InstallRefused("the service account was not created")
        return "created"

    action = "create the system account (no login, no home)"
    return _Item(Step("service_user", "missing", action, detail), run)


def _interpreter_item(ctx: _Context) -> _Item:
    """The interpreter the unit runs. Installing the package is not this command's job: it
    reports whether the interpreter exists and the operator installs the package there."""
    path = _real(ctx.options, ctx.options.python)
    exists = path.is_file() and os.access(path, os.X_OK)
    return _Item(
        Step(
            "interpreter",
            "present" if exists else "missing",
            "the interpreter exists"
            if exists
            else "install Tessera (with its mcp extra) into a virtualenv at this path; this "
            "command does not",
            {},
        ),
        lambda: "skipped",
    )


def _build(ctx: _Context) -> list[_Item]:
    options = ctx.options
    _check_options(options)

    def service_owner() -> tuple[int, int] | None:
        return ctx.accounts.lookup(options.user)

    prompt_write, prompt_final = _prompt_bytes(ctx)
    profile, existing, changes = _desired_profile(ctx, prompt_final)
    reference = (profile["credential_ref"] + "\n").encode()
    prompt_matches = profile.get("prompt_sha256") == hashlib.sha256(prompt_final).hexdigest()

    items = [
        _service_user_item(ctx),
        _dir_item(ctx, "config_dir", options.config_dir, 0o755, lambda: (0, 0)),
        _dir_item(ctx, "credentials_dir", options.credentials_dir, 0o700, lambda: (0, 0)),
        _dir_item(ctx, "state_dir", options.state_dir, 0o700, service_owner),
        _file_item(
            ctx,
            "prompt",
            options.prompt_path,
            prompt_write,
            0o600,
            overwrite_needs_replace=True,
            action_word="copy the prompt to",
            detail={"size": len(prompt_final)},
        ),
    ]
    items.append(_profile_item(ctx, profile, existing, changes, prompt_matches))
    items.append(
        _file_item(
            ctx,
            "model_credential_reference",
            options.reference_path,
            reference,
            0o600,
            overwrite_needs_replace=True,
            action_word="write the reference (not a secret) to",
        )
    )
    items.append(_interpreter_item(ctx))
    unit = render_unit(options).encode()
    unit_item = _file_item(
        ctx,
        "systemd_unit",
        options.unit_path,
        unit,
        0o644,
        overwrite_needs_replace=True,
        action_word="write the unit (no [Install]) to",
    )
    if unit_item.step.state != "present":
        inner = unit_item.run

        def run_unit() -> str:
            result = inner()
            if ctx.runner(["systemctl", "daemon-reload"]) != 0:
                raise InstallRefused("systemctl daemon-reload failed")
            return result

        unit_item = _Item(unit_item.step, run_unit)
    items.append(unit_item)
    return items


def _accounts(options: Options, accounts: Accounts | None, runner: Runner | None) -> Accounts:
    return accounts if accounts is not None else SystemAccounts(runner)


def plan(options: Options, *, accounts: Accounts | None = None) -> list[Step]:
    """Read-only: what apply would do. Writes nothing and runs nothing."""
    ctx = _Context(options, _accounts(options, accounts, None), _refuse_runner)
    return [item.step for item in _build(ctx)]


def _refuse_runner(argv: Sequence[str]) -> int:
    raise InstallRefused("plan never runs commands")


def _exit_codes() -> dict[str, int]:
    from . import first_call_readiness

    return first_call_readiness._EXIT


def credential_files(options: Options) -> dict[str, str]:
    """The names the service sees mapped to the files they are stored as: the unit's
    ``LoadCredential=NAME:PATH`` lines whose PATH is in the credentials directory."""
    names: dict[str, str] = {}
    credentials = str(Path(options.credentials_dir))
    for line in render_unit(options).splitlines():
        if line.startswith("LoadCredential="):
            name, _, path = line.removeprefix("LoadCredential=").partition(":")
            if path.rsplit("/", 1)[0] == credentials:
                names[name] = path.rsplit("/", 1)[1]
    return names


def pending_operator(options: Options) -> list[dict[str, str]]:
    """What stays a human act. Nothing here is opened, read or stat'd."""
    credentials = Path(options.credentials_dir)
    return [
        {
            "name": "api_key",
            "path": str(credentials / KEY_FILE),
            "action": "install the API key with `tessera-service-credential install`",
        },
        {
            "name": "agent_token",
            "path": str(credentials / TOKEN_FILE),
            "action": "install the agent token through the operator's enrollment",
        },
        {
            "name": "spend_limits",
            "path": "PUT /model-spend/limits",
            "action": "a person sets the project's spend limits (nothing is spent until they do)",
        },
        {
            "name": "grant_and_agent_credential",
            "path": "the profile's grant_id, revision, epoch and subject_credential_id",
            "action": (
                "a person enrolls the agent credential and issues a reasoning grant for it with "
                "`tessera authority issue-reasoning` (or POST "
                "/projects/{id}/agent-authority-grants with bounds.kind reasoning), then puts the "
                "issued grant's id, revision and epoch in the profile; the readiness grant check "
                "confirms it"
            ),
        },
        {
            "name": "admission",
            "path": str(Path(options.config_dir) / ADMITTED_FILE),
            "action": (
                "a person admits the service with `tessera reasoning admit` (their own session "
                "records the admission on receipts, then this file is written with a reference "
                "to it; ADR 0163)"
            ),
        },
    ]


# -- the admission marker (ADR 0163) ---------------------------------------------------------------

MAX_ADMITTED_BYTES = 4096


def admitted_path(options: Options) -> Path:
    """Where the unit's ``ConditionPathExists`` marker lives on this host."""
    return _real(options, Path(options.config_dir) / ADMITTED_FILE)


def write_admitted_marker(
    options: Options,
    content: bytes,
    *,
    replace: bool = False,
    accounts: Accounts | None = None,
) -> Path:
    """Write the admission marker atomically: root-owned 0600, through an exclusive temp file
    in the same directory, fsynced, renamed over the target, directory fsynced. The content is a
    non-secret reference to the admission record; the unit only checks that the file exists.

    Refuses a symlink or non-regular file at the path, a group/world-writable parent, a missing
    config directory, and (without ``replace``) any existing marker. Needs root unless a root
    prefix is given (staging and tests), where ``accounts`` performs the chown."""
    _check_options(options)
    if len(content) > MAX_ADMITTED_BYTES:
        raise InstallRefused("the admission marker content is too large")
    if options.root is None and os.geteuid() != 0:
        raise InstallRefused("writing the admission marker needs root")
    if options.root is not None and accounts is None:
        raise InstallRefused("with a root option, writing the marker needs injected accounts")
    path = admitted_path(options)
    _check_ancestors(options, path)
    if _lstat(path.parent, want="dir") is None:
        raise InstallRefused(f"{path.parent} does not exist; install the service first")
    if _lstat(path, want="file") is not None and not replace:
        raise InstallRefused(
            f"{path} already exists; nothing was changed (replace is needed to overwrite it)"
        )
    owner = accounts or SystemAccounts(_refuse_runner)
    directory = path.parent
    temp = directory / f".{path.name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, content)
            os.fsync(fd)
        finally:
            os.close(fd)
        owner.chown(temp, 0, 0)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    _fsync_directory(directory)
    return path


def remove_admitted_marker(options: Options) -> bool:
    """Remove the marker (the unit can then no longer start). False when it was absent.
    Refuses a symlink or a non-regular file. Needs root unless a root prefix is given."""
    _check_options(options)
    if options.root is None and os.geteuid() != 0:
        raise InstallRefused("removing the admission marker needs root")
    path = admitted_path(options)
    if _lstat(path, want="file") is None:
        return False
    os.unlink(path)
    _fsync_directory(path.parent)
    return True


def apply(
    options: Options,
    *,
    accounts: Accounts | None = None,
    runner: Runner | None = None,
    readiness: Callable[..., dict] | None = None,
) -> dict[str, Any]:
    """Run the steps that are not present, then the readiness check."""
    _check_options(options)
    if options.root is None and os.geteuid() != 0:
        raise InstallRefused("apply needs root")
    if options.root is not None and (accounts is None or runner is None):
        raise InstallRefused("with a root option, apply needs injected accounts and runner")
    run = runner or _default_runner
    ctx = _Context(options, _accounts(options, accounts, run), run)
    items = _build(ctx)
    for item in items:
        if item.step.detail.get("refused_without_replace"):
            raise InstallRefused(
                f"{item.step.name}: {item.step.detail['path']} exists with other content; "
                "nothing was changed (replace is needed to overwrite it)"
            )
    results: list[dict[str, str]] = []
    failed = None
    for item in items:
        if item.step.state == "present":
            results.append({"name": item.step.name, "result": "unchanged"})
            continue
        try:
            # Re-resolve right before acting: an earlier step may have changed what this needs.
            fresh = next(i for i in _build(ctx) if i.step.name == item.step.name)
            results.append({"name": item.step.name, "result": fresh.run()})
        except InstallRefused as error:
            failed = {"name": item.step.name, "result": "failed", "error": str(error)}
        except OSError as error:
            code = errno.errorcode.get(error.errno or 0, "error")
            failed = {
                "name": item.step.name,
                "result": "failed",
                "error": f"filesystem error ({code})",
            }
        if failed:
            results.append(failed)
            break
    report: dict[str, Any] = {
        "steps": results,
        "pending_operator": pending_operator(options),
    }
    if failed:
        report.update(readiness=None, exit_code=1, error=failed["error"])
        return report
    kwargs = {
        "profile_path": _real(options, options.profile_path),
        "credentials_directory": _real(options, options.credentials_dir),
        "state_dir": _real(options, options.state_dir),
        "control_state_dir": options.control_state_dir,
        "control_url": options.control_url,
        "project_id": options.project_id,
        # Run by the installer (root), the check expects what the install made: the state
        # directory belongs to the service account, the key file (ADR 0093) to root.
        "state_owner_uid": ctx.accounts.lookup(options.user)[0]
        if ctx.accounts.lookup(options.user)
        else None,
        "key_owner_uid": 0,
        # The check is pointed at the stored directory, where the files carry their stored names.
        "credential_files": credential_files(options),
        "admitted_path": admitted_path(options),
    }
    from . import first_call_readiness

    result = (readiness or first_call_readiness.run_checks)(**kwargs)
    report["readiness"] = result
    report["exit_code"] = _exit_codes().get(result.get("overall"), 1)
    return report
