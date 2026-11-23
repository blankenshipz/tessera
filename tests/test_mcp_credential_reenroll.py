"""The re-enrollment guidance tool: exact owner commands, no secret path, same gate as health."""

from __future__ import annotations

import asyncio
import json
import shlex

import httpx
import pytest
from mcp_required import require_mcp

require_mcp()

from tessera_mcp.client import ControlClient  # noqa: E402
from tessera_mcp.config import Config  # noqa: E402
from tessera_mcp.credential_reenroll import TOOL_NAME  # noqa: E402
from tessera_mcp.server import create_server  # noqa: E402

REF = "credential-ref:salad-management"
# The CLI contract's option names for `tessera credentials reenroll` (a drift fails the test).
CONTRACT_OPTIONS = {
    "--credential-directory",
    "--reference",
    "--version",
    "--expires-in-hours",
    "--receipt-directory",
    "--profiles-file",
}


def _item(state, version=2, pinned=None, provider="salad", reference=REF, profile="gpu-main"):
    return {
        "provider_id": provider,
        "profile": profile,
        "reference": reference,
        "version": version,
        "pinned_version": pinned,
        "state": state,
        "seconds_remaining": 10,
        "revoked": state == "revoked",
        "reason": f"reason for {state}",
    }


def _client(tmp_path, items, seen, actions=("provider_trust:read",)):
    reference = tmp_path / "token"
    reference.write_text("synthetic-agent-token")
    reference.chmod(0o600)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/external-agent/status":
            return httpx.Response(200, json={"actions": list(actions)})
        return httpx.Response(200, json={"worst_state": "ok", "items": items})

    return ControlClient(
        Config("http://127.0.0.1:8765", reference, "proj1", "dest"), httpx.MockTransport(handler)
    )


def _call(tmp_path, items, arguments=None, seen=None):
    seen = [] if seen is None else seen
    server = create_server(_client(tmp_path, items, seen), provider_trust_tools=True)
    result = asyncio.run(server.call_tool(TOOL_NAME, arguments or {}))
    return result[1] if isinstance(result, tuple) else result


def test_expired_gives_the_next_version_command(tmp_path):
    out = _call(tmp_path, [_item("expired", version=2)])
    (entry,) = out["items"]
    assert entry["command"] == (
        "tessera credentials reenroll salad --credential-directory <CREDENTIAL_DIRECTORY> "
        f"--reference {REF} --version 3 --expires-in-hours 72 "
        "--profiles-file <PROFILES_FILE> < /path/to/secret-file"
    )
    assert entry["provider_id"] == "salad" and entry["state"] == "expired"
    assert entry["profile"] == "gpu-main" and entry["reason"] == "reason for expired"
    assert entry["steps"] and "never accepts" in entry["note"]
    assert "never accepts" in out["note"]


def test_warning_enrolls_the_next_version(tmp_path):
    (entry,) = _call(tmp_path, [_item("warning", version=5)])["items"]
    assert entry["state"] == "warning" and "--version 6" in entry["command"]
    assert any("Delete the previous key" in step for step in entry["steps"])


def test_a_revoked_credential_gets_no_reenroll_command_because_the_cli_refuses_it(tmp_path):
    # The CLI refuses a revoked reference (reference_revoked) before reading stdin, so the tool
    # must not hand out a command that can only fail: the guidance is a new reference.
    (entry,) = _call(tmp_path, [_item("revoked", version=5)])["items"]
    assert entry["state"] == "revoked" and entry["command"] is None
    text = " ".join(entry["steps"])
    assert "NEW reference" in text and "never undone" in text
    assert "reenroll" not in text.replace("re-enrolling", "")


def test_version_mismatch_pins_the_profile_instead(tmp_path):
    (entry,) = _call(tmp_path, [_item("version_mismatch", version=4, pinned=3)])["items"]
    assert "reenroll" not in entry["command"]
    assert entry["pinned_version"] == 3
    words = shlex.split(entry["command"])
    assert words[:3] == ["tessera", "providers", "update"]
    assert words[words.index("--section") + 1] == "salad-profile"
    assert words[words.index("--key") + 1] == "gpu-main"
    assert json.loads(words[words.index("--entry-json") + 1]) == {"credential_version": 4}


def test_healthy_gives_an_empty_list_and_a_statement(tmp_path):
    out = _call(tmp_path, [_item("ok"), _item("unknown")])
    assert out["items"] == [] and out["statement"] == "all credentials healthy"


def test_provider_filter_and_validation(tmp_path):
    items = [_item("expired"), _item("expired", provider="other-one", reference="cred:other")]
    out = _call(tmp_path, items, {"provider": "other-one"})
    assert [e["provider_id"] for e in out["items"]] == ["other-one"]
    assert _call(tmp_path, items, {"provider": "nothing"})["items"] == []
    seen: list = []
    for bad in ("Salad", "a b", "salad;rm", "-x", "a" * 42, ""):
        with pytest.raises(Exception, match="provider must match"):
            _call(tmp_path, items, {"provider": bad}, seen)
    assert seen == []


@pytest.mark.parametrize(
    "reference", ["ref; rm -rf /", "ref$(id)", "a b", "ref`x`", "ref\nx", "ref'q", ""]
)
def test_hostile_reference_is_refused(tmp_path, reference):
    out = _call(tmp_path, [_item("expired", reference=reference)])
    assert out["items"] == [] and out["refused_unsafe_items"] == 1
    assert reference not in json.dumps(out) or reference == ""


def test_hostile_provider_in_the_record_is_refused(tmp_path):
    out = _call(tmp_path, [_item("expired", provider="salad; id")])
    assert out["items"] == [] and out["refused_unsafe_items"] == 1


def test_schema_has_exactly_one_optional_provider_field_and_extra_arguments_send_nothing(tmp_path):
    seen: list = []
    server = create_server(_client(tmp_path, [_item("expired")], seen), provider_trust_tools=True)
    tool = {t.name: t for t in asyncio.run(server.list_tools())}[TOOL_NAME]
    schema = tool.inputSchema
    assert set(schema["properties"]) == {"provider"}
    assert "required" not in schema or schema["required"] == []
    assert schema["additionalProperties"] is False
    seen.clear()
    for extra in ("secret", "token", "value", "credential"):
        with pytest.raises(Exception, match="extra_forbidden|Extra inputs"):
            asyncio.run(server.call_tool(TOOL_NAME, {"provider": "salad", extra: "s3cret"}))
    assert seen == []


def test_registered_only_under_the_health_gate_and_read_only(tmp_path):
    client = _client(tmp_path, [], [])
    names = {t.name for t in asyncio.run(create_server(client).list_tools())}
    assert TOOL_NAME not in names and "tessera_credential_health" not in names
    tools = {
        t.name: t
        for t in asyncio.run(create_server(client, provider_trust_tools=True).list_tools())
    }
    assert TOOL_NAME in tools and "tessera_credential_health" in tools
    assert tools[TOOL_NAME].annotations.readOnlyHint is True
    assert tools[TOOL_NAME].annotations.openWorldHint is False
    assert tools[TOOL_NAME].annotations.destructiveHint is False


def test_only_the_health_route_is_read(tmp_path):
    seen: list = []
    _call(tmp_path, [_item("expired")], seen=seen)
    assert seen == [("GET", "/credential-health")]


def test_generated_command_matches_the_cli_contract_option_names(tmp_path):
    (entry,) = _call(tmp_path, [_item("expired", version=1)])["items"]
    words = shlex.split(entry["command"])
    assert words[:3] == ["tessera", "credentials", "reenroll"]
    redirect = words.index("<")
    assert words[redirect + 1] == "/path/to/secret-file" and len(words) == redirect + 2
    words = words[3:redirect]
    assert words[0] == "salad"
    options = {w for w in words[1:] if w.startswith("--")}
    assert options == {
        "--credential-directory",
        "--reference",
        "--version",
        "--expires-in-hours",
        "--profiles-file",
    }
    assert options <= CONTRACT_OPTIONS
    values = dict(zip(words[1::2], words[2::2], strict=True))
    assert values["--credential-directory"] == "<CREDENTIAL_DIRECTORY>"
    assert values["--reference"] == REF and values["--version"] == "2"


def test_the_generated_command_parses_with_the_real_cli_parser(tmp_path):
    """The words the tool tells the owner to run are accepted by the actual CLI parser."""
    import argparse

    from tessera_control import credential_reenroll

    (entry,) = _call(tmp_path, [_item("expired", version=1)])["items"]
    words = shlex.split(entry["command"])
    words = words[words.index("credentials") - 0 : words.index("<")]
    root = argparse.ArgumentParser(prog="tessera")
    credential_reenroll.add_parser(root.add_subparsers(dest="command", required=True))
    parsed = root.parse_args(words)
    assert parsed.provider == "salad" and parsed.reference == REF
    assert parsed.version == 2 and parsed.expires_in_hours == 72
    assert parsed.profiles_file == "<PROFILES_FILE>"
