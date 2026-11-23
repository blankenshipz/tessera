"""ADR 0124 operator loop: spec templates, rendered submit, wave receipts, gate decision tool.

The mcp extra is required: a missing one fails this module at import instead of skipping it.
"""

from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest
from campaign_fixture import Env
from mcp.server.fastmcp.exceptions import ToolError
from test_campaign_gates import finished_campaign
from test_campaign_standing import Sched

from tessera_control.api import create_app
from tessera_mcp.client import ControlClient
from tessera_mcp.config import Config
from tessera_mcp.server import CAMPAIGN_ACTIONS, CAMPAIGN_TOOL_SCOPES, create_server
from tessera_mcp.spec_template import MAX_TEMPLATE_BYTES, TemplateError, render_spec

TEMPLATE = {
    "version": 1,
    "name": "${RUN_NAME}",
    "budget_cap_cents": 2,
    "deadline_seconds": 600,
    "worker_principal_id": "worker",
    "stages": [
        {
            "key": "s1",
            "backend": "docker-local-cpu-v1",
            "image": "python@sha256:" + "a" * 64,
            "argv": ["python", "-c", "print('${RUN_NAME}')"],
            "output_paths": ["o.json"],
            "max_output_bytes": 10,
            "max_duration_seconds": 60,
            "inputs": [
                {
                    "kind": "snapshot",
                    "name": "code",
                    "snapshot_id": "${SNAP}",
                    "digest": "b" * 64,
                    "data_class": "public",
                }
            ],
        }
    ],
}
VALUES = {"RUN_NAME": "wave one", "SNAP": "snap1"}


# -- spec_template -------------------------------------------------------------------------------


def test_render_substitutes_string_leaves_from_a_dict_or_json_text():
    spec = render_spec(TEMPLATE, VALUES)
    assert spec["name"] == "wave one"
    assert spec["stages"][0]["argv"][2] == "print('wave one')"
    assert spec["stages"][0]["inputs"][0]["snapshot_id"] == "snap1"
    assert render_spec(json.dumps(TEMPLATE), VALUES) == spec
    assert TEMPLATE["name"] == "${RUN_NAME}"  # the template is not mutated
    assert spec["budget_cap_cents"] == 2  # non-strings are untouched


def test_missing_and_unused_values_are_refused():
    with pytest.raises(TemplateError, match="Missing values.*SNAP"):
        render_spec(TEMPLATE, {"RUN_NAME": "x"})
    with pytest.raises(TemplateError, match="do not use|does not use.*EXTRA"):
        render_spec(TEMPLATE, {**VALUES, "EXTRA": "x"})


@pytest.mark.parametrize(
    "value",
    ['x", "evil": "1', '"}, {"a": "', '{"a": 1}', "\\", "x\n}", "[1, 2]", "$ {RUN_NAME}"],
)
def test_a_value_cannot_add_keys_or_change_types(value):
    spec = render_spec(TEMPLATE, {**VALUES, "RUN_NAME": value})
    assert spec["name"] == value  # inert text in a string leaf
    expected = copy.deepcopy(TEMPLATE)
    expected["name"] = value
    expected["stages"][0]["argv"][2] = f"print('{value}')"
    expected["stages"][0]["inputs"][0]["snapshot_id"] = "snap1"
    assert spec == expected
    assert set(spec) == set(TEMPLATE)


def test_a_placeholder_in_an_object_key_is_refused():
    with pytest.raises(TemplateError, match="object key"):
        render_spec({"version": 1, "${KEY}": "x"}, {"KEY": "x"})
    with pytest.raises(TemplateError, match="object key"):
        render_spec({"a": [{"${K}": 1}]}, {"K": "x"})


@pytest.mark.parametrize("name", ["lower", "1BAD", "BAD-NAME", "", "A B", "A}"])
def test_a_bad_value_name_is_refused(name):
    with pytest.raises(TemplateError, match="Value name"):
        render_spec({"a": "x"}, {name: "v"})


def test_non_string_and_oversized_values_are_refused():
    for value in (1, None, ["a"], {"a": "b"}):
        with pytest.raises(TemplateError, match="must be a string"):
            render_spec({"a": "${V}"}, {"V": value})  # type: ignore[dict-item]
    with pytest.raises(TemplateError, match="at most"):
        render_spec({"a": "${V}"}, {"V": "x" * 5000})


def test_a_value_that_contains_a_placeholder_is_refused():
    with pytest.raises(TemplateError, match="new placeholder"):
        render_spec({"a": "${V}", "b": "${W}"}, {"V": "${W}", "W": "ok"})
    with pytest.raises(TemplateError, match="new placeholder"):
        render_spec({"a": "${V}"}, {"V": "${NOT_GIVEN}"})


def test_bad_or_oversized_templates_are_refused():
    with pytest.raises(TemplateError, match="not valid JSON"):
        render_spec("{not json", {})
    with pytest.raises(TemplateError, match="JSON object"):
        render_spec("[1]", {})
    with pytest.raises(TemplateError, match="too large"):
        render_spec('{"a": "' + "x" * MAX_TEMPLATE_BYTES + '"}', {})
    with pytest.raises(TemplateError, match="Too many"):
        render_spec({"a": "x"}, {f"V{i}": "x" for i in range(257)})


# -- tools over a mock transport -----------------------------------------------------------------


def _server(tmp_path, handler, *, actions=None, approval_tool=True):
    reference = tmp_path / "tok"
    reference.write_text("synthetic-token")
    reference.chmod(0o600)
    client = ControlClient(
        Config("http://127.0.0.1:8765", reference, "proj1", "dest"), httpx.MockTransport(handler)
    )
    return create_server(
        client, campaign_tools=True, approval_tool=approval_tool, campaign_actions=actions
    )


def _call(server, name, arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    return result[1] if isinstance(result, tuple) else result


def _names(server):
    return {t.name for t in asyncio.run(server.list_tools())}


def _recorder(seen, answers=None):
    def handler(request):
        body = json.loads(request.content) if request.content else None
        seen.append(
            (request.method, request.url.path, request.headers.get("idempotency-key"), body)
        )
        if answers and (request.method, request.url.path) in answers:
            return answers[(request.method, request.url.path)](request)
        return httpx.Response(200, json={"ok": True})

    return handler


def test_render_sends_only_a_validate_request_and_returns_spec_and_validation(tmp_path):
    seen: list = []
    server = _server(tmp_path, _recorder(seen))
    result = _call(server, "tessera_render_campaign_spec", {"template": TEMPLATE, "values": VALUES})
    assert result["validation"] == {"ok": True}
    assert result["spec"]["name"] == "wave one"
    assert [(m, p, k) for m, p, k, _ in seen] == [
        ("POST", "/projects/proj1/campaign-specs/validate", None)
    ]
    assert seen[0][3]["spec"] == result["spec"]


def test_a_refused_render_sends_no_request_at_all(tmp_path):
    seen: list = []
    server = _server(tmp_path, _recorder(seen))
    bad_values = [{"RUN_NAME": "x"}, {**VALUES, "EXTRA": "x"}, {**VALUES, "RUN_NAME": "${SNAP}"}]
    for tool in ("tessera_render_campaign_spec", "tessera_submit_rendered_campaign"):
        for values in bad_values:
            arguments = {"template": TEMPLATE, "values": values}
            if tool.startswith("tessera_submit"):
                arguments["idempotency_key"] = "k"
            with pytest.raises(ToolError):
                _call(server, tool, arguments)
    # Rendered, but not a valid spec: also refused locally.
    with pytest.raises(ToolError):
        _call(
            server,
            "tessera_render_campaign_spec",
            {"template": {**TEMPLATE, "version": "${V}"}, "values": {**VALUES, "V": "9"}},
        )
    assert seen == []


def test_submit_rendered_posts_the_rendered_spec_with_the_key(tmp_path):
    seen: list = []
    server = _server(tmp_path, _recorder(seen))
    _call(
        server,
        "tessera_submit_rendered_campaign",
        {"template": json.dumps(TEMPLATE), "values": VALUES, "idempotency_key": "rk-1"},
    )
    ((method, path, key, body),) = seen
    assert (method, path, key) == ("POST", "/projects/proj1/campaigns", "rk-1")
    assert body["spec"]["name"] == "wave one"
    assert body["spec"]["stages"][0]["inputs"][0]["snapshot_id"] == "snap1"


def test_registration_follows_the_enrolled_scopes(tmp_path):
    handler = _recorder([])
    submit_only = _names(_server(tmp_path, handler, actions=frozenset({"campaign:submit"})))
    read_only = _names(_server(tmp_path, handler, actions=frozenset({"campaign:read"})))
    submit_tools = {"tessera_render_campaign_spec", "tessera_submit_rendered_campaign"}
    assert submit_tools <= submit_only and "tessera_campaign_wave_receipts" not in submit_only
    assert "tessera_campaign_wave_receipts" in read_only and not submit_tools & read_only
    for name in (*submit_tools, "tessera_campaign_wave_receipts"):
        assert CAMPAIGN_TOOL_SCOPES[name] in CAMPAIGN_ACTIONS


def _receipts_handler(seen):
    def campaign(request):
        cid = request.url.path.split("/")[2]
        if cid == "gone":
            return httpx.Response(404, json={"detail": "secret-detail"})
        return httpx.Response(
            200, json={"id": cid, "status": "running" if cid == "r" else "succeeded"}
        )

    def one(request):
        cid = request.url.path.split("/")[2]
        return httpx.Response(200, json={"campaign_id": cid})

    class Routes(dict):
        def __bool__(self):
            return True

        def __contains__(self, key):
            return True

        def __getitem__(self, key):
            method, path = key
            if path.endswith("/receipt"):
                return one
            if path.endswith("/gate-decisions"):
                return lambda request: httpx.Response(
                    200, json={"decisions": [], "effective": None}
                )
            return campaign

    return _recorder(seen, Routes())


def test_wave_receipts_report_terminal_running_and_failing_campaigns(tmp_path):
    seen: list = []
    server = _server(tmp_path, _receipts_handler(seen))
    result = _call(server, "tessera_campaign_wave_receipts", {"campaign_ids": ["t", "r", "gone"]})
    done, running, failed = result["waves"]
    assert done["status"] == "succeeded" and done["receipt"] == {"campaign_id": "t"}
    assert done["gate_decisions"] == {"decisions": [], "effective": None}
    assert running["status"] == "running" and running["receipt"] is None
    assert running["receipt_absent_reason"] == "campaign_not_terminal"
    assert "error" in failed and "receipt" not in failed and "secret-detail" not in failed["error"]
    assert ("GET", "/campaigns/r/receipt") not in [(m, p) for m, p, _, _ in seen]
    assert all(m == "GET" for m, _, _, _ in seen)


def _wave_answers(views, receipt_status=None, gate_status=200):
    def campaign(request):
        return httpx.Response(200, json=views[request.url.path.split("/")[2]])

    def receipt(request):
        cid = request.url.path.split("/")[2]
        code = (receipt_status or {}).get(cid, 200)
        return httpx.Response(code, json={"campaign_id": cid} if code == 200 else {"d": "x"})

    def gates(request):
        if gate_status != 200:
            return httpx.Response(gate_status, json={"d": "secret-detail"})
        return httpx.Response(200, json={"decisions": [], "effective": None})

    class Routes(dict):
        def __bool__(self):
            return True

        def __contains__(self, key):
            return True

        def __getitem__(self, key):
            path = key[1]
            if path.endswith("/receipt"):
                return receipt
            return gates if path.endswith("/gate-decisions") else campaign

    return Routes()


CANCELLED = {"id": "c", "status": "cancelled", "approval": None, "approvals": []}
FINISHED = {"id": "f", "status": "succeeded", "approval": {"status": "active"}}


def test_wave_receipts_cancelled_before_approval_has_null_receipt_and_reason(tmp_path):
    seen: list = []
    answers = _wave_answers({"c": CANCELLED, "f": FINISHED}, {"c": 404})
    server = _server(tmp_path, _recorder(seen, answers))
    cancelled, finished = _call(
        server, "tessera_campaign_wave_receipts", {"campaign_ids": ["c", "f"]}
    )["waves"]
    assert cancelled["status"] == "cancelled" and cancelled["receipt"] is None
    assert cancelled["receipt_absent_reason"] == "no_receipt_recorded"
    assert "error" not in cancelled
    assert cancelled["gate_decisions"] == {"decisions": [], "effective": None}
    assert finished["receipt"] == {"campaign_id": "f"} and "error" not in finished
    assert "receipt_absent_reason" not in finished


@pytest.mark.parametrize("code", [403, 500])
def test_wave_receipts_non_404_receipt_error_stays_an_error(tmp_path, code):
    answers = _wave_answers({"c": CANCELLED}, {"c": code})
    server = _server(tmp_path, _recorder([], answers))
    (entry,) = _call(server, "tessera_campaign_wave_receipts", {"campaign_ids": ["c"]})["waves"]
    assert f"HTTP {code}" in entry["error"] and "receipt_absent_reason" not in entry
    assert entry["gate_decisions"] == {"decisions": [], "effective": None}


def test_wave_receipts_404_for_an_approved_campaign_stays_an_error(tmp_path):
    view = {**CANCELLED, "approval": None, "approvals": [{"status": "revoked"}]}
    answers = _wave_answers({"c": view}, {"c": 404})
    server = _server(tmp_path, _recorder([], answers))
    (entry,) = _call(server, "tessera_campaign_wave_receipts", {"campaign_ids": ["c"]})["waves"]
    assert "HTTP 404" in entry["error"] and "receipt_absent_reason" not in entry


def test_wave_receipts_gate_read_failure_does_not_hide_the_receipt(tmp_path):
    answers = _wave_answers({"f": FINISHED}, gate_status=500)
    server = _server(tmp_path, _recorder([], answers))
    (entry,) = _call(server, "tessera_campaign_wave_receipts", {"campaign_ids": ["f"]})["waves"]
    assert entry["receipt"] == {"campaign_id": "f"} and "error" not in entry
    assert "HTTP 500" in entry["gate_decisions_error"] and "secret-detail" not in str(entry)
    assert "gate_decisions" not in entry


@pytest.mark.parametrize("ids", [[], ["a", "a"], [f"c{i}" for i in range(21)]])
def test_wave_receipts_refuse_bad_id_lists_without_a_request(tmp_path, ids):
    seen: list = []
    server = _server(tmp_path, _recorder(seen))
    with pytest.raises(ToolError, match="1-20 distinct"):
        _call(server, "tessera_campaign_wave_receipts", {"campaign_ids": ids})
    assert seen == []


def test_the_gate_decision_tool_is_approval_only(tmp_path):
    handler = _recorder([])
    assert "tessera_record_gate_decision" not in CAMPAIGN_TOOL_SCOPES
    assert "tessera_record_gate_decision" not in _names(
        _server(tmp_path, handler, approval_tool=False)
    )
    assert "tessera_record_gate_decision" in _names(_server(tmp_path, handler))


def test_gate_decision_reads_the_revision_then_posts_decision_note_and_key(tmp_path):
    seen: list = []
    server = _server(
        tmp_path,
        _recorder(
            seen, {("GET", "/campaigns/c1"): lambda r: httpx.Response(200, json={"revision": 4})}
        ),
    )
    arguments = {"campaign_id": "c1", "decision": "go", "note": "looks right"}
    _call(server, "tessera_record_gate_decision", arguments)
    _call(server, "tessera_record_gate_decision", {**arguments, "decision": "no_go"})
    _call(
        server,
        "tessera_record_gate_decision",
        {**arguments, "idempotency_key": "mine", "expected_revision": 9},
    )
    assert seen[0][:2] == ("GET", "/campaigns/c1")
    posts = [row for row in seen if row[0] == "POST"]
    assert {p for _, p, _, _ in posts} == {"/campaigns/c1/gate-decision"}
    assert posts[0][3] == {"expected_revision": 4, "decision": "go", "note": "looks right"}
    assert posts[1][3]["decision"] == "no_go"
    assert posts[0][2].startswith("gate-") and posts[0][2] != posts[1][2]
    assert posts[2][2] == "mine" and posts[2][3]["expected_revision"] == 9


@pytest.mark.parametrize("decision", ["GO", "approve", "", "nogo", "go "])
def test_a_bad_gate_decision_sends_nothing(tmp_path, decision):
    seen: list = []
    server = _server(tmp_path, _recorder(seen))
    with pytest.raises(ToolError, match="nothing was sent"):
        _call(
            server,
            "tessera_record_gate_decision",
            {"campaign_id": "c1", "decision": decision, "note": "n"},
        )
    assert seen == []


def test_an_http_403_surfaces_an_error_without_relaying_the_body(tmp_path):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"revision": 1})
        return httpx.Response(403, json={"detail": "leaky-internal-detail"})

    server = _server(tmp_path, handler)
    with pytest.raises(ToolError, match="HTTP 403") as caught:
        _call(
            server,
            "tessera_record_gate_decision",
            {"campaign_id": "c1", "decision": "go", "note": "n"},
        )
    assert "leaky-internal-detail" not in str(caught.value)


# -- the real control app: the platform decides who may record a gate decision -------------------


def test_real_app_refuses_an_agent_and_records_a_persons_gate_decision(tmp_path):
    sched = Sched(tmp_path)
    env: Env = sched.env
    campaign_id = finished_campaign(sched)
    env.agent(sorted(CAMPAIGN_ACTIONS), "steerer")
    env.human("approver")
    app = create_app(env.state)

    def call(who, **extra):
        reference = tmp_path / f"{who}.token"
        reference.write_text(f"synthetic-{who}-token-" + "x" * 30)
        reference.chmod(0o600)
        client = ControlClient(
            Config("http://127.0.0.1:8765", reference, env.project["id"], "planner.local"),
            httpx.ASGITransport(app=app),
        )
        server = create_server(client, campaign_tools=True, approval_tool=True)
        arguments = {"campaign_id": campaign_id, "decision": "go", "note": "reviewed", **extra}
        return _call(server, "tessera_record_gate_decision", arguments)

    count = "SELECT count(*) AS n FROM campaign_gate_decisions"
    with pytest.raises(ToolError, match="HTTP 403"):
        call("steerer")
    assert env.query(count)[0]["n"] == 0
    call("approver", idempotency_key="gate-real-1")
    assert env.query(count)[0]["n"] == 1
    call(
        "approver", idempotency_key="gate-real-1"
    )  # same key and revision: a replay, not a new row
    assert env.query(count)[0]["n"] == 1
