"""Owner re-enrollment guidance built from credential health (never a secret).

The tool this module backs reads the existing credential-health route and turns each item that
needs an owner into the exact command the owner runs in a terminal. Nothing here accepts, stores,
reads or sends a secret: the input is at most a provider id, and the secret is entered only by the
``tessera credentials reenroll`` command, from standard input.
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from tessera_mcp.client import ControlClient

PROVIDER_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
REFERENCE_PATTERN = re.compile(r"[A-Za-z0-9:._/@-]{1,200}")
ATTENTION_STATES = ("expired", "revoked", "version_mismatch", "warning")
REENROLL_EXPIRY_HOURS = 72
PROFILES_PLACEHOLDER = "<PROFILES_FILE>"
DIRECTORY_PLACEHOLDER = "<CREDENTIAL_DIRECTORY>"
SECRET_FILE_PLACEHOLDER = "/path/to/secret-file"
NOTE = (
    "This tool never accepts, stores or transmits a secret and changes nothing. The secret is "
    "entered only by the owner, on standard input of the command above."
)
HEALTHY = "all credentials healthy"
TOOL_NAME = "tessera_credential_reenroll_command"


def _text(value: Any, limit: int = 300) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _steps(state: str, command: str | None) -> list[str]:
    if state == "version_mismatch":
        return [
            "Decide which version is right: the store holds a different version than the "
            "profile pins. If the stored version is the one to use, pin it with the command "
            "below (run as the owner, with your usual providers file option).",
            command or "Update the provider profile's credential_version to the stored version.",
            "Restart the control, guardian and executor services so they resolve it.",
            "Re-read tessera_credential_health and confirm the finding is cleared.",
        ]
    if state == "revoked":
        return [
            "This reference was revoked on purpose: re-enrolling it is refused (a revocation is "
            "never undone by a re-enrollment).",
            "Enroll a NEW reference for the provider with its operator tooling (a person does "
            "this), then point the profile at the new reference.",
            "Restart the control, guardian and executor services so they resolve it.",
            "Re-read tessera_credential_health and confirm the finding is cleared.",
        ]
    return [
        "Get a new key from the provider console (a person does this; Tessera cannot).",
        "Put it in an owner-only file (mode 0600) or pipe it; never pass it as an argument "
        "or environment variable.",
        f"Run the command below as the user that owns the credential directory: {command}",
        "Restart the control, guardian and executor services as the command prints; if the "
        "profile pins a credential_version, pin the new version as it prints.",
        "Delete the previous key at the provider once the services use the new one: replacing "
        "it here does not revoke it there.",
        "Re-read tessera_credential_health and confirm the finding is cleared.",
    ]


def _entry(item: dict[str, Any]) -> dict[str, Any] | None:
    """One guidance entry, or None when a value from the record is not safe to put in a command."""
    provider = item.get("provider_id")
    reference = item.get("reference")
    if not isinstance(provider, str) or not PROVIDER_PATTERN.fullmatch(provider):
        return None
    if not isinstance(reference, str) or not REFERENCE_PATTERN.fullmatch(reference):
        return None
    state = item["state"]
    stored = _int(item.get("version"))
    profile = item.get("profile")
    command: str | None
    if state == "revoked":
        command = None  # the CLI refuses a revoked reference; guidance is in the steps
    elif state == "version_mismatch":
        safe_profile = profile if isinstance(profile, str) and profile else None
        if provider == "salad" and stored is not None and safe_profile is not None:
            entry_json = json.dumps({"credential_version": stored})
            command = (
                "tessera providers update --section salad-profile "
                f"--key {shlex.quote(safe_profile)} --entry-json {shlex.quote(entry_json)}"
            )
        else:
            command = None
    else:
        version = f" --version {stored + 1}" if stored is not None else ""
        # More than the 24 hour warning window, so the finding clears to ok instead of warning.
        longer = f" --expires-in-hours {REENROLL_EXPIRY_HOURS}"
        # The Salad read-only check needs the organization, which the profile file carries.
        profiles = f" --profiles-file {PROFILES_PLACEHOLDER}" if provider == "salad" else ""
        command = (
            f"tessera credentials reenroll {shlex.quote(provider)} "
            f"--credential-directory {DIRECTORY_PLACEHOLDER} "
            f"--reference {shlex.quote(reference)}{version}{longer}{profiles} "
            f"< {SECRET_FILE_PLACEHOLDER}"
        )
    return {
        "provider_id": provider,
        "profile": _text(profile, 100),
        "reference": reference,
        "state": state,
        "reason": _text(item.get("reason")),
        "pinned_version": _int(item.get("pinned_version")),
        "command": command,
        "steps": _steps(state, command),
        "note": NOTE,
    }


def build_guidance(health: dict[str, Any], provider: str | None) -> dict[str, Any]:
    if provider is not None and not PROVIDER_PATTERN.fullmatch(provider):
        raise ValueError("provider must match [a-z0-9][a-z0-9-]{0,40}")
    entries: list[dict[str, Any]] = []
    refused = 0
    for item in health.get("items") or []:
        if not isinstance(item, dict) or item.get("state") not in ATTENTION_STATES:
            continue
        if provider is not None and item.get("provider_id") != provider:
            continue
        entry = _entry(item)
        if entry is None:
            refused += 1
        else:
            entries.append(entry)
    if entries:
        statement = f"{len(entries)} credential(s) need the owner's attention."
    elif refused:
        statement = "no usable guidance: a credential record had an unsafe value."
    else:
        statement = HEALTHY
    result: dict[str, Any] = {"items": entries, "statement": statement, "note": NOTE}
    if refused:
        result["refused_unsafe_items"] = refused
    return result


def register_credential_reenroll_tool(server: FastMCP, client: ControlClient) -> None:
    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    )
    async def tessera_credential_reenroll_command(provider: str | None = None) -> dict[str, Any]:
        """Show the owner's exact re-enrollment command for each credential needing attention.

        Reads the credential-health route and, for every credential that is expired, revoked,
        version-mismatched or within 24 h of expiry (optionally only `provider`, a lowercase
        provider id), returns the shell line the owner runs, the ordered steps and a note. The
        directory and the secret file stay placeholders. This tool never accepts, stores or
        transmits a secret, has no parameter that could carry one and changes nothing; the
        secret is entered only through the `tessera credentials reenroll` command.
        """
        if provider is not None and not PROVIDER_PATTERN.fullmatch(provider):
            raise ValueError("provider must match [a-z0-9][a-z0-9-]{0,40}")
        return build_guidance(await client.credential_health(), provider)

    tool = server._tool_manager._tools[TOOL_NAME]
    # Refuse any argument but `provider` at schema validation, before the function runs.
    tool.fn_metadata.arg_model.model_config["extra"] = "forbid"
    tool.fn_metadata.arg_model.model_rebuild(force=True)
    tool.parameters["additionalProperties"] = False
