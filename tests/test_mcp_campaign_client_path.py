"""MCP client path: v2 submit, list/receipt, snapshot/output/log/budget tools, scope gating."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from mcp_required import require_mcp

require_mcp()

from tessera_mcp.client import ControlClient  # noqa: E402
from tessera_mcp.config import Config  # noqa: E402
from tessera_mcp.harness_binding import PERMITTED_TOOLS  # noqa: E402
from tessera_mcp.server import CAMPAIGN_ACTIONS, create_server  # noqa: E402

IMAGE = "python@sha256:" + "a" * 64
V2 = {
    "version": 2,
    "name": "v2",
    "budget_cap_cents": 10,
    "deadline_seconds": 3600,
    "stages": [
        {
            "key": "a",
            "provider": "local-docker",
            "runtime": "docker-cpu-v1",
            "image": IMAGE,
            "argv": ["python", "-c", "print(1)"],
            "outputs": [{"name": "result", "path": "out.json", "max_bytes": 1024}],
            "max_duration_seconds": 60,
            "inputs": [
                {
                    "kind": "artifact",
                    "name": "pinned",
                    "campaign_id": "campaign_1",
                    "stage": "a",
                    "output": "result",
                    "sha256": "c" * 64,
                }
            ],
        }
    ],
}


def _server(tmp_path, seen, actions=None):
    reference = tmp_path / "token"
    reference.write_text("synthetic-agent-token")
    reference.chmod(0o600)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.url.query.decode(), request.content))
        return httpx.Response(200, json={"ok": True})

    client = ControlClient(
        Config("http://127.0.0.1:8765", reference, "proj1", "dest"), httpx.MockTransport(handler)
    )
    return create_server(
        client,
        campaign_tools=True,
        campaign_actions=None if actions is None else frozenset(actions),
    )


def _call(server, name, arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    return result[1] if isinstance(result, tuple) else result


def _names(server):
    return {
        t.name for t in asyncio.run(server.list_tools()) if "campaign" in t.name or "snap" in t.name
    }


def test_submit_accepts_v2_spec_unchanged(tmp_path):
    seen: list = []
    _call(_server(tmp_path, seen), "tessera_submit_campaign", {"spec": V2, "idempotency_key": "k"})
    method, path, _, body = seen[0]
    assert (method, path) == ("POST", "/projects/proj1/campaigns")
    sent = json.loads(body)["spec"]
    assert sent["version"] == 2
    assert sent["stages"][0]["provider"] == "local-docker"
    assert sent["stages"][0]["inputs"][0]["kind"] == "artifact"


def test_invalid_v2_spec_never_reaches_control(tmp_path):
    seen: list = []
    broken = {**V2, "stages": [{**V2["stages"][0], "provider": "nowhere"}]}
    with pytest.raises(Exception):  # noqa: B017 - SDK wraps validation errors
        _call(
            _server(tmp_path, seen),
            "tessera_submit_campaign",
            {"spec": broken, "idempotency_key": "k"},
        )
    assert seen == []


def test_read_tools_hit_expected_routes(tmp_path):
    seen: list = []
    server = _server(tmp_path, seen)
    _call(server, "tessera_list_campaigns", {"offset": 5, "limit": 10})
    _call(server, "tessera_get_campaign_receipt", {"campaign_id": "c1"})
    _call(server, "tessera_list_snapshots", {})
    _call(server, "tessera_campaign_outputs", {"campaign_id": "c1"})
    _call(server, "tessera_campaign_logs", {"campaign_id": "c1"})
    _call(server, "tessera_campaign_stage_watch", {"campaign_id": "c1"})
    _call(server, "tessera_campaign_stage_watch", {"campaign_id": "c1", "offset": 2, "limit": 5})
    _call(server, "tessera_campaign_stage_status", {"campaign_id": "c1"})
    _call(server, "tessera_campaign_budget", {"campaign_id": "c1"})
    _call(server, "tessera_campaign_retries", {"campaign_id": "c1"})
    _call(server, "tessera_campaign_standing_status", {})
    _call(server, "tessera_campaign_gate_decisions", {"campaign_id": "c1"})
    _call(server, "tessera_campaign_holdout", {"campaign_id": "c1"})
    _call(server, "tessera_campaign_holdout_claims", {})
    _call(server, "tessera_campaign_holdout_claims", {"partition_digest": "a" * 64})
    assert [(m, p, q) for m, p, q, _ in seen] == [
        ("GET", "/projects/proj1/campaigns", "offset=5&limit=10"),
        ("GET", "/campaigns/c1/receipt", ""),
        ("GET", "/projects/proj1/agent-snapshots", "offset=0&limit=50"),
        ("GET", "/campaigns/c1/outputs", ""),
        ("GET", "/campaigns/c1/logs", ""),
        ("GET", "/campaigns/c1/stage-watch", "offset=0&limit=20"),
        ("GET", "/campaigns/c1/stage-watch", "offset=2&limit=5"),
        ("GET", "/campaigns/c1/stage-status", ""),
        ("GET", "/campaigns/c1/budget", ""),
        ("GET", "/campaigns/c1/retries", ""),
        ("GET", "/projects/proj1/agent-standing-status", ""),
        ("GET", "/campaigns/c1/gate-decisions", ""),
        ("GET", "/campaigns/c1/holdout", ""),
        ("GET", "/projects/proj1/holdout-claims", ""),
        ("GET", "/projects/proj1/holdout-claims", "partition_digest=" + "a" * 64),
    ]


def test_bad_pagination_and_ids_are_refused_locally(tmp_path):
    seen: list = []
    server = _server(tmp_path, seen)
    for tool, arguments in (
        ("tessera_list_campaigns", {"limit": 0}),
        ("tessera_list_snapshots", {"offset": -1}),
        ("tessera_get_campaign_receipt", {"campaign_id": ".."}),
        ("tessera_campaign_logs", {"campaign_id": ""}),
        ("tessera_campaign_stage_watch", {"campaign_id": "c1", "limit": 101}),
        ("tessera_campaign_stage_watch", {"campaign_id": "c1", "offset": -1}),
        ("tessera_campaign_stage_watch", {"campaign_id": ".."}),
    ):
        with pytest.raises(Exception):  # noqa: B017
            _call(server, tool, arguments)
    assert seen == []


def test_tools_register_only_for_enrolled_scopes(tmp_path):
    assert _names(_server(tmp_path, [], ["campaign:read"])) == {
        "tessera_get_campaign",
        "tessera_list_campaigns",
        "tessera_get_campaign_receipt",
        "tessera_campaign_retries",
        "tessera_campaign_standing_status",
        "tessera_campaign_gate_decisions",
        "tessera_campaign_holdout",
        "tessera_campaign_holdout_claims",
        "tessera_list_stuck_campaigns",
        "tessera_campaign_report",
        "tessera_campaign_wave_receipts",
    }
    assert _names(_server(tmp_path, [], ["campaign:submit", "snapshot:read"])) == {
        "tessera_submit_campaign",
        "tessera_validate_campaign_spec",
        "tessera_render_campaign_spec",
        "tessera_submit_rendered_campaign",
        "tessera_list_snapshots",
        "tessera_get_snapshot",
        "tessera_get_snapshot_license_labels",
    }
    assert _names(_server(tmp_path, [], ["artifact:read", "campaign:logs", "budget:read"])) == {
        "tessera_campaign_outputs",
        "tessera_campaign_logs",
        "tessera_campaign_stage_watch",
        "tessera_campaign_stage_status",
        "tessera_campaign_grants",
        "tessera_campaign_budget",
    }


def test_mutating_annotations_and_catalog_isolation(tmp_path):
    tools = {t.name: t for t in asyncio.run(_server(tmp_path, []).list_tools())}
    assert tools["tessera_submit_campaign"].annotations.readOnlyHint is False
    assert tools["tessera_cancel_campaign"].annotations.readOnlyHint is False
    for name in (
        "tessera_list_campaigns",
        "tessera_list_snapshots",
        "tessera_campaign_logs",
        "tessera_campaign_stage_watch",
        "tessera_get_snapshot",
        "tessera_campaign_stage_status",
    ):
        assert tools[name].annotations.readOnlyHint is True
        assert tools[name].annotations.destructiveHint is False
    # None of the optional tools may leak into the frozen harness catalog.
    assert not {n for n in tools if "campaign" in n or "snapshot" in n} & set(PERMITTED_TOOLS)
    # budget:read alone (a results-only credential) never switches campaign tools on.
    assert "budget:read" not in CAMPAIGN_ACTIONS
    assert "tessera_get_campaign_receipt" in tools["tessera_get_campaign"].description


def _chained(**ref):
    stage = {
        **V2["stages"][0],
        "inputs": [
            {"kind": "receipt_output", "name": "prev", "stage": "a", "output": "result", **ref}
        ],
    }
    return {**V2, "stages": [stage]}


def test_submit_receipt_output_with_predecessors(tmp_path):
    seen: list = []
    _call(
        _server(tmp_path, seen),
        "tessera_submit_campaign",
        {
            "spec": _chained(predecessor="first"),
            "idempotency_key": "k",
            "predecessors": {"first": "campaign_1"},
        },
    )
    body = json.loads(seen[0][3])
    assert body["predecessors"] == {"first": "campaign_1"}
    assert body["spec"]["stages"][0]["inputs"][0]["kind"] == "receipt_output"
    assert body["spec"]["stages"][0]["inputs"][0]["predecessor"] == "first"


def test_submit_omits_empty_predecessors(tmp_path):
    seen: list = []
    server = _server(tmp_path, seen)
    _call(server, "tessera_submit_campaign", {"spec": V2, "idempotency_key": "k"})
    _call(
        server,
        "tessera_submit_campaign",
        {"spec": V2, "idempotency_key": "k", "predecessors": {}},
    )
    assert all("predecessors" not in json.loads(item[3]) for item in seen)


def test_receipt_output_by_campaign_id_and_docs(tmp_path):
    seen: list = []
    server = _server(tmp_path, seen)
    _call(
        server,
        "tessera_submit_campaign",
        {"spec": _chained(campaign_id="campaign_1"), "idempotency_key": "k"},
    )
    assert json.loads(seen[0][3])["spec"]["stages"][0]["inputs"][0]["campaign_id"] == "campaign_1"
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert "receipt_output" in tools["tessera_submit_campaign"].description
    assert "predecessors" in tools["tessera_submit_campaign"].inputSchema["properties"]
    _call(server, "tessera_validate_campaign_spec", {"spec": _chained(predecessor="first")})
    assert seen[-1][1].endswith("/campaign-specs/validate")


def test_receipt_output_needs_exactly_one_reference(tmp_path):
    seen: list = []
    for ref in ({}, {"campaign_id": "campaign_1", "predecessor": "first"}):
        with pytest.raises(Exception):  # noqa: B017
            _call(
                _server(tmp_path, seen),
                "tessera_submit_campaign",
                {"spec": _chained(**ref), "idempotency_key": "k"},
            )
    assert seen == []
