"""Owner-only person enrolment tools: secret handling and thin pass-through."""

from __future__ import annotations

import asyncio
import json
import os
import stat

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from tessera_mcp.client import ControlClient
from tessera_mcp.config import Config
from tessera_mcp.server import create_server

TOOLS = {"tessera_enroll_person", "tessera_list_people", "tessera_renew_person"}
STORED = {
    "principal_id": "p1",
    "credential_id": "c1",
    "label": "Reviewer",
    "role": "researcher",
    "kind": "person",
    "expires_at": "2030-01-01T00:00:00Z",
    "enrolled_by_principal_id": "owner1",
}


def _server(tmp_path, handler, *, flag=True):
    reference = tmp_path / "owner.token"
    reference.write_text("synthetic-owner-token")
    reference.chmod(0o600)
    client = ControlClient(
        Config("http://127.0.0.1:8765", reference, "proj1", "dest"), httpx.MockTransport(handler)
    )
    return create_server(client, person_enrollment_tool=flag)


def _call(server, name, arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    return result[1] if isinstance(result, tuple) else result


def _names(server):
    return {t.name for t in asyncio.run(server.list_tools())}


def _dir(tmp_path):
    out = tmp_path / "creds"
    out.mkdir(mode=0o700)
    return out


def test_registered_only_with_the_flag(tmp_path):
    handler = lambda request: httpx.Response(200, json={})  # noqa: E731
    assert not TOOLS & _names(_server(tmp_path, handler, flag=False))
    assert TOOLS <= _names(_server(tmp_path, handler))


def test_flag_alone_registers_only_these_tools(tmp_path, monkeypatch):
    from tessera_mcp import server as module

    reference = tmp_path / "cli.token"
    reference.write_text("synthetic-token")
    reference.chmod(0o600)
    for name, value in {
        "TESSERA_CONTROL_URL": "http://127.0.0.1:8765",
        "TESSERA_AGENT_TOKEN_FILE": str(reference),
        "TESSERA_PROJECT_ID": "proj1",
        "TESSERA_AGENT_DESTINATION": "dest",
    }.items():
        monkeypatch.setenv(name, value)
    built: list = []

    class Stub:
        def run(self, transport):
            return None

    monkeypatch.setattr(module, "create_server", lambda c, **k: built.append(k) or Stub())
    monkeypatch.setattr(module, "_has_provider_trust_scope", lambda client: False)
    monkeypatch.setattr(module, "_live_actions", lambda client: pytest.fail("no live scope read"))
    assert module.main(["--person-enrollment-tool"]) == 0
    assert built == [{"campaign_tools": False, "person_enrollment_tool": True}]
    handler = lambda request: httpx.Response(200, json={})  # noqa: E731
    reference2 = tmp_path / "t2"
    reference2.write_text("x")
    reference2.chmod(0o600)
    client = ControlClient(
        Config("http://127.0.0.1:8765", reference2, "p", "d"), httpx.MockTransport(handler)
    )
    solo = create_server(client, person_enrollment_tool=True)
    assert TOOLS <= _names(solo)
    assert not {n for n in _names(solo) if "campaign" in n or "approve" in n}


def test_enroll_writes_a_private_header_file_and_never_returns_the_token(tmp_path):
    seen: list = []

    def handler(request):
        seen.append((request.method, request.url.path, request.headers["idempotency-key"]))
        seen.append(json.loads(request.content))
        return httpx.Response(201, json=STORED)

    path = _dir(tmp_path) / "person.header"
    server = _server(tmp_path, handler)
    result = _call(
        server,
        "tessera_enroll_person",
        {
            "label": "Reviewer",
            "valid_hours": 24,
            "credential_path": str(path),
            "idempotency_key": "k1",
        },
    )
    method, url, key = seen[0]
    body = seen[1]
    assert (method, url, key) == ("POST", "/people", "k1")
    assert set(body) == {"token", "label", "expires_at"} and body["label"] == "Reviewer"
    assert len(body["token"]) >= 43 and body["expires_at"].endswith("Z")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert path.read_text() == f"Authorization: Bearer {body['token']}\n"
    assert body["token"] not in json.dumps(result)
    assert result["credential_written"] is True and result["credential_path"] == str(path)
    assert result["principal_id"] == "p1" and result["credential_id"] == "c1"
    # replay with the same key: stored ids, no token, no second file write, no second call
    again = _call(
        server,
        "tessera_enroll_person",
        {"label": "Reviewer", "credential_path": str(path), "idempotency_key": "k1"},
    )
    assert again["credential_written"] is False and again["principal_id"] == "p1"
    assert len(seen) == 2


def test_it_refuses_to_overwrite_and_sends_nothing(tmp_path):
    calls: list = []

    def handler(request):
        calls.append(1)
        return httpx.Response(201, json=STORED)

    path = _dir(tmp_path) / "person.header"
    path.write_text("keep")
    with pytest.raises(ToolError, match="refusing to overwrite"):
        _call(
            _server(tmp_path, handler),
            "tessera_enroll_person",
            {"label": "A", "credential_path": str(path)},
        )
    assert path.read_text() == "keep" and not calls


def test_it_refuses_a_missing_or_writable_directory(tmp_path):
    handler = lambda request: pytest.fail("must not call control")  # noqa: E731
    server = _server(tmp_path, handler)
    with pytest.raises(ToolError, match="already exist"):
        _call(
            server,
            "tessera_enroll_person",
            {"label": "A", "credential_path": str(tmp_path / "no" / "f")},
        )
    loose = tmp_path / "loose"
    loose.mkdir()
    loose.chmod(0o777)
    with pytest.raises(ToolError, match="writable"):
        _call(server, "tessera_enroll_person", {"label": "A", "credential_path": str(loose / "f")})


def test_it_writes_nothing_on_a_control_error_and_403_is_passed_through(tmp_path):
    path = _dir(tmp_path) / "person.header"
    server = _server(tmp_path, lambda request: httpx.Response(403, json={"detail": "secret"}))
    with pytest.raises(ToolError) as caught:
        _call(server, "tessera_enroll_person", {"label": "A", "credential_path": str(path)})
    assert "HTTP 403" in str(caught.value) and "secret" not in str(caught.value)
    assert not path.exists()


def test_a_write_failure_after_acceptance_is_honest_and_leaks_no_token(tmp_path, monkeypatch):
    from tessera_mcp import people_tools

    seen: list = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(201, json=STORED)

    def broken(path, token):
        raise OSError("disk full " + token)

    monkeypatch.setattr(people_tools, "_write_header", broken)
    path = _dir(tmp_path) / "person.header"
    with pytest.raises(ToolError) as caught:
        _call(
            _server(tmp_path, handler),
            "tessera_enroll_person",
            {"label": "A", "credential_path": str(path)},
        )
    text = str(caught.value)
    assert seen[0]["token"] not in text
    assert "unusable until" in text and "p1" in text and len(seen) == 1


def test_list_and_renew_paths_and_bodies(tmp_path):
    seen: list = []

    def handler(request):
        seen.append(
            (
                request.method,
                request.url.raw_path.decode(),
                request.headers.get("idempotency-key"),
                json.loads(request.content) if request.content else None,
            )
        )
        return httpx.Response(200, json={"ok": True})

    server = _server(tmp_path, handler)
    _call(server, "tessera_list_people", {"offset": 5, "limit": 10})
    _call(
        server,
        "tessera_renew_person",
        {"principal_id": "p/1", "valid_hours": 48, "idempotency_key": "r1"},
    )
    assert seen[0] == ("GET", "/people?offset=5&limit=10", None, None)
    method, url, key, body = seen[1]
    assert (method, url, key) == ("POST", "/people/p%2F1/renew", "r1")
    assert list(body) == ["expires_at"] and body["expires_at"].endswith("Z")
    with pytest.raises(ToolError):
        _call(server, "tessera_renew_person", {"principal_id": "p1", "valid_hours": 169})
    with pytest.raises(ToolError):
        _call(server, "tessera_list_people", {"limit": 0})
