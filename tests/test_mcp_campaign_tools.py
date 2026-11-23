"""Campaign MCP tools call the right control paths; no approval tool exists."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from mcp_required import require_mcp

require_mcp()

from tessera_mcp.client import ControlClient  # noqa: E402
from tessera_mcp.config import Config  # noqa: E402
from tessera_mcp.server import create_server  # noqa: E402

SPEC = {
    "version": 1,
    "name": "synthetic",
    "budget_cap_cents": 2,
    "deadline_seconds": 600,
    "worker_principal_id": "worker",
    "stages": [
        {
            "key": "s1",
            "backend": "docker-local-cpu-v1",
            "image": "python@sha256:" + "a" * 64,
            "argv": ["python", "-c", "print(1)"],
            "output_paths": ["o.json"],
            "max_output_bytes": 10,
            "max_duration_seconds": 60,
            "inputs": [
                {
                    "kind": "snapshot",
                    "name": "code",
                    "snapshot_id": "snap",
                    "digest": "b" * 64,
                    "data_class": "public",
                }
            ],
        }
    ],
}


def _server(tmp_path, seen, *, campaign_tools=True):
    reference = tmp_path / "token"
    reference.write_text("synthetic-agent-token")
    reference.chmod(0o600)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.method,
                request.url.path,
                request.headers.get("idempotency-key"),
                request.content,
            )
        )
        return httpx.Response(200, json={"id": "cmp_1", "status": "submitted"})

    client = ControlClient(
        Config("http://127.0.0.1:8765", reference, "proj1", "dest"), httpx.MockTransport(handler)
    )
    return create_server(client, campaign_tools=campaign_tools)


def _call(server, name, arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    return result[1] if isinstance(result, tuple) else result


def test_tools_are_opt_in_and_approval_is_not_exposed(tmp_path):
    names = {t.name for t in asyncio.run(_server(tmp_path, [], campaign_tools=False).list_tools())}
    assert not {n for n in names if "campaign" in n}
    tools = {t.name: t for t in asyncio.run(_server(tmp_path, []).list_tools())}
    campaign = {n for n in tools if "campaign" in n}
    # Forced on (no live scope list) every campaign tool registers; the control API still
    # enforces each scope. Scope-gated registration is covered in test_mcp_campaign_client_path.
    assert campaign == {
        "tessera_submit_campaign",
        "tessera_get_campaign",
        "tessera_cancel_campaign",
        "tessera_list_campaigns",
        "tessera_get_campaign_receipt",
        "tessera_campaign_report",
        "tessera_campaign_outputs",
        "tessera_campaign_logs",
        "tessera_campaign_budget",
        "tessera_campaign_retries",
        "tessera_campaign_standing_status",
        "tessera_campaign_gate_decisions",
        # Platform stuck-campaign findings: read-only list (the acknowledge tool has no "campaign"
        # in its name; both are covered in test_mcp_campaign_stuck.py).
        "tessera_list_stuck_campaigns",
        # ADR 0068: read-only holdout claims and scored metrics.
        "tessera_campaign_holdout",
        "tessera_campaign_holdout_claims",
        # ADR 0055 steering: a dry-run, a grant read, and two reduce-only actions.
        "tessera_validate_campaign_spec",
        "tessera_campaign_grants",
        "tessera_reduce_campaign_budget",
        "tessera_revoke_campaign_release",
        # ADR 0061: pause/resume, lower-only concurrency limit, single-stage cancel.
        "tessera_pause_campaign",
        "tessera_resume_campaign",
        "tessera_limit_campaign_concurrency",
        "tessera_cancel_campaign_stage",
        # ADR 0069: re-run a finished campaign as a new submission (a person approves it).
        "tessera_rerun_campaign",
        # ADR 0060: filters a stage's approved provider candidates.
        "tessera_steer_campaign_placement",
        # ADR 0093: an agent submits a campaign a person already cleared (one-shot).
        "tessera_submit_cleared_campaign",
    }
    assert "tessera_list_snapshots" in tools
    assert tools["tessera_get_campaign"].annotations.readOnlyHint is True
    for name in ("tessera_submit_campaign", "tessera_cancel_campaign"):
        assert tools[name].annotations.readOnlyHint is False
        assert tools[name].annotations.destructiveHint is False
    assert "approve" not in " ".join(tools)
    assert "grants nothing" in tools["tessera_submit_campaign"].description.lower()


def test_submit_get_and_cancel_hit_the_expected_paths(tmp_path):
    seen: list = []
    server = _server(tmp_path, seen)
    _call(server, "tessera_submit_campaign", {"spec": SPEC, "idempotency_key": "k1"})
    _call(server, "tessera_get_campaign", {"campaign_id": "cmp_1"})
    _call(
        server,
        "tessera_cancel_campaign",
        {"campaign_id": "cmp_1", "expected_revision": 2, "reason": "stop", "idempotency_key": "k2"},
    )
    assert [(m, p, k) for m, p, k, _ in seen] == [
        ("POST", "/projects/proj1/campaigns", "k1"),
        ("GET", "/campaigns/cmp_1", None),
        ("POST", "/campaigns/cmp_1/cancel", "k2"),
    ]
    assert json.loads(seen[0][3])["spec"]["name"] == "synthetic"
    assert json.loads(seen[2][3]) == {"expected_revision": 2, "reason": "stop"}


def test_invalid_spec_never_reaches_control(tmp_path):
    seen: list = []
    server = _server(tmp_path, seen)
    with pytest.raises(Exception):  # noqa: B017 - SDK wraps validation errors
        _call(
            server,
            "tessera_submit_campaign",
            {"spec": {**SPEC, "stages": []}, "idempotency_key": "k"},
        )
    assert seen == []


def _main_with_status(monkeypatch, status):
    """Run `main` with a faked status read; return the campaign_tools flag it chose."""
    from tessera_mcp import server as module

    chosen: dict = {}

    class FakeConfig:
        @staticmethod
        def from_env():
            return FakeConfig()

        def token(self):
            return "synthetic"

    class FakeClient:
        def __init__(self, config):
            pass

        async def status(self):
            if isinstance(status, Exception):
                raise status
            return status

    class FakeServer:
        def run(self, transport):
            chosen["transport"] = transport

    def fake_create(client, *, campaign_tools=False, campaign_actions=None):
        chosen["campaign_tools"] = campaign_tools
        return FakeServer()

    monkeypatch.setattr(module, "Config", FakeConfig)
    monkeypatch.setattr(module, "ControlClient", FakeClient)
    monkeypatch.setattr(module, "create_server", fake_create)
    return module, chosen


@pytest.mark.parametrize(
    ("status", "argv", "expected"),
    [
        ({"actions": ["campaign:read", "other"]}, [], True),
        ({"actions": ["campaign:submit"]}, [], True),
        ({"actions": ["proposal:create"]}, [], False),
        (RuntimeError("down"), [], False),
        ({"unexpected": 1}, [], False),
        ({"actions": ["campaign:read"]}, ["--no-campaign-tools"], False),
        ({"actions": ["proposal:create"]}, ["--campaign-tools"], True),
    ],
)
def test_main_registers_campaign_tools_from_live_scopes(monkeypatch, status, argv, expected):
    module, chosen = _main_with_status(monkeypatch, status)
    assert module.main(argv) == 0
    assert chosen == {"campaign_tools": expected, "transport": "stdio"}
