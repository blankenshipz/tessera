"""The plain-words campaign report over the API, the CLI and the MCP tool."""

from __future__ import annotations

import asyncio
import json

import pytest
from test_artifact_disposition_authority import _other_tenant_headers
from test_campaign_api import _post, world  # noqa: F401  (the fixture)
from test_campaign_cli import FakeTransport, _reset, owner_header  # noqa: F401
from test_mcp_campaign_tools import _call, _server

from tessera_control import cli

SINCE, UNTIL = "2000-01-01T00:00:00Z", "2100-01-01T00:00:00Z"


def _approved(client, headers, project, spec):
    campaign = _post(client, f"/projects/{project}/campaigns", headers, {"spec": spec}).json()
    approve = {
        "expected_revision": campaign["revision"],
        "expected_spec_digest": campaign["spec_digest"],
    }
    _post(client, f"/campaigns/{campaign['id']}/approve", headers, approve, "a1")
    return campaign


def test_a_campaign_report_is_plain_words_without_ids_unless_asked(world):  # noqa: F811
    client, headers, project, spec, _ = world
    campaign = _approved(client, headers, project, spec)
    plain = client.get(f"/campaigns/{campaign['id']}/report", headers=headers)
    assert plain.status_code == 200, plain.text
    report = plain.json()
    assert report["schema"] == "tessera.campaign-report.v1" and report["scope"] == "campaign"
    assert report["headline"] and report["text"].startswith(report["headline"][:20])
    assert campaign["id"] not in json.dumps(report)  # no internal id in the plain report
    detailed = client.get(f"/campaigns/{campaign['id']}/report?detail=true", headers=headers).json()
    assert campaign["id"] in json.dumps(detailed)


def test_a_window_report_lists_the_campaigns_created_in_the_window(world):  # noqa: F811
    client, headers, project, spec, _ = world
    campaign = _approved(client, headers, project, spec)
    path = f"/projects/{project}/campaign-report"
    report = client.get(f"{path}?since={SINCE}&until={UNTIL}", headers=headers)
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["scope"] == "window" and campaign["id"] not in json.dumps(body)
    assert body["totals"]["campaigns"] == 1 and body["text"]
    empty = client.get(
        f"{path}?since=2000-01-01T00:00:00Z&until=2000-01-02T00:00:00Z", headers=headers
    )
    assert empty.json()["totals"]["campaigns"] == 0


def test_bad_bounds_and_missing_access_are_refused(world):  # noqa: F811
    client, headers, project, spec, state = world
    campaign = _approved(client, headers, project, spec)
    path = f"/projects/{project}/campaign-report"
    assert client.get(f"{path}?since=not-a-time&until={UNTIL}", headers=headers).status_code in (
        400,
        422,
    )
    assert client.get(f"{path}?since={SINCE}", headers=headers).status_code == 422
    assert (
        client.get(f"{path}?since={SINCE}&until={UNTIL}&limit=0", headers=headers).status_code
        == 422
    )
    assert client.get(f"/campaigns/{campaign['id']}/report").status_code == 401
    outsider = _other_tenant_headers(state)
    assert client.get(f"/campaigns/{campaign['id']}/report", headers=outsider).status_code == 404
    assert client.get(f"{path}?since={SINCE}&until={UNTIL}", headers=outsider).status_code in (
        403,
        404,
    )


# -- the CLI --------------------------------------------------------------------------------------


def _run(argv, capsys):
    arguments = cli.parser().parse_args(["campaign", *argv])
    code = cli.run_campaign(arguments, FakeTransport)
    return code, capsys.readouterr()


def test_the_cli_report_prints_the_text_and_can_print_json(owner_header, capsys):  # noqa: F811
    FakeTransport.reply = (200, {"text": "The campaign finished.", "headline": "h"})
    code, out = _run(["report", "--owner-header", str(owner_header), "--campaign-id", "c1"], capsys)
    assert code == 0 and out.out.strip() == "The campaign finished."
    assert FakeTransport.calls[-1][:2] == ("GET", "/campaigns/c1/report?detail=false")
    code, out = _run(
        ["report", "--owner-header", str(owner_header), "--campaign-id", "c1", "--detail",
         "--format", "json"], capsys,
    )  # fmt: skip
    assert FakeTransport.calls[-1][1] == "/campaigns/c1/report?detail=true"
    assert json.loads(out.out)["headline"] == "h"


def test_the_cli_window_report_and_its_argument_rules(owner_header, capsys):  # noqa: F811
    FakeTransport.reply = (200, {"text": "Window."})
    code, out = _run(
        ["report", "--owner-header", str(owner_header), "--project-id", "p1",
         "--since", SINCE, "--until", UNTIL, "--limit", "7"], capsys,
    )  # fmt: skip
    assert code == 0 and out.out.strip() == "Window."
    method, path = FakeTransport.calls[-1][:2]
    assert method == "GET" and path.startswith("/projects/p1/campaign-report?since=2000-01-01")
    assert "limit=7" in path and "detail=false" in path
    for bad in (["--project-id", "p1"], ["--campaign-id", "c1", "--since", SINCE], []):
        code, _out = _run(["report", "--owner-header", str(owner_header), *bad], capsys)
        assert code == 2


# -- the MCP tool ---------------------------------------------------------------------------------


def test_the_mcp_tool_is_read_only_and_reaches_both_routes(tmp_path):
    seen: list = []
    server = _server(tmp_path, seen)
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    tool = tools["tessera_campaign_report"]
    assert tool.annotations.readOnlyHint is True and tool.annotations.destructiveHint is False
    _call(server, "tessera_campaign_report", {"campaign_id": "cmp_1"})
    _call(server, "tessera_campaign_report", {"since": SINCE, "until": UNTIL, "detail": True})
    assert [(m, p) for m, p, _k, _c in seen][0] == ("GET", "/campaigns/cmp_1/report")
    method, path = seen[1][:2]
    assert method == "GET" and path == "/projects/proj1/campaign-report"
    for bad in ({}, {"campaign_id": "c", "since": SINCE, "until": UNTIL}, {"since": SINCE}):
        with pytest.raises(Exception):  # noqa: B017 - a refused call, never a request
            _call(server, "tessera_campaign_report", bad)
    assert len(seen) == 2
