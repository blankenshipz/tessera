"""Review-job MCP tools (ADR 0084): thin clients of the control API, no bypass."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from tessera_mcp.client import ControlClient
from tessera_mcp.config import Config
from tessera_mcp.harness_binding import PERMITTED_TOOLS
from tessera_mcp.server import create_server

D = "a" * 64
AGENT_TOOLS = {
    "tessera_submit_review_job",
    "tessera_list_review_jobs",
    "tessera_read_review_job",
    "tessera_list_review_findings",
    "tessera_read_review_finding",
}
DECISION_TOOLS = {"tessera_resolve_review_finding", "tessera_approve_review_job_tier"}
PAYLOAD = {
    "kind": "plan_review",
    "title": "t",
    "inputs": [{"snapshot_id": "s1", "digest": D}],
    "prompt_digest": D,
    "destination_id": "dest1",
    "tier": "sonnet",
    "budget_cents": 50,
}


def _server(tmp_path, handler, **flags):
    reference = tmp_path / "tok"
    reference.write_text("synthetic-token")
    reference.chmod(0o600)
    client = ControlClient(
        Config("http://127.0.0.1:8765", reference, "proj1", "dest"), httpx.MockTransport(handler)
    )
    return create_server(client, **flags)


def _call(server, name, arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    return result[1] if isinstance(result, tuple) else result


def _names(server):
    return {t.name for t in asyncio.run(server.list_tools())}


def _ok(request):
    return httpx.Response(200, json={})


def test_registration_follows_the_flags(tmp_path):
    base = _names(_server(tmp_path, _ok))
    assert not (base & (AGENT_TOOLS | DECISION_TOOLS))
    agent = _names(_server(tmp_path, _ok, review_tools=True))
    assert AGENT_TOOLS <= agent and not (agent & DECISION_TOOLS)
    both = _names(_server(tmp_path, _ok, review_tools=True, review_decision_tool=True))
    assert (AGENT_TOOLS | DECISION_TOOLS) <= both
    solo = _names(_server(tmp_path, _ok, review_decision_tool=True))
    assert DECISION_TOOLS <= solo and not (solo & AGENT_TOOLS)


def test_harness_catalog_is_unchanged():
    assert not ((AGENT_TOOLS | DECISION_TOOLS) & set(PERMITTED_TOOLS))


def test_untrusted_text_is_marked_in_descriptions(tmp_path):
    tools = {
        t.name: t.description
        for t in asyncio.run(_server(tmp_path, _ok, review_tools=True).list_tools())
    }
    assert "UNTRUSTED" in tools["tessera_read_review_finding"]
    assert "UNTRUSTED" in tools["tessera_list_review_findings"]


def test_submit_sends_path_key_and_body(tmp_path):
    seen: list = []

    def handler(request):
        seen.append(
            (
                request.method,
                request.url.path,
                request.headers.get("idempotency-key"),
                json.loads(request.content),
            )
        )
        return httpx.Response(201, json={"state": "queued"})

    server = _server(tmp_path, handler, review_tools=True)
    result = _call(
        server, "tessera_submit_review_job", {"payload": PAYLOAD, "idempotency_key": "k1"}
    )
    assert result == {"state": "queued"}
    method, path, key, body = seen[0]
    assert (method, path, key) == ("POST", "/projects/proj1/review-jobs", "k1")
    assert body == {**PAYLOAD, "egress": "none"}


def test_submit_validates_before_sending(tmp_path):
    def handler(request):
        pytest.fail("nothing may be sent")

    server = _server(tmp_path, handler, review_tools=True)
    for bad in (
        {**PAYLOAD, "tier": "gpt"},
        {**PAYLOAD, "inputs": []},
        {**PAYLOAD, "budget_cents": 10001},
        {**PAYLOAD, "egress": "public"},
        {**PAYLOAD, "extra": 1},
        {**PAYLOAD, "inputs": [{"snapshot_id": "s1", "digest": D, "data_class": "public"}]},
        {**PAYLOAD, "inputs": [{"snapshot_id": "s1", "digest": D, "data_class": "private_raw"}]},
    ):
        with pytest.raises(ToolError):
            _call(server, "tessera_submit_review_job", {"payload": bad, "idempotency_key": "k"})


def test_list_and_read_paths(tmp_path):
    seen: list = []

    def handler(request):
        seen.append(request.url.raw_path.decode())
        return httpx.Response(200, json={})

    server = _server(tmp_path, handler, review_tools=True)
    _call(server, "tessera_list_review_jobs", {"offset": 2, "limit": 5})
    _call(server, "tessera_read_review_job", {"job_id": "job_1"})
    _call(server, "tessera_list_review_findings", {"job_id": "job_1", "state": "open"})
    _call(server, "tessera_list_review_findings", {})
    _call(server, "tessera_read_review_finding", {"finding_id": "f_1"})
    assert seen == [
        "/projects/proj1/review-jobs?offset=2&limit=5",
        "/review-jobs/job_1",
        "/projects/proj1/review-findings?offset=0&limit=20&job_id=job_1&state=open",
        "/projects/proj1/review-findings?offset=0&limit=20",
        "/review-findings/f_1",
    ]


def test_resolve_reads_the_finding_first_then_posts(tmp_path):
    seen: list = []

    def handler(request):
        seen.append((request.method, request.url.path, request.headers.get("idempotency-key")))
        if request.method == "GET":
            return httpx.Response(200, json={"digest": D, "revision": 3})
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"advisory": True, "execution_authorized": False})

    server = _server(tmp_path, handler, review_decision_tool=True)
    args = {
        "finding_id": "f_1",
        "decision": "accept",
        "rationale": "why",
        "expected_revision": 3,
        "expected_digest": D,
    }
    assert _call(server, "tessera_resolve_review_finding", args)["advisory"] is True
    _call(server, "tessera_resolve_review_finding", {**args, "idempotency_key": "mine"})
    assert seen[0] == ("GET", "/review-findings/f_1", None)
    assert seen[1][:2] == ("POST", "/review-findings/f_1/resolve")
    assert seen[1][2].startswith("resolve-")
    assert seen[2] == {
        "decision": "accept",
        "rationale": "why",
        "expected_revision": 3,
        "expected_digest": D,
    }
    assert seen[4][2] == "mine"


def test_resolve_refuses_a_digest_mismatch_without_sending(tmp_path):
    methods: list = []

    def handler(request):
        methods.append(request.method)
        return httpx.Response(200, json={"digest": "b" * 64})

    server = _server(tmp_path, handler, review_decision_tool=True)
    with pytest.raises(ToolError, match="nothing was sent"):
        _call(
            server,
            "tessera_resolve_review_finding",
            {
                "finding_id": "f_1",
                "decision": "reject",
                "rationale": "r",
                "expected_revision": 1,
                "expected_digest": D,
            },
        )
    assert methods == ["GET"]


def test_a_403_passes_through_for_agent_credentials(tmp_path):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"digest": D})
        return httpx.Response(403, json={"detail": "human only"})

    server = _server(tmp_path, handler, review_decision_tool=True)
    with pytest.raises(ToolError, match="HTTP 403"):
        _call(
            server,
            "tessera_resolve_review_finding",
            {
                "finding_id": "f_1",
                "decision": "accept",
                "rationale": "r",
                "expected_revision": 1,
                "expected_digest": D,
            },
        )
    with pytest.raises(ToolError, match="HTTP 403"):
        _call(
            server,
            "tessera_approve_review_job_tier",
            {"job_id": "j", "expected_revision": 1, "rationale": "r"},
        )


def test_approve_tier_path_and_body(tmp_path):
    seen: list = []

    def handler(request):
        seen.append(
            (request.url.path, request.headers["idempotency-key"], json.loads(request.content))
        )
        return httpx.Response(200, json={"state": "queued"})

    server = _server(tmp_path, handler, review_decision_tool=True)
    _call(
        server,
        "tessera_approve_review_job_tier",
        {"job_id": "j1", "expected_revision": 2, "rationale": "ok", "idempotency_key": "k"},
    )
    assert seen == [
        ("/review-jobs/j1/approve-tier", "k", {"expected_revision": 2, "rationale": "ok"})
    ]


def _cli(tmp_path, monkeypatch, live):
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

    monkeypatch.setattr(module, "create_server", lambda client, **kw: built.append(kw) or Stub())
    monkeypatch.setattr(module, "_has_provider_trust_scope", lambda client: False)
    monkeypatch.setattr(module, "_live_actions", live)
    return module, built


def test_cli_flag_wiring(tmp_path, monkeypatch):
    module, built = _cli(tmp_path, monkeypatch, lambda client: pytest.fail("no live read"))
    assert module.main(["--review-decision-tool"]) == 0
    assert built == [{"campaign_tools": False, "review_decision_tool": True}]
    built.clear()
    module, built = _cli(tmp_path, monkeypatch, lambda client: frozenset({"review:read"}))
    assert module.main([]) == 0
    assert built[0]["review_tools"] is True and "review_decision_tool" not in built[0]
    built.clear()
    module, built = _cli(tmp_path, monkeypatch, lambda client: frozenset({"context:read"}))
    assert module.main([]) == 0
    assert built == [{"campaign_tools": False}]


def test_each_tool_follows_its_own_scope(tmp_path):
    reviewer = _names(
        _server(
            tmp_path,
            _ok,
            review_tools=True,
            review_actions=frozenset({"review:read", "review:findings"}),
        )
    )
    assert AGENT_TOOLS - {"tessera_submit_review_job"} <= reviewer
    assert "tessera_submit_review_job" not in reviewer
    submitter = _names(
        _server(tmp_path, _ok, review_tools=True, review_actions=frozenset({"review:submit"}))
    )
    assert submitter & AGENT_TOOLS == {"tessera_submit_review_job"}
    assert (
        _names(_server(tmp_path, _ok, review_tools=True, review_actions=frozenset()))
        & (AGENT_TOOLS)
        == set()
    )


def test_findings_state_is_a_closed_set(tmp_path):
    seen: list = []

    def handler(request):
        seen.append(request.url.params.get("state"))
        return httpx.Response(200, json={})

    server = _server(tmp_path, handler, review_tools=True)
    for ok in ("open", "accepted", "rejected"):
        _call(server, "tessera_list_review_findings", {"state": ok})
    assert seen == ["open", "accepted", "rejected"]
    with pytest.raises(ToolError):
        _call(server, "tessera_list_review_findings", {"state": "bogus"})
    assert len(seen) == 3


def test_cli_explicit_campaign_flag_still_registers_review(tmp_path, monkeypatch):
    live = frozenset({"review:read", "context:read"})
    for flag in ("--campaign-tools", "--no-campaign-tools"):
        module, built = _cli(tmp_path, monkeypatch, lambda client: live)
        assert module.main([flag]) == 0
        assert built[0]["review_tools"] is True
        assert built[0]["review_actions"] == frozenset({"review:read"})
    module, built = _cli(tmp_path, monkeypatch, lambda client: pytest.fail("no live read"))
    assert module.main(["--approval-tool"]) == 0
    assert "review_tools" not in built[0]
