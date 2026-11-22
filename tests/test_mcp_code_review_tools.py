"""Code-review jobs through the existing review MCP tools (no parallel tools)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from tessera_mcp.client import ControlClient
from tessera_mcp.config import Config
from tessera_mcp.server import create_server

HEAD, BASE, RUBRIC = "a" * 64, "b" * 64, "c" * 64
AGENT_TOOLS = {
    "tessera_submit_review_job",
    "tessera_list_review_jobs",
    "tessera_read_review_job",
    "tessera_list_review_findings",
    "tessera_read_review_finding",
}
DECISION_TOOLS = {"tessera_resolve_review_finding", "tessera_approve_review_job_tier"}


def _input(digest):
    return {"snapshot_id": "s-" + digest[:4], "digest": digest}


def _payload(subject, digests=(HEAD, BASE, RUBRIC), **over):
    body = {
        "kind": "code_review",
        "title": "review",
        "inputs": [_input(d) for d in digests],
        "prompt_digest": RUBRIC,
        "destination_id": "dest1",
        "tier": "sonnet",
        "budget_cents": 50,
        "code_review": subject,
    }
    body.update(over)
    return body


DIFF = {"mode": "diff", "head_digest": HEAD, "base_digest": BASE, "rubric_digest": RUBRIC}
SNAPSHOT = {"mode": "snapshot", "head_digest": HEAD, "rubric_digest": RUBRIC}


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


@pytest.mark.parametrize(
    ("subject", "digests", "expected"),
    [
        (DIFF, (HEAD, BASE, RUBRIC), DIFF),
        (SNAPSHOT, (HEAD, RUBRIC), SNAPSHOT),
    ],
)
def test_code_review_submit_sends_the_exact_body(tmp_path, subject, digests, expected):
    seen: list = []

    def handler(request):
        seen.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(201, json={"state": "queued"})

    server = _server(tmp_path, handler, review_tools=True)
    payload = _payload(subject, digests)
    _call(server, "tessera_submit_review_job", {"payload": payload, "idempotency_key": "k"})
    method, path, body = seen[0]
    assert (method, path) == ("POST", "/projects/proj1/review-jobs")
    assert body == {**payload, "egress": "none", "code_review": expected}


def test_inputs_carry_no_client_data_class(tmp_path):
    def handler(request):
        pytest.fail("nothing may be sent")

    server = _server(tmp_path, handler, review_tools=True)
    payload = _payload(DIFF)
    payload["inputs"] = [{**item, "data_class": "public"} for item in payload["inputs"]]
    with pytest.raises(ToolError):
        _call(server, "tessera_submit_review_job", {"payload": payload, "idempotency_key": "k"})


def test_invalid_subjects_send_nothing(tmp_path):
    def handler(request):
        pytest.fail("nothing may be sent")

    server = _server(tmp_path, handler, review_tools=True)
    other = "d" * 64
    bad = [
        _payload(None),  # subject required for code_review
        {k: v for k, v in _payload(DIFF).items() if k != "code_review"},
        _payload({**DIFF, "base_digest": None}),  # diff needs a base
        _payload({"mode": "diff", "head_digest": HEAD, "rubric_digest": RUBRIC}),
        _payload({**SNAPSHOT, "base_digest": BASE}),  # snapshot forbids a base
        _payload({**DIFF, "base_digest": HEAD}),  # base must differ from head
        _payload(DIFF, (HEAD, RUBRIC)),  # base not among inputs
        _payload(DIFF, (BASE, RUBRIC)),  # head not among inputs
        _payload(SNAPSHOT, (HEAD,)),  # rubric not among inputs
        _payload({**SNAPSHOT, "rubric_digest": other}, (HEAD, RUBRIC)),
        _payload({**DIFF, "head_digest": "XYZ"}),
        _payload({**DIFF, "mode": "patch"}),
        _payload({**DIFF, "extra": 1}),
        _payload(DIFF, kind="plan_review"),  # subject forbidden for other kinds
        _payload(DIFF, kind="pre_gate_review"),
        _payload(DIFF, kind="code_audit"),
    ]
    for payload in bad:
        with pytest.raises(ToolError):
            _call(server, "tessera_submit_review_job", {"payload": payload, "idempotency_key": "k"})


def test_list_kind_filter_builds_the_path_and_rejects_unknown(tmp_path):
    seen: list = []

    def handler(request):
        seen.append(request.url.raw_path.decode())
        return httpx.Response(200, json={})

    server = _server(tmp_path, handler, review_tools=True)
    _call(server, "tessera_list_review_jobs", {"offset": 1, "limit": 5, "kind": "code_review"})
    _call(server, "tessera_list_review_jobs", {})
    assert seen == [
        "/projects/proj1/review-jobs?offset=1&limit=5&kind=code_review",
        "/projects/proj1/review-jobs?offset=0&limit=20",
    ]
    for bad in ("code_review&x=1", "nope", "", "../x"):
        with pytest.raises(ToolError):
            _call(server, "tessera_list_review_jobs", {"kind": bad})
    assert len(seen) == 2


def test_tool_names_are_unchanged_and_decisions_stay_gated(tmp_path):
    agent = _names(_server(tmp_path, _ok, review_tools=True))
    assert {n for n in agent if "review_job" in n or "review_finding" in n} == AGENT_TOOLS
    both = _names(_server(tmp_path, _ok, review_tools=True, review_decision_tool=True))
    assert {
        n for n in both if "review_job" in n or "review_finding" in n
    } == AGENT_TOOLS | DECISION_TOOLS
    assert not (agent & DECISION_TOOLS)


def test_descriptions_state_findings_are_numbered_and_verdict_advisory(tmp_path):
    tools = {
        t.name: t.description
        for t in asyncio.run(
            _server(tmp_path, _ok, review_tools=True, review_decision_tool=True).list_tools()
        )
    }
    for name in (
        "tessera_read_review_job",
        "tessera_list_review_findings",
        "tessera_read_review_finding",
        "tessera_resolve_review_finding",
    ):
        text = tools[name].lower()
        assert "numbered" in text and "advisory" in text, name
    assert "CHANGES-REQUIRED" in tools["tessera_read_review_job"]
    assert "receipt" in tools["tessera_read_review_job"]
