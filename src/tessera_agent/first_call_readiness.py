"""Operator readiness check for the first receipted call of the on-platform agent loop.

``tessera-reasoning-readiness check`` verifies everything the first call needs EXCEPT the key
value: the profile, the credential reference file, the model-runtime egress allowlist, the receipts
store, the control-plane slices and the model-spend caps. The API key file is looked at by
``stat`` only (present, owner, mode, size): it is never opened, read, hashed or printed, so no
function here can leak it. A deployment that is complete apart from the key file reports
``ready_except_key``; anything else that is missing or wrong reports ``not_ready``. A check that
could not run (``unchecked``, for example the spend caps without ``--control-state-dir``) is NOT
a pass: it keeps the report from saying ``ready`` or ``ready_except_key`` and the result is
``not_fully_checked`` (its own non-zero exit status), because an unverified spend limit is exactly
what makes the first call fail after the key is installed.

It makes no model call and no outbound call except two unauthenticated probes of the control
plane's own ``/health/ready`` and ``/model-spend/limits`` routes when ``--control-url`` is given
(no credential is ever sent). It writes only a short-lived probe file inside the receipts
directory and removes it.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .model_destinations import (
    ANTHROPIC_ACCOUNT_MODE,
    FORBIDDEN_ENVIRONMENT,
    DestinationRefused,
    destination,
    model_runtime_allowlist,
    model_runtime_egress_allowed,
    model_tier,
    require_tier_admitted,
    validate_credential_ref,
)
from .remote_reasoning_runtime import validate_profile
from .remote_reasoning_store import private_file
from .service_startup import (
    ANTHROPIC_API_KEY_FILE,
    MODEL_CREDENTIAL_FILE,
    StartupBlocked,
    credential_root,
    require_model_credential,
)

PROVIDER = "anthropic"
EXPECTED_EGRESS = frozenset({("api.anthropic.com", 443)})
# A key is 1..512 printable bytes (the runtime's shape); one trailing newline is tolerated on disk.
KEY_FILE_MAX_BYTES = 513
MIN_FREE_BYTES = 16 * 1024 * 1024
PROBE_TIMEOUT_SECONDS = 3
EXIT_READY, EXIT_READY_EXCEPT_KEY, EXIT_NOT_READY, EXIT_USAGE = 0, 10, 1, 2
EXIT_NOT_FULLY_CHECKED = 11
SUMMARIES = {
    "ready": "ready: every check passed; the first receipted call can run.",
    "ready_except_key": (
        "ready except key: install the API key with tessera-service-credential install, "
        "then start the service"
    ),
    "not_ready": "not ready: fix the failing checks above, then run the check again.",
    # {names} is the checks that could not run; this is not a pass.
    "not_fully_checked": (
        "not fully checked: {names} could not be verified (pass --control-url and "
        "--control-state-dir, or check the limits through GET /model-spend); no check failed, "
        "but this is not a pass and the first call may still be refused"
    ),
}
_ROUTE_EXISTS = {401, 403, 405, 422}

Opener = Callable[[str, str, float], int]


def _check(name: str, status: str, detail: str, fix: str | None = None) -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, "fix": fix}


# -- individual checks -----------------------------------------------------------------------------


def _environment(environment: Mapping[str, str]) -> dict[str, Any]:
    present = [name for name in FORBIDDEN_ENVIRONMENT if name in environment]
    if present:
        return _check(
            "environment",
            "fail",
            "an API-key fallback variable is set: " + ", ".join(present),
            "unset it in the service unit, its drop-ins and environment files; the key is "
            "delivered only as a systemd credential",
        )
    return _check("environment", "ok", "no API-key fallback variable is set")


def _load_profile(path: Path) -> tuple[dict | None, dict[str, Any]]:
    try:
        profile = json.loads(private_file(path))
    except (OSError, ValueError):
        return None, _check(
            "profile",
            "fail",
            "the profile is missing, unreadable or not a private (0600, owned) regular file",
            "install the reasoning profile as a root-owned 0600 file and load it with "
            "LoadCredential=profile:...",
        )
    if not isinstance(profile, dict):
        return None, _check("profile", "fail", "the profile is not a JSON object")
    try:
        validate_profile(profile)
        found = destination(profile["model"]["providerID"], profile["account_mode"])
    except (ValueError, KeyError, TypeError, DestinationRefused) as error:
        # validate_profile's messages are fixed strings (codes), never profile content.
        detail = str(error) if isinstance(error, ValueError) else "invalid profile"
        return None, _check("profile", "fail", detail[:200], "correct the profile")
    if profile.get("account_mode") != ANTHROPIC_ACCOUNT_MODE or found.provider_id != PROVIDER:
        return None, _check(
            "profile",
            "fail",
            "this check is for the Anthropic API-key-reference destination",
            "set account_mode anthropic-api-key-ref and an anthropic model",
        )
    tier = model_tier(PROVIDER, profile["model"]["id"])
    try:
        require_tier_admitted(tier, profile.get("fable_approval_id"))
    except DestinationRefused:
        return None, _check(
            "profile",
            "fail",
            f"tier {tier} needs a per-job approval id",
            "set fable_approval_id or choose a default-tier model; nothing downshifts silently",
        )
    return profile, _check("profile", "ok", f"valid Anthropic profile (tier {tier})")


def _project_id(profile: dict, supplied: str | None) -> str | None:
    declared = (profile.get("model_spend") or {}).get("project_id")
    return declared or supplied


def _credential_reference(profile: dict | None, root: Path | None, root_error: str | None):
    name = "credential_reference"
    if profile is None:
        return _check(name, "unchecked", "the profile is not valid")
    try:
        validate_credential_ref(
            destination(profile["model"]["providerID"], profile["account_mode"]),
            profile.get("credential_ref"),
        )
    except DestinationRefused:
        return _check(
            name,
            "fail",
            "the profile credential_ref is not an anthropic-api-key://OWNER/REFERENCE reference",
            "set credential_ref to the reference printed by tessera-service-credential install",
        )
    if root is None:
        return _check(
            name,
            "fail",
            root_error or "the credentials directory is unusable",
            "pass the private directory systemd provides as CREDENTIALS_DIRECTORY (mode 0700, "
            "owned by the service user)",
        )
    if not os.path.lexists(root / MODEL_CREDENTIAL_FILE):
        return _check(
            name,
            "missing",
            "the model-credential reference file is not present",
            "run tessera-service-credential install, and add "
            "LoadCredential=model-credential:/etc/tessera/credentials/reasoning-model-credential "
            "to the unit",
        )
    try:
        require_model_credential(root, profile)
    except StartupBlocked as error:
        return _check(
            name,
            "fail",
            f"the reference file is not usable ({error})",
            "re-run tessera-service-credential install with the profile's reference labels",
        )
    return _check(name, "ok", "the reference file matches the profile's credential_ref")


def _api_key_file(root: Path | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """STAT ONLY. The key file is never opened, so its value cannot be read or leaked here."""
    name = "api_key_file"
    key = {"present": False, "size_ok": None}
    fix = (
        "install the API key with tessera-service-credential install (secret on stdin), and add "
        "LoadCredential=anthropic-api-key:/etc/tessera/credentials/reasoning-anthropic-api-key"
    )
    if root is None:
        return _check(name, "unchecked", "the credentials directory is unusable"), key
    try:
        info = os.lstat(root / ANTHROPIC_API_KEY_FILE)
    except FileNotFoundError:
        return _check(name, "missing", "no API key file is present", fix), key
    except OSError:
        return _check(name, "fail", "the API key file cannot be inspected"), key
    key["present"] = True
    key["size_ok"] = 0 < info.st_size <= KEY_FILE_MAX_BYTES
    problems = []
    if not stat.S_ISREG(info.st_mode):
        problems.append("not a regular file")
    if info.st_mode & 0o077:
        problems.append("group or other can access it (mode must be 0600)")
    if info.st_uid != os.getuid():
        problems.append("not owned by the service user")
    if not key["size_ok"]:
        problems.append("size is outside 1..512 bytes")
    if problems:
        return _check(name, "fail", "; ".join(problems), "re-install the key file"), key
    return _check(name, "ok", "present, private, owned by the service user, size in range"), key


def _egress_allowlist() -> dict[str, Any]:
    name = "egress_allowlist"
    allowed = model_runtime_allowlist()
    extra = sorted(f"{host}:{port}" for host, port in allowed - EXPECTED_EGRESS)
    missing = sorted(f"{host}:{port}" for host, port in EXPECTED_EGRESS - allowed)
    if extra or missing:
        return _check(
            name,
            "fail",
            "the model-runtime allowlist is not exactly the API host"
            + (f"; extra: {', '.join(extra)}" if extra else "")
            + (f"; missing: {', '.join(missing)}" if missing else ""),
            "the allowlist is code-reviewed in model_destinations; do not widen it",
        )
    probes = (("example.com", 443), ("api.anthropic.com", 80), ("169.254.169.254", 80))
    if any(model_runtime_egress_allowed(PROVIDER, host, port) for host, port in probes):
        return _check(name, "fail", "the egress check allows a host or port it should refuse")
    return _check(name, "ok", "only api.anthropic.com:443 is allowed for model calls")


def _receipts_store(state_dir: Path) -> dict[str, Any]:
    name = "receipts_store"
    fix = "create the state directory as the service user with mode 0700 (StateDirectory=)"
    try:
        info = os.lstat(state_dir)
    except OSError:
        return _check(name, "fail", "the state directory does not exist", fix)
    if not stat.S_ISDIR(info.st_mode):
        return _check(name, "fail", "the state path is not a directory", fix)
    if info.st_uid != os.getuid():
        return _check(name, "fail", "the state directory is not owned by the service user", fix)
    if info.st_mode & 0o022:
        return _check(name, "fail", "the state directory is group or world writable", fix)
    probe = state_dir / f".readiness-probe-{os.getpid()}"
    try:
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, b"probe")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.unlink(probe)
    except OSError:
        try:
            os.unlink(probe)
        except OSError:
            pass
        return _check(name, "fail", "a probe file cannot be written in the state directory", fix)
    free = os.statvfs(state_dir)
    if free.f_bavail * free.f_frsize < MIN_FREE_BYTES:
        return _check(name, "fail", "less than 16 MiB is free on the state filesystem")
    return _check(name, "ok", "the state directory is private and writable, with space")


def _default_opener(method: str, url: str, timeout: float) -> int:
    """One unauthenticated request; returns the HTTP status. No proxy, no redirects, no headers."""

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(urllib.request.Request(url, method=method), timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def _control_slices(control_url: str | None, opener: Opener | None) -> dict[str, Any]:
    name = "control_slices"
    if not control_url:
        return _check(name, "unchecked", "no --control-url was given")
    parts = urlsplit(control_url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username:
        return _check(name, "fail", "the control URL must be http(s) without credentials")
    base = control_url.rstrip("/")
    probe = opener or _default_opener
    try:
        ready = probe("GET", base + "/health/ready", PROBE_TIMEOUT_SECONDS)
        route = probe("GET", base + "/model-spend/limits", PROBE_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - sanitized: unreachable is one finding
        return _check(
            name,
            "fail",
            "the control plane is not reachable",
            "start tessera serve and check the control URL",
        )
    if ready != 200:
        return _check(name, "fail", f"the control plane is not ready (HTTP {ready})")
    if route == 404:
        return _check(
            name,
            "fail",
            "the model-spend routes are missing: the control plane predates the ledger slice",
            "upgrade the control plane before the reasoning service",
        )
    if route not in _ROUTE_EXISTS:
        return _check(name, "fail", f"unexpected answer from the model-spend route (HTTP {route})")
    return _check(name, "ok", "the control plane is ready and serves the model-spend routes")


def _spend_caps(
    profile: dict | None, control_state_dir: Path | None, project_id: str | None
) -> dict[str, Any]:
    name = "spend_caps"
    if control_state_dir is None:
        return _check(name, "unchecked", "no --control-state-dir was given")
    if profile is None:
        return _check(name, "unchecked", "the profile is not valid")
    if not project_id:
        return _check(
            name,
            "unchecked",
            "the profile declares no model_spend.project_id and none was supplied (the project "
            "comes from the grant at run time)",
            "set model_spend.project_id in the profile or pass --project-id",
        )
    database = Path(control_state_dir) / "tessera.sqlite3"
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    except sqlite3.Error:
        return _check(name, "fail", "the control database cannot be opened read-only")
    try:
        try:
            rows = {
                row[0]: row
                for row in connection.execute(
                    "SELECT provider_id, per_call_micro, daily_micro FROM model_spend_limits "
                    "WHERE project_id=? AND provider_id IN (?, '*')",
                    (project_id, PROVIDER),
                )
            }
        except sqlite3.Error:
            return _check(
                name,
                "fail",
                "the control database is older than the model-spend slice",
                "migrate the control database (upgrade the control plane)",
            )
    finally:
        connection.close()
    row = rows.get(PROVIDER) or rows.get("*")
    if row is None:
        return _check(
            name,
            "fail",
            "no model-spend limit is set for this project and provider; the first reservation "
            "would be refused",
            "a person sets limits with PUT /model-spend/limits (see docs/model-spend.md)",
        )
    _, per_call, daily = row
    if per_call <= 0 or daily < per_call:
        return _check(
            name, "fail", "the limit row is not usable (per-call must be > 0 and <= daily)"
        )
    needed = int(profile["limits"]["max_cost_cents_per_call"] * 1_000_000)
    if needed > per_call:
        return _check(
            name,
            "fail",
            "the profile's max_cost_cents_per_call is above the control plane's per-call cap, so "
            "the first reservation would be refused",
            "lower limits.max_cost_cents_per_call in the profile or raise the cap (a person)",
        )
    return _check(name, "ok", "a usable limit exists and covers the profile's per-call bound")


# -- the report ------------------------------------------------------------------------------------


def run_checks(
    *,
    profile_path: Path,
    credentials_directory: Path,
    state_dir: Path,
    environment: Mapping[str, str] | None = None,
    control_state_dir: Path | None = None,
    control_url: str | None = None,
    opener: Opener | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """All checks, in a stable order. Never reads the API key file's contents."""
    environment = os.environ if environment is None else environment
    profile, profile_check = _load_profile(Path(profile_path))
    root: Path | None = None
    root_error: str | None = None
    try:
        root = credential_root({"CREDENTIALS_DIRECTORY": str(credentials_directory)})
    except (StartupBlocked, OSError):
        root_error = "the credentials directory is missing, not private or not owned by the user"
    key_check, key = _api_key_file(root)
    project = _project_id(profile, project_id) if profile else None
    checks = [
        _environment(environment),
        profile_check,
        _credential_reference(profile, root, root_error),
        key_check,
        _egress_allowlist(),
        _receipts_store(Path(state_dir)),
        _control_slices(control_url, opener),
        _spend_caps(profile, control_state_dir, project),
    ]
    blocking = [c for c in checks if c["status"] not in {"ok", "unchecked"}]
    unchecked = [c["name"] for c in checks if c["status"] == "unchecked"]
    key_only = (
        len(blocking) == 1
        and blocking[0]["name"] == "api_key_file"
        and blocking[0]["status"] == "missing"
    )
    if blocking and not key_only:
        overall = "not_ready"  # a check failed: that is reported before anything unverified
    elif unchecked:
        overall = "not_fully_checked"  # an unverified check never lets the report say ready
    else:
        overall = "ready_except_key" if key_only else "ready"
    summary = SUMMARIES[overall].format(names=", ".join(unchecked))
    return {
        "overall": overall,
        "summary": summary,
        "checks": checks,
        "unchecked": unchecked,
        "key": key,
    }


_EXIT = {
    "ready": EXIT_READY,
    "ready_except_key": EXIT_READY_EXCEPT_KEY,
    "not_ready": EXIT_NOT_READY,
    "not_fully_checked": EXIT_NOT_FULLY_CHECKED,
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="tessera-reasoning-readiness",
        description="Check everything the first receipted model call needs except the key value.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="run the readiness checks and print JSON")
    check.add_argument("--profile", type=Path, required=True)
    check.add_argument("--credentials-directory", type=Path, required=True)
    check.add_argument("--state-dir", type=Path, required=True)
    check.add_argument("--control-state-dir", type=Path)
    check.add_argument("--control-url")
    check.add_argument("--project-id")
    args = parser.parse_args(argv)
    report = run_checks(
        profile_path=args.profile,
        credentials_directory=args.credentials_directory,
        state_dir=args.state_dir,
        environment=dict(os.environ),
        control_state_dir=args.control_state_dir,
        control_url=args.control_url,
        project_id=args.project_id,
    )
    print(json.dumps(report, indent=2))
    print(report["summary"], file=sys.stderr)
    return _EXIT[report["overall"]]


if __name__ == "__main__":
    raise SystemExit(main())
