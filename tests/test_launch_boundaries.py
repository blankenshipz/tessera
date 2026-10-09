"""Local launch safeguards; no live service, provider or real credentials."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tessera_control.api import create_app
from tessera_control.deployment import DeploymentConfig
from tessera_control.models import CreateCredential, ExecutionSpec
from tessera_control.request_limits import (
    MAX_REQUEST_BYTES,
    MAX_TOKEN_CHARS,
    RequestLimitsMiddleware,
)
from tessera_control.store import initialize_state


def _intake(messages, *, headers=(), max_bytes=8, stall=False):
    sent, admitted = [], []
    pending = iter(messages)

    async def receive():
        if stall:
            await asyncio.sleep(1)
        return next(pending)

    async def send(message):
        sent.append(message)

    async def app(scope, receive, send):
        admitted.append(await receive())
        # Multiple response chunks must not be buffered or altered.
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        await send({"type": "http.response.body", "body": b"last"})

    asyncio.run(
        RequestLimitsMiddleware(app, max_bytes=max_bytes, timeout_seconds=0.01)(
            {"type": "http", "headers": list(headers)}, receive, send
        )
    )
    return sent, admitted


@pytest.mark.parametrize("declared", [None, b"8"])
def test_exact_limit_chunked_body_is_admitted_and_response_stream_is_preserved(declared):
    headers = [] if declared is None else [(b"content-length", declared)]
    sent, admitted = _intake(
        [
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"5678"},
        ],
        headers=headers,
    )
    assert admitted == [{"type": "http.request", "body": b"12345678", "more_body": False}]
    assert [message.get("body") for message in sent[1:]] == [b"first", b"last"]


@pytest.mark.parametrize("declared", [None, b"1", b"8"])
def test_actual_stream_size_is_bounded_even_with_missing_or_false_length(declared):
    sent, admitted = _intake(
        [
            {"type": "http.request", "body": b"12345678", "more_body": True},
            {"type": "http.request", "body": b"9"},
        ],
        headers=[] if declared is None else [(b"content-length", declared)],
    )
    assert sent[0]["status"] == 413
    assert admitted == []


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ([(b"content-length", b"9")], 413),
        ([(b"content-length", b"-1")], 400),
        ([(b"content-length", b"1.0")], 400),
        ([(b"content-length", b" 1")], 400),
        ([(b"content-length", b"9" * 100)], 400),
        ([(b"content-length", b"1"), (b"content-length", b"1")], 400),
        ([(b"authorization", b"Bearer a"), (b"authorization", b"Bearer b")], 401),
        ([(b"authorization", b"Bearer " + b"x" * (MAX_TOKEN_CHARS + 1))], 401),
    ],
)
def test_bad_headers_are_rejected_without_reading_body_or_echoing_secrets(headers, status):
    sent, admitted = _intake([], headers=headers)
    assert sent[0]["status"] == status
    assert admitted == []
    assert b"Bearer a" not in sent[-1]["body"]


def test_short_body_disconnect_and_slow_body_never_admit_commands():
    sent, admitted = _intake(
        [{"type": "http.request", "body": b"a"}], headers=[(b"content-length", b"2")]
    )
    assert sent[0]["status"] == 400 and not admitted
    sent, admitted = _intake([{"type": "http.disconnect"}])
    assert not sent and not admitted
    sent, admitted = _intake([], stall=True)
    assert sent[0]["status"] == 408 and not admitted


def test_api_intake_rejection_leaves_no_command_or_idempotency_effect(tmp_path):
    state = tmp_path / "state"
    initialize_state(state)
    owner = (state / "owner.header").read_text().strip().split(": ", 1)[1]
    with TestClient(create_app(state)) as client:
        headers = {"Authorization": owner, "Idempotency-Key": "bounded-project"}
        denied = client.post(
            "/projects",
            headers=headers | {"Content-Length": str(MAX_REQUEST_BYTES + 1)},
            content=b"{}",
        )
        assert denied.status_code == 413
        assert client.get("/projects", headers=headers).json() == {"projects": []}
        created = client.post("/projects", headers=headers, json={"name": "Bounded"})
        assert created.status_code == 201  # rejected intake did not consume the key
        duplicate_auth = client.get(
            "/me", headers=[("Authorization", owner), ("Authorization", owner)]
        )
        assert duplicate_auth.status_code == 401
        assert owner not in duplicate_auth.text


@pytest.mark.parametrize(
    "host",
    [
        "*",
        "*.example.test",
        "host:8765",
        "host@evil",
        "bad host",
        "host\n",
        "-host",
        "host..test",
        "host?query",
        "host#fragment",
        "host\\path",
        "[not-ipv6]",
    ],
)
def test_remote_trusted_hosts_are_concrete_hostnames(host):
    with pytest.raises(ValueError):
        DeploymentConfig.remote(["control.example.test", host], "https://control.example.test")


@pytest.mark.parametrize(
    "suffix",
    [":0", ":65536", ":bad", ":", "/path", "?", "#", "\n", "\x7f"],
)
def test_remote_origin_rejects_invalid_ports_paths_and_parser_normalization(suffix):
    with pytest.raises(ValueError):
        DeploymentConfig.remote(["control.example.test"], "https://control.example.test" + suffix)


def test_remote_origin_rejects_even_empty_userinfo_and_accepts_explicit_tls_port():
    with pytest.raises(ValueError):
        DeploymentConfig.remote(["control.example.test"], "https://@control.example.test")
    config = DeploymentConfig.remote(["CONTROL.example.test"], "https://control.example.test:8443")
    assert config.public_origin == "https://control.example.test:8443"
    assert config.profile == "remote-lab-v1"  # syntax validation is not production auth


@pytest.mark.parametrize("profile", ["local-cpu", "kubernetes-opencode"])
def test_output_plan_limit_matches_runtime_and_quarantine(profile):
    from tessera_control.artifacts import MAX_ARTIFACT_BYTES, MAX_BASE64_CHARS

    spec = {
        "argv": ["python", "analysis.py"],
        "input_snapshot_digest": "a" * 64,
        "pinned_skill_digests": [],
        "output_paths": ["result.txt"],
        "profile": profile,
        "max_output_bytes": MAX_ARTIFACT_BYTES,
    }
    assert ExecutionSpec.model_validate(spec).max_output_bytes == MAX_ARTIFACT_BYTES
    for invalid in (MAX_ARTIFACT_BYTES + 1, 0, True, "1024"):
        with pytest.raises(ValidationError):
            ExecutionSpec.model_validate(spec | {"max_output_bytes": invalid})
    assert MAX_BASE64_CHARS + 1024 * 1024 < MAX_REQUEST_BYTES


def test_credential_issuance_bound_matches_auth_intake_without_echoing_secret():
    body = {
        "kind": "human_session",
        "audience": "control",
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
        "token": "x" * MAX_TOKEN_CHARS,
    }
    credential = CreateCredential.model_validate(body)
    assert body["token"] not in credential.model_dump_json()
    with pytest.raises(ValidationError):
        CreateCredential.model_validate(body | {"token": "x" * (MAX_TOKEN_CHARS + 1)})
    assert json.loads(credential.model_dump_json())["token"] == "**********"


def test_oversized_output_plan_is_rejected_before_revision_or_approval(tmp_path):
    from test_plans import _headers, _project, _spec, _task

    state = tmp_path / "state"
    initialize_state(state)
    with TestClient(create_app(state)) as client:
        project = _project(client, state)
        task = _task(client, state, project["id"])
        body = {
            "expected_revision": task["revision"],
            "title": "Bounded plan",
            "purpose": "Reject unsupported output before approval",
            "budget_cents": 0,
            "max_duration_seconds": 60,
            "execution_spec": _spec() | {"max_output_bytes": 16 * 1024 * 1024 + 1},
        }
        headers = _headers(state, "output-plan")
        denied = client.put(f"/tasks/{task['id']}/plan", headers=headers, json=body)
        assert denied.status_code == 422
        unchanged = client.get(f"/tasks/{task['id']}", headers=headers).json()
        assert unchanged == task
        body["execution_spec"]["max_output_bytes"] = 16 * 1024 * 1024
        accepted = client.put(f"/tasks/{task['id']}/plan", headers=headers, json=body)
        assert accepted.status_code == 200
        assert accepted.json()["revision"] == task["revision"] + 1
