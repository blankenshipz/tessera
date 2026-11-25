"""``tessera reasoning admit`` and ``tessera reasoning revoke-admission`` (ADR 0163).

``admit`` is a person's act: it sends their own ``human_session`` credential (a header file, never
an argument or environment value) to ``POST /projects/{id}/reasoning-admissions``. The control
plane refuses every other identity (an agent, a worker, the owner token with or without an
automation label) and records the admission on receipts: who, which project, provider and model,
the caps and the expiry. Only then is ``reasoning.admitted`` written, atomically, root-owned 0600,
holding a non-secret reference to that record. The unit still checks only that the file exists.

This command sends no automation label: a script holding the owner token cannot admit. It never
reads, prints or stores a credential value.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

DEFAULT_CONTROL_URL = "http://127.0.0.1:8765"
MAX_EXPIRES_IN = 30 * 86_400
CAP_FLAGS = (
    ("max_cost_cents_per_call", "--max-cost-cents-per-call"),
    ("daily_cost_cap_cents", "--daily-cost-cap-cents"),
    ("daily_token_cap", "--daily-token-cap"),
)


def add_admission_parsers(actions: argparse._SubParsersAction) -> None:
    admit = actions.add_parser(
        "admit",
        help="a person admits the service: record it on receipts, then write the marker",
        description=(
            "Record a person's admission of the reasoning service (ADR 0163) with their own "
            "human_session credential, then write CONFIG_DIR/reasoning.admitted (root, 0600) "
            "holding a reference to the record. Caps default to the profile's limits."
        ),
    )
    _common(admit)
    admit.add_argument("--profile", type=Path, help="default CONFIG_DIR/reasoning.json")
    admit.add_argument("--project-id", help="default: the profile's model_spend.project_id")
    admit.add_argument("--provider", help="default: the profile's model.providerID")
    admit.add_argument("--model", help="bare model id; default: the profile's model.id")
    for _name, flag in CAP_FLAGS:
        admit.add_argument(flag, type=int, help="default: the profile's limit")
    admit.add_argument("--max-calls", type=int, help="optional total number of calls admitted")
    expiry = admit.add_mutually_exclusive_group(required=True)
    expiry.add_argument("--expires-in", type=int, metavar="SECONDS", help="at most 30 days")
    expiry.add_argument(
        "--expires-at",
        metavar="ISO8601",
        help="exact expiry with an offset; use it with --idempotency-key to retry",
    )
    admit.add_argument("--idempotency-key", help="reuse only to retry the identical request")
    admit.add_argument(
        "--replace", action="store_true", help="replace an existing marker (for example a bare one)"
    )

    revoke = actions.add_parser(
        "revoke-admission",
        help="revoke a recorded admission and remove the marker that references it",
    )
    _common(revoke)
    revoke.add_argument("--project-id", required=True)
    revoke.add_argument("--admission-id", required=True)
    revoke.add_argument("--expected-revision", type=int, default=1)
    revoke.add_argument("--idempotency-key")
    revoke.add_argument(
        "--keep-marker", action="store_true", help="revoke the record but leave the file"
    )


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", type=Path, help="prefix every path (for staging and tests)")
    parser.add_argument("--config-dir", type=Path, default=Path("/etc/tessera"))
    parser.add_argument(
        "--session-header",
        type=Path,
        required=True,
        help="a private file 'Authorization: Bearer ...' holding a person's session credential",
    )
    parser.add_argument("--control-url", default=DEFAULT_CONTROL_URL)


def _options(arguments: argparse.Namespace):
    from .reasoning_install import Options

    config = arguments.config_dir
    profile = getattr(arguments, "profile", None) or config / "reasoning.json"
    return Options(
        root=arguments.root,
        config_dir=config,
        credentials_dir=config / "credentials",
        profile_path=profile,
        prompt_path=config / "reasoning-prompt.txt",
    )


def admission_body(
    arguments: argparse.Namespace, profile: dict, now: datetime | None = None
) -> tuple[str, dict]:
    """(project id, request body), validated by the model the control API uses."""
    from tessera_control.reasoning_admissions import CreateReasoningAdmission

    now = now or datetime.now(UTC)
    model = profile.get("model") if isinstance(profile.get("model"), dict) else {}
    limits = profile.get("limits") if isinstance(profile.get("limits"), dict) else {}
    project = arguments.project_id or (profile.get("model_spend") or {}).get("project_id")
    if not isinstance(project, str) or not project:
        raise ValueError("project")
    if arguments.expires_at is not None:
        expires = datetime.fromisoformat(arguments.expires_at.replace("Z", "+00:00"))
        if expires.tzinfo is None:
            raise ValueError("expires-at")
    else:
        if not 1 <= arguments.expires_in <= MAX_EXPIRES_IN:
            raise ValueError("expires-in")
        expires = now + timedelta(seconds=arguments.expires_in)
    caps: dict = {}
    for name, _flag in CAP_FLAGS:
        value = getattr(arguments, name)
        caps[name] = value if value is not None else limits.get(name)
    if arguments.max_calls is not None:
        caps["max_calls"] = arguments.max_calls
    body = {
        "provider_id": arguments.provider or model.get("providerID"),
        "model": arguments.model or model.get("id"),
        "caps": caps,
        "expires_at": expires.isoformat(),
    }
    return project, CreateReasoningAdmission.model_validate(body).model_dump(
        mode="json", exclude_none=True
    )


def _error(message: str, code: int = 2) -> int:
    print(json.dumps({"error": message}, sort_keys=True), file=sys.stderr)
    return code


def _transport(arguments, transport_factory):
    from tessera_control.agent_research_review import control_origin, read_owner_header
    from tessera_control.authority_cli import _transport_class

    origin = control_origin(arguments.control_url)
    # No automation identity: an admission is a person's act, and the label would only mark it
    # as automation (which the control plane refuses for this decision anyway).
    return (transport_factory or _transport_class())(
        origin, read_owner_header(arguments.session_header)
    )


def run_admit(arguments: argparse.Namespace, *, transport_factory=None, accounts=None) -> int:
    from tessera_control.agent_research_review import ReviewError
    from tessera_control.authority_cli import _refusal

    from . import reasoning_install as engine
    from .reasoning_admission import marker_bytes, read_marker

    try:
        options = _options(arguments)
        engine._check_options(options)
        if read_marker(engine.admitted_path(options))[0] != "absent" and not arguments.replace:
            return _error(
                "the admission marker already exists; nothing was recorded or changed "
                "(pass --replace to replace it, for example a bare marker)"
            )
        if options.root is None and engine.os.geteuid() != 0:
            return _error("admit writes a root-owned marker and needs root; nothing was recorded")
        profile = engine._read_json_object(
            engine._real(options, options.profile_path), "the profile"
        )
    except engine.InstallRefused as error:
        return _error(f"reasoning admit: {error}")
    try:
        project, body = admission_body(arguments, profile)
    except ValueError:
        return _error(
            "Invalid admission: a project (flag or the profile's model_spend.project_id), a "
            "provider and bare model, integer caps (per call <= daily) and an expiry within 30 "
            "days with an offset are required"
        )
    key = arguments.idempotency_key or "reasoning-admit-" + uuid.uuid4().hex
    if not arguments.idempotency_key:
        print(f"idempotency-key: {key}", file=sys.stderr)
    print(f"expires-at: {body['expires_at']}", file=sys.stderr)
    try:
        transport = _transport(arguments, transport_factory)
        status, response = transport(
            "POST", f"/projects/{quote(project, safe='')}/reasoning-admissions", body, key
        )
    except ReviewError as error:
        return _error(str(error), 1)
    if not (200 <= status < 300 and isinstance(response, dict)):
        print(json.dumps(_refusal(status, response), sort_keys=True))
        return 1
    try:
        path = engine.write_admitted_marker(
            options, marker_bytes(response), replace=arguments.replace, accounts=accounts
        )
    except (engine.InstallRefused, OSError) as error:
        reason = str(error) if isinstance(error, engine.InstallRefused) else type(error).__name__
        print(
            json.dumps(
                {
                    "error": f"the admission was recorded but the marker was not written: {reason}",
                    "admission_id": response.get("id"),
                    "retry": "rerun with the same --idempotency-key and --expires-at",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    summary = {
        key_name: response.get(key_name)
        for key_name in (
            "id",
            "project_id",
            "provider_id",
            "model",
            "caps",
            "expires_at",
            "admitted_by_principal_id",
            "decision_class",
            "record_digest",
            "state",
        )
    }
    print(json.dumps({"admission": summary, "marker": str(path)}, indent=2, sort_keys=True))
    return 0


def run_revoke(arguments: argparse.Namespace, *, transport_factory=None) -> int:
    from tessera_control.agent_research_review import ReviewError
    from tessera_control.authority_cli import _refusal

    from . import reasoning_install as engine
    from .reasoning_admission import MarkerMalformed, parse_marker, read_marker

    key = arguments.idempotency_key or "reasoning-revoke-admission-" + uuid.uuid4().hex
    body = {"expected_revision": arguments.expected_revision}
    try:
        transport = _transport(arguments, transport_factory)
        status, response = transport(
            "POST",
            f"/projects/{quote(arguments.project_id, safe='')}/reasoning-admissions/"
            f"{quote(arguments.admission_id, safe='')}/revoke",
            body,
            key,
        )
    except ReviewError as error:
        return _error(str(error), 1)
    if not (200 <= status < 300 and isinstance(response, dict)):
        print(json.dumps(_refusal(status, response), sort_keys=True))
        return 1
    removed = False
    if not arguments.keep_marker:
        options = _options(arguments)
        state, content = read_marker(engine.admitted_path(options))
        try:
            marker = parse_marker(content) if state == "read" and content is not None else None
        except MarkerMalformed:
            marker = None
        if marker is not None and marker["admission_id"] == arguments.admission_id:
            try:
                removed = engine.remove_admitted_marker(options)
            except (engine.InstallRefused, OSError) as error:
                reason = (
                    str(error) if isinstance(error, engine.InstallRefused) else type(error).__name__
                )
                return _error(f"revoked, but the marker was not removed: {reason}", 1)
    print(
        json.dumps(
            {
                "admission_id": response.get("id"),
                "state": response.get("state"),
                "marker_removed": removed,
            },
            sort_keys=True,
        )
    )
    return 0
