"""Operator readiness check for the first receipted call of the on-platform agent loop.

``tessera-reasoning-readiness check`` verifies everything the first call needs EXCEPT the key
value: the profile, the credential reference file, the model-runtime egress allowlist, the receipts
store, the control-plane slices, the model-spend caps and the agent authority grant the profile
names (an execution grant or a reasoning-only one, ADR 0155; read-only from the control database).
The API key file is looked at by ``stat`` only (present, owner, mode, size): it is never opened,
read, hashed or printed, so no function here can leak it. A deployment that is complete apart from
the key file reports ``ready_except_key``; anything else that is missing or wrong reports
``not_ready``. A check that could not run (``unchecked``, for example the spend caps without
``--control-state-dir``) is NOT a pass: it keeps the report from saying ``ready`` or
``ready_except_key`` and the result is ``not_fully_checked`` (its own non-zero exit status),
because an unverified spend limit is exactly what makes the first call fail after the key is
installed.

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
from datetime import UTC, datetime
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
from .remote_reasoning_store import manager_directory_ok, manager_file_ok, private_file
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
        "--control-state-dir, or check the limits through GET /model-spend and the grant through "
        "GET /agent-authority-grants/{{id}}); no check failed, "
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


def _load_profile(
    path: Path, credentials_dir: Path | None = None
) -> tuple[dict | None, dict[str, Any]]:
    try:
        profile = json.loads(private_file(path, credentials_dir=credentials_dir))
    except (OSError, ValueError):
        return None, _check(
            "profile",
            "fail",
            "the profile is missing, unreadable or not a private regular file (owned 0600, or a "
            "root-owned 0400/0440 service-manager credential)",
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


def _credential_reference(
    profile: dict | None,
    root: Path | None,
    root_error: str | None,
    filename: str = MODEL_CREDENTIAL_FILE,
):
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
    if not os.path.lexists(root / filename):
        return _check(
            name,
            "missing",
            "the model-credential reference file is not present",
            "run tessera-service-credential install, and add "
            "LoadCredential=model-credential:/etc/tessera/credentials/reasoning-model-credential "
            "to the unit",
        )
    try:
        require_model_credential(root, profile, filename)
    except StartupBlocked as error:
        return _check(
            name,
            "fail",
            f"the reference file is not usable ({error})",
            "re-run tessera-service-credential install with the profile's reference labels",
        )
    return _check(name, "ok", "the reference file matches the profile's credential_ref")


def _api_key_file(
    root: Path | None, owner_uid: int | None = None, filename: str = ANTHROPIC_API_KEY_FILE
) -> tuple[dict[str, Any], dict[str, Any]]:
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
        info = os.lstat(root / filename)
    except FileNotFoundError:
        return _check(name, "missing", "no API key file is present", fix), key
    except OSError:
        return _check(name, "fail", "the API key file cannot be inspected"), key
    key["present"] = True
    key["size_ok"] = 0 < info.st_size <= KEY_FILE_MAX_BYTES
    problems = []
    manager = manager_directory_ok(os.lstat(root)) and manager_file_ok(info)
    if not stat.S_ISREG(info.st_mode):
        problems.append("not a regular file")
    elif manager and info.st_uid == 0:
        pass  # a service manager's own credential: root-owned 0400/0440 in its private directory
    else:
        if info.st_mode & 0o077:
            problems.append("group or other can access it (mode must be 0600)")
        if info.st_uid != (os.getuid() if owner_uid is None else owner_uid):
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


def _receipts_store(state_dir: Path, owner_uid: int | None = None) -> dict[str, Any]:
    name = "receipts_store"
    fix = "create the state directory as the service user with mode 0700 (StateDirectory=)"
    try:
        info = os.lstat(state_dir)
    except OSError:
        return _check(name, "fail", "the state directory does not exist", fix)
    if not stat.S_ISDIR(info.st_mode):
        return _check(name, "fail", "the state path is not a directory", fix)
    if info.st_uid != (os.getuid() if owner_uid is None else owner_uid):
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


UNREADABLE_CONTROL_DATABASE = (
    "the control database is not readable by this user (run as a user that can read it, or "
    "check spend caps separately)"
)
# ``connect(mode=ro)`` is lazy, so a file this user cannot open or read surfaces at the first
# query, with one of these messages. Only a missing table or column means the database predates
# the model-spend slice.
_UNREADABLE_MARKERS = ("unable to open", "readonly", "read-only", "permission", "denied")
_SCHEMA_MARKERS = ("no such table", "no such column")


def _spend_query_failure(name: str, exc: sqlite3.Error) -> dict[str, Any]:
    message = str(exc).lower()
    if any(marker in message for marker in _SCHEMA_MARKERS):
        return _check(
            name,
            "fail",
            "the control database is older than the model-spend slice",
            "migrate the control database (upgrade the control plane)",
        )
    if isinstance(exc, sqlite3.OperationalError) and any(
        marker in message for marker in _UNREADABLE_MARKERS
    ):
        return _check(name, "fail", UNREADABLE_CONTROL_DATABASE)
    return _check(name, "fail", "the control database cannot be queried")


def _open_control_database(name: str, control_state_dir: Path):
    """(read-only connection, None) or (None, the failing check). Never creates the file."""
    database = Path(control_state_dir) / "tessera.sqlite3"
    try:
        return sqlite3.connect(f"file:{database}?mode=ro", uri=True), None
    except sqlite3.Error:
        try:
            os.stat(database)
        except (FileNotFoundError, NotADirectoryError):
            return None, _check(name, "fail", "the control database cannot be opened read-only")
        except OSError:
            pass  # this user cannot even look into the control state directory
        # present, but not for this user
        return None, _check(name, "fail", UNREADABLE_CONTROL_DATABASE)


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
    connection, failure = _open_control_database(name, control_state_dir)
    if connection is None:
        return failure
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
        except sqlite3.Error as exc:
            return _spend_query_failure(name, exc)
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


ISSUE_REASONING_GRANT = (
    "a person issues a reasoning grant with `tessera authority issue-reasoning` (or POST "
    "/projects/{id}/agent-authority-grants with bounds.kind reasoning) and sets the profile's "
    "grant_id, revision and epoch to the issued grant's"
)
_GRANT_FIXES = {
    "grant_profile_incomplete": "set grant_id, revision and epoch in the profile",
    "grant_missing": ISSUE_REASONING_GRANT,
    "grant_malformed": ISSUE_REASONING_GRANT,
    "grant_kind_unsupported": ISSUE_REASONING_GRANT,
    "grant_revoked": ISSUE_REASONING_GRANT,
    "grant_expired": ISSUE_REASONING_GRANT,
    "grant_lineage_inactive": "the grant's ancestry, subject credential or issuer is no longer "
    "live; " + ISSUE_REASONING_GRANT,
    "grant_revision_mismatch": "set the profile's revision and epoch to the grant's current "
    "ones, or issue a new grant",
    "grant_scope_mismatch": "issue a grant for this profile's project, agent credential, "
    "destination, provider and model (a reasoning grant carries exactly ['model:call'] and no "
    "bindings), or correct the profile",
    "grant_binding_inactive": "the execution grant this agent grant is bound to is no longer "
    "active at its pinned revision and epoch; " + ISSUE_REASONING_GRANT,
    "grant_budget_exhausted": ISSUE_REASONING_GRANT + " (with a budget that covers the run)",
    "grant_budget_below_call_cap": "issue a grant whose budget covers at least one call at the "
    "profile's limits.max_cost_cents_per_call, or lower that bound",
}


def _grant_finding(code: str, detail: str) -> dict[str, Any]:
    """A named finding: the detail starts with its stable code."""
    return _check("grant", "fail", f"{code}: {detail}", _GRANT_FIXES[code])


def _is_revision(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _grant(
    profile: dict | None, control_state_dir: Path | None, project_id: str | None
) -> dict[str, Any]:
    """The agent authority grant the profile names, read-only from the control database: it
    exists, is active with a live lineage, unexpired, at the profile's revision and epoch, in the
    spend project, scoped to this profile and has budget for one call at the per-call cap. A
    reasoning grant (ADR 0155) must carry exactly ``model:call``, no binding, and the profile's
    provider and model; an execution grant must still have every bound execution grant active.
    Anything unreadable or malformed fails: this check never reports a grant it could not read."""
    name = "grant"
    if control_state_dir is None:
        return _check(name, "unchecked", "no --control-state-dir was given")
    if profile is None:
        return _check(name, "unchecked", "the profile is not valid")
    grant_id = profile.get("grant_id")
    if not isinstance(grant_id, str) or not (
        _is_revision(profile.get("revision")) and _is_revision(profile.get("epoch"))
    ):
        return _grant_finding(
            "grant_profile_incomplete",
            "the profile does not name the grant's id, revision and epoch",
        )
    connection, failure = _open_control_database(name, control_state_dir)
    if connection is None:
        return failure
    try:
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT * FROM agent_authority_grants WHERE id=?", (grant_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            return _grant_query_failure(name, exc)
        if row is None:
            return _grant_finding("grant_missing", "no agent authority grant has the profile's id")
        try:
            return _grant_row(connection, row, profile, project_id)
        except sqlite3.Error as exc:
            return _grant_query_failure(name, exc)
        except (KeyError, TypeError, ValueError, AttributeError):  # fail closed, never crash
            return _grant_finding("grant_malformed", "the grant and the profile cannot be compared")
    finally:
        connection.close()


def _grant_query_failure(name: str, exc: sqlite3.Error) -> dict[str, Any]:
    message = str(exc).lower()
    if any(marker in message for marker in _SCHEMA_MARKERS):
        return _check(
            name,
            "fail",
            "the control database is older than the agent authority and model-spend slices",
            "migrate the control database (upgrade the control plane)",
        )
    return _spend_query_failure(name, exc)


def _grant_row(connection, row, profile: dict, project_id: str | None) -> dict[str, Any]:
    from tessera_control.agent_authority import require_active_agent_lineage
    from tessera_control.agent_authority_models import authority_kind
    from tessera_control.grant_model_spend import MICRO, grant_remaining_micro
    from tessera_control.store import StoreError

    now = datetime.now(UTC)
    try:
        bounds = json.loads(row["bounds_json"])
        if not isinstance(bounds, dict):
            raise TypeError
        kind = authority_kind(bounds)
        expiry = datetime.fromisoformat(bounds["expires_at"].replace("Z", "+00:00"))
        budget = bounds["budget_cents"]
        if expiry.tzinfo is None or not isinstance(budget, int) or isinstance(budget, bool):
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        return _grant_finding("grant_malformed", "the grant's stored bounds cannot be read")
    if kind not in {"execution", "reasoning"}:
        return _grant_finding("grant_kind_unsupported", "the grant kind is not known here")
    if row["status"] != "active":
        return _grant_finding("grant_revoked", "the grant is not active")
    if now >= expiry:
        return _grant_finding("grant_expired", "the grant has expired")
    try:
        require_active_agent_lineage(connection, row["id"])
    except (StoreError, ValueError, KeyError, TypeError, AttributeError):
        return _grant_finding(
            "grant_lineage_inactive", "the grant's lineage is revoked, expired or not live"
        )
    if row["revision"] != profile["revision"] or row["epoch"] != profile["epoch"]:
        return _grant_finding(
            "grant_revision_mismatch",
            "the grant's revision or epoch is not the profile's (the runtime fence refuses it)",
        )
    model = profile.get("model") or {}
    fields = []
    if project_id and row["project_id"] != project_id:
        fields.append("project")
    if (
        row["subject_credential_id"] != profile.get("subject_credential_id")
        or bounds.get("subject_credential_id") != row["subject_credential_id"]
    ):
        fields.append("subject_credential_id")
    if bounds.get("destination") != profile.get("destination"):
        fields.append("destination")
    if bounds.get("model") != model.get("id"):
        fields.append("model")
    duration = bounds.get("max_duration_seconds")
    if not _is_revision(duration) or profile.get("max_duration_seconds", 0) > duration:
        fields.append("max_duration_seconds")
    if kind == "reasoning":
        if bounds.get("provider_id") != model.get("providerID"):
            fields.append("provider_id")
        if bounds.get("actions") != ["model:call"]:
            fields.append("actions")
        if bounds.get("bindings") != []:
            fields.append("bindings")
    if fields:
        return _grant_finding(
            "grant_scope_mismatch",
            "the grant does not match the profile on: " + ", ".join(fields),
        )
    if kind == "execution" and not _execution_bindings_active(connection, bounds, now):
        return _grant_finding(
            "grant_binding_inactive",
            "an execution grant this agent grant is bound to is not active, current and unexpired",
        )
    remaining = grant_remaining_micro(connection, row)
    if remaining <= 0:
        return _grant_finding("grant_budget_exhausted", "the grant has no budget left")
    cap = (profile.get("limits") or {}).get("max_cost_cents_per_call")
    if not isinstance(cap, int | float) or isinstance(cap, bool) or cap <= 0:
        return _grant_finding(
            "grant_profile_incomplete", "the profile has no usable limits.max_cost_cents_per_call"
        )
    if remaining < int(cap * MICRO):
        return _grant_finding(
            "grant_budget_below_call_cap",
            "the grant's remaining budget is below the profile's max_cost_cents_per_call, so the "
            "first reservation would be refused",
        )
    return _check(
        "grant",
        "ok",
        f"an active, unexpired {kind} grant matches the profile and covers one call at the "
        "per-call cap",
    )


def _execution_bindings_active(connection, bounds: dict, now: datetime) -> bool:
    """An execution-kind agent grant is usable only while every execution grant it is bound to
    is active at the pinned revision and epoch and unexpired (the issuance rule)."""
    bindings = bounds.get("bindings")
    if not isinstance(bindings, list) or not bindings:
        return False
    for binding in bindings:
        try:
            found = connection.execute(
                "SELECT status, revision, epoch, expires_at FROM execution_grants WHERE id=?",
                (binding["execution_grant_id"],),
            ).fetchone()
            if (
                found is None
                or found["status"] != "active"
                or found["revision"] != binding["expected_execution_grant_revision"]
                or found["epoch"] != binding["expected_execution_epoch"]
                or datetime.fromisoformat(found["expires_at"].replace("Z", "+00:00")) <= now
            ):
                return False
        except (KeyError, TypeError, ValueError, AttributeError):
            return False
    return True


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
    state_owner_uid: int | None = None,
    key_owner_uid: int | None = None,
    credential_files: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """All checks, in a stable order. Never reads the API key file's contents.

    ``state_owner_uid`` and ``key_owner_uid`` name the owners to expect when the check is run by
    someone other than the service user (the installer, as root: the state directory belongs to
    the service account and the key file, per ADR 0093, to root). Default: the running user.

    ``credential_files`` maps the names the service sees (``model-credential``,
    ``anthropic-api-key``) to the file names they have on disk when the check is pointed at the
    stored directory instead of the unit's ``$CREDENTIALS_DIRECTORY`` (the unit's
    ``LoadCredential=NAME:PATH`` lines give the mapping)."""
    files = credential_files or {}
    environment = os.environ if environment is None else environment
    profile, profile_check = _load_profile(Path(profile_path), Path(credentials_directory))
    root: Path | None = None
    root_error: str | None = None
    try:
        root = credential_root({"CREDENTIALS_DIRECTORY": str(credentials_directory)})
    except (StartupBlocked, OSError):
        root_error = "the credentials directory is missing, not private or not owned by the user"
    key_check, key = _api_key_file(
        root, key_owner_uid, files.get(ANTHROPIC_API_KEY_FILE, ANTHROPIC_API_KEY_FILE)
    )
    project = _project_id(profile, project_id) if profile else None
    checks = [
        _environment(environment),
        profile_check,
        _credential_reference(
            profile, root, root_error, files.get(MODEL_CREDENTIAL_FILE, MODEL_CREDENTIAL_FILE)
        ),
        key_check,
        _egress_allowlist(),
        _receipts_store(Path(state_dir), state_owner_uid),
        _control_slices(control_url, opener),
        _spend_caps(profile, control_state_dir, project),
        _grant(profile, control_state_dir, project),
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


_DEFAULT_UNIT_PATH = "/etc/systemd/system/tessera-reasoning.service"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="tessera-reasoning-readiness",
        description="Check everything the first receipted model call needs except the key value.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="run the readiness checks and print JSON")
    check.add_argument("--profile", type=Path)
    check.add_argument("--credentials-directory", type=Path)
    check.add_argument("--state-dir", type=Path)
    check.add_argument(
        "--in-unit",
        action="store_true",
        help="run as the service user in a transient unit with the installed unit's credentials",
    )
    check.add_argument("--unit-path", type=Path, default=Path(_DEFAULT_UNIT_PATH))
    check.add_argument("--control-state-dir", type=Path)
    check.add_argument("--control-url")
    check.add_argument("--project-id")
    args = parser.parse_args(argv)
    if args.in_unit:
        from tessera_agent import readiness_in_unit

        try:
            code, output = readiness_in_unit.run_in_unit(
                args.unit_path,
                state_dir=args.state_dir,
                control_state_dir=args.control_state_dir,
                control_url=args.control_url,
                project_id=args.project_id,
            )
        except ValueError as exc:
            print(f"in-unit check refused: {exc}", file=sys.stderr)
            return 2
        print(output)
        return code
    env_dir = os.environ.get("CREDENTIALS_DIRECTORY", "")
    env_dir_path = Path(env_dir) if env_dir and Path(env_dir).is_absolute() else None
    credentials_directory = args.credentials_directory or env_dir_path
    profile = args.profile or (env_dir_path / "profile" if env_dir_path is not None else None)
    if credentials_directory is None or profile is None or args.state_dir is None:
        parser.error(
            "give --profile, --credentials-directory and --state-dir, "
            "or run with an absolute CREDENTIALS_DIRECTORY and --state-dir"
        )
    report = run_checks(
        profile_path=profile,
        credentials_directory=credentials_directory,
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
