"""Local real HTTP/stdio integration; all authority is isolated test-issued auth."""

import asyncio
import json
import os
import socket
import sqlite3
import sys
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_admin_configuration import (
    command,
    config,
    create,
    grant,
)
from test_admin_configuration import (
    f as authority_fixture,  # noqa: F401 -- shared isolated authority fixture
)

from tessera_operator.salad_control import ControlUnavailable, SaladControl


class RedactedAuthority(dict):
    def __repr__(self):
        return "<isolated test authority; credential values redacted>"


@pytest.fixture
def f(authority_fixture):  # noqa: F811 -- pytest resolves the imported fixture by name
    return RedactedAuthority(authority_fixture)


@pytest.fixture
def target(f):
    record = create(f, config())
    delegation = create(f, grant(f, record), "agent")
    delegation = command(f, delegation, "approve")
    return record, delegation


@pytest.fixture
def server(f, tmp_path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    service = uvicorn.Server(
        uvicorn.Config(f["client"].app, log_level="critical", access_log=False, lifespan="off")
    )
    thread = threading.Thread(target=lambda: service.run(sockets=[listener]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not service.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert service.started
    token = tmp_path.resolve() / "operator-token"
    token.write_text(f["agent"]["Authorization"].removeprefix("Bearer "))
    token.chmod(0o600)
    try:
        yield origin, token
    finally:
        service.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_rev003_configure_pins_delegated_token_across_owner_file_swap(
    f, target, server, monkeypatch
):
    record, _ = target
    origin, token_file = server
    control = SaladControl(origin, token_file, f["project"], record["id"])
    original_request = control._request
    swapped = False

    def racing_request(method, suffix, **kwargs):
        nonlocal swapped
        response = original_request(method, suffix, **kwargs)
        if method == "GET" and not swapped:
            replacement = token_file.with_name("replacement")
            replacement.write_text(f["owner"]["Authorization"].removeprefix("Bearer "))
            replacement.chmod(0o600)
            os.replace(replacement, token_file)
            swapped = True
        return response

    monkeypatch.setattr(control, "_request", racing_request)
    result = None
    try:
        result = control.configure(
            "luxai", "tessera-research", None, record["revision"], "pin-race"
        )
    except ControlUnavailable:
        pass  # Examine actual authority DB, never log credential-bearing exceptions/headers.
    with sqlite3.connect(f["state"] / "tessera.sqlite3") as db:
        rows = db.execute(
            "SELECT actor_principal_id FROM operator_audit_events "
            "WHERE event_type IN ('admin.state','admin.command') "
            "AND json_extract(payload_json,'$.id')=? AND json_extract(payload_json,'$.revision')>?",
            (record["id"], record["revision"]),
        ).fetchall()
    assert rows == [(f["agent_id"],), (f["agent_id"],)]  # Both state/command audits are delegated.
    assert result is not None and result["authority"] == "delegated"
    assert result["record"]["revision"] == record["revision"] + 1
    # New operations reload the now-owner file rather than caching the earlier delegate.
    with pytest.raises(ControlUnavailable, match="separate_delegated_operator_required"):
        control.inspect()
    with pytest.raises(ControlUnavailable, match="separate_delegated_operator_required"):
        control.configure("luxai", "other-name", None, result["record"]["revision"], "new-owner")
    with sqlite3.connect(f["state"] / "tessera.sqlite3") as db:
        assert (
            db.execute(
                "SELECT count(*) FROM operator_audit_events WHERE event_type='admin.state' "
                "AND json_extract(payload_json,'$.id')=?",
                (record["id"],),
            ).fetchone()[0]
            == 2
        )


@pytest.mark.parametrize("revocation", ["credential", "delegation"])
def test_rev003_pinned_delegate_revocation_still_denied_before_post(
    f, target, server, monkeypatch, revocation
):
    record, delegation = target
    origin, token_file = server
    control = SaladControl(origin, token_file, f["project"], record["id"])
    original_request = control._request
    revoked = False

    def racing_request(method, suffix, **kwargs):
        nonlocal revoked
        response = original_request(method, suffix, **kwargs)
        if method == "GET" and not revoked:
            replacement = token_file.with_name("replacement")
            replacement.write_text(f["owner"]["Authorization"].removeprefix("Bearer "))
            replacement.chmod(0o600)
            os.replace(replacement, token_file)
            if revocation == "credential":
                response_revoke = f["client"].post(
                    f"/credentials/{f['agent_credential']}/revoke",
                    headers=f["owner"] | {"Idempotency-Key": "revoke-mid-operation"},
                )
                assert response_revoke.status_code == 200
            else:
                command(f, delegation, "revoke")
            revoked = True
        return response

    monkeypatch.setattr(control, "_request", racing_request)
    with pytest.raises(
        ControlUnavailable, match="control_authentication_required|control_scope_denied"
    ):
        control.configure("luxai", "tessera-research", None, record["revision"], "revoked-pin")
    with sqlite3.connect(f["state"] / "tessera.sqlite3") as db:
        assert (
            db.execute(
                "SELECT count(*) FROM operator_audit_events "
                "WHERE event_type IN ('admin.state','admin.command') "
                "AND json_extract(payload_json,'$.id')=? "
                "AND json_extract(payload_json,'$.revision')>?",
                (record["id"], record["revision"]),
            ).fetchone()[0]
            == 0
        )


def test_projection_scope_revoke_project_and_secret_exclusion(f, target):
    record, delegation = target
    path = f"/admin/projects/{f['project']}/records/{record['id']}/salad-setup"
    response = f["client"].get(path, headers=f["agent"])
    assert response.status_code == 200
    view = response.json()
    assert view["authority"] == "delegated"
    assert view["state"] == "project_creation_requires_portal"
    assert view["project_created"] is False and not view["execution_enabled"]
    assert "credential-ref:" not in response.text
    other = create(f, config())
    assert (
        f["client"].get(path.replace(record["id"], other["id"]), headers=f["agent"]).status_code
        == 403
    )
    assert (
        f["client"]
        .get(path.replace(f["project"], "foreign-project"), headers=f["agent"])
        .status_code
        == 404
    )
    command(f, delegation, "revoke")
    assert f["client"].get(path, headers=f["agent"]).status_code == 403


def test_projection_requires_both_scopes_and_rejects_external_research(f):
    record = create(f, config())
    delegation = create(f, grant(f, record, actions=["configuration.read"]), "agent")
    command(f, delegation, "approve")
    path = f"/admin/projects/{f['project']}/records/{record['id']}/salad-setup"
    assert f["client"].get(path, headers=f["agent"]).status_code == 403
    # Existing external-agent restriction is not widened even when a target exists.
    with sqlite3.connect(f["state"] / "tessera.sqlite3") as db:
        db.execute(
            "INSERT INTO external_agent_scopes VALUES (?,?,?)",
            (f["agent_credential"], json.dumps(["context:read"]), "research-only"),
        )
    assert f["client"].get(path, headers=f["agent"]).status_code in {401, 403}


def test_projection_credential_expiry_preserves_record(f, target):
    record, _ = target
    path = f"/admin/projects/{f['project']}/records/{record['id']}/salad-setup"
    assert f["client"].get(path, headers=f["agent"]).status_code == 200
    with sqlite3.connect(f["state"] / "tessera.sqlite3") as db:
        db.execute(
            "UPDATE credentials SET expires_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), f["agent_credential"]),
        )
    assert f["client"].get(path, headers=f["agent"]).status_code in {401, 403}
    owner = f["client"].get(path, headers=f["owner"])
    assert owner.status_code == 200 and owner.json()["record"]["revision"] == record["revision"]


def test_real_stdio_http_authoritative_replay_cas_and_reconnect(f, target, server):
    record, _ = target
    origin, token = server

    async def roundtrip(write):
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-B",
                "-m",
                "tessera_operator.salad",
                "--control-url",
                origin,
                "--control-token-file",
                str(token),
                "--control-project-id",
                f["project"],
                "--control-record-id",
                record["id"],
            ],
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                assert {t.name for t in (await session.list_tools()).tools} == {
                    "salad_inspect",
                    "salad_configure",
                    "salad_readiness",
                }
                if write:
                    args = {
                        "organization": "luxai",
                        "expected_revision": record["revision"],
                        "idempotency_key": "setup-first",
                    }
                    first = await session.call_tool("salad_configure", args)
                    saved = json.loads(first.content[0].text)
                    assert saved["record"]["revision"] == record["revision"] + 1
                    replay = await session.call_tool("salad_configure", args)
                    assert json.loads(replay.content[0].text) == saved
                    stale = await session.call_tool(
                        "salad_configure",
                        args
                        | {"proposed_project": "other-name", "idempotency_key": "stale-change"},
                    )
                    assert (
                        json.loads(stale.content[0].text)["reason"]
                        == "control_revision_or_replay_conflict"
                    )
                    changed = await session.call_tool(
                        "salad_configure", args | {"proposed_project": "other-name"}
                    )
                    assert (
                        json.loads(changed.content[0].text)["reason"]
                        == "control_revision_or_replay_conflict"
                    )
                result = await session.call_tool("salad_readiness", {})
                view = json.loads(result.content[0].text)
                assert view["record"]["spec"]["salad_organization"] == "luxai"
                assert view["record"]["spec"]["salad_project"] is None
                assert view["proposed_project"] == "tessera-research"
                assert view["provider_validation"] == "not_performed_by_control"
                assert view["control_integration"] == "metadata_only_connected"
                assert not view["execution_enabled"]
                return view

    first = asyncio.run(roundtrip(True))
    assert asyncio.run(roundtrip(False)) == first
    # A fresh independent HTTP read confirms the authoritative DB, not an MCP cache.
    response = f["client"].get(
        f"/admin/projects/{f['project']}/records/{record['id']}/salad-setup", headers=f["agent"]
    )
    assert response.json() == first
    with sqlite3.connect(f["state"] / "tessera.sqlite3") as db:
        assert (
            db.execute(
                "SELECT count(*) FROM idempotency WHERE idempotency_key='setup-first'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "origin",
    [
        "http://remote.invalid",
        "https://user:pass@example.invalid",
        "https://example.invalid/path",
        "https://example.invalid?key=bad",
    ],
)
def test_control_origin_and_path_closed(origin, tmp_path):
    with pytest.raises(ValueError):
        SaladControl(origin, tmp_path / "token", "project", "admin_record")
    with pytest.raises(ValueError):
        SaladControl("http://127.0.0.1", tmp_path / "token", "../escape", "admin_record")


def test_no_owner_substitution_and_no_cache_success(f, target, server, tmp_path):
    origin, token = server
    record, _ = target
    control = SaladControl(origin, token, f["project"], record["id"])
    assert control.inspect()["authority"] == "delegated"
    owner = tmp_path.resolve() / "isolated-owner-token"
    owner.write_text(f["owner"]["Authorization"].removeprefix("Bearer "))
    owner.chmod(0o600)
    with pytest.raises(ControlUnavailable, match="separate_delegated_operator_required"):
        SaladControl(origin, owner, f["project"], record["id"]).inspect()
    with pytest.raises(ControlUnavailable, match="separate_delegated_operator_required"):
        SaladControl(origin, owner, f["project"], record["id"]).configure(
            "luxai", "tessera-research", None, record["revision"], "initial-owner"
        )
    with sqlite3.connect(f["state"] / "tessera.sqlite3") as db:
        assert (
            db.execute(
                "SELECT count(*) FROM operator_audit_events WHERE event_type='admin.state' "
                "AND json_extract(payload_json,'$.id')=?",
                (record["id"],),
            ).fetchone()[0]
            == 1
        )
    token.chmod(0o644)
    with pytest.raises(ControlUnavailable, match="control_unavailable"):
        control.inspect()
    token.chmod(0o600)
    os.unlink(token)
    with pytest.raises(ControlUnavailable, match="control_unavailable"):
        control.inspect()


def test_redirect_denied_without_raw_error_or_retry(tmp_path, monkeypatch):
    token = tmp_path.resolve() / "token"
    token.write_text("isolated_" + "x" * 48)
    token.chmod(0o600)
    calls = []

    def handle(request):
        calls.append(request.url)
        return httpx.Response(
            302, headers={"Location": "https://foreign.invalid"}, text="sensitive provider details"
        )

    original = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handle))
    )
    control = SaladControl("http://127.0.0.1:1", token, "project", "admin_record")
    with pytest.raises(ControlUnavailable, match="control_request_failed") as error:
        control.inspect()
    assert len(calls) == 1 and "sensitive" not in str(error.value)
