"""tessera_submit_code_review and the tessera-review CLI (ADR 0108): validation sends nothing."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from tessera_mcp import review_cli
from tessera_mcp.client import ControlClient
from tessera_mcp.config import Config
from tessera_mcp.review_tools import REVIEW_TOOL_SCOPES, build_code_review_body
from tessera_mcp.server import create_server

SHA = "a" * 40
TOOL = "tessera_submit_code_review"
ARGS = {
    "repo": "app",
    "branch": "feature/x",
    "head_sha": SHA,
    "tier": "sonnet",
    "budget_cents": 50,
    "focused_tests": ["tests/test_a.py"],
}


def _client(tmp_path, handler):
    reference = tmp_path / "tok"
    reference.write_text("synthetic-token")
    reference.chmod(0o600)
    return ControlClient(
        Config("http://127.0.0.1:8765", reference, "proj1", "dest"), httpx.MockTransport(handler)
    )


def _server(tmp_path, handler, **flags):
    return create_server(_client(tmp_path, handler), **flags)


def _call(server, arguments):
    result = asyncio.run(server.call_tool(TOOL, arguments))
    return result[1] if isinstance(result, tuple) else result


def _names(server):
    return {t.name for t in asyncio.run(server.list_tools())}


def _no_send(request):
    pytest.fail("nothing may be sent")


def test_scope_mapping():
    assert REVIEW_TOOL_SCOPES[TOOL] == "review:submit"


def test_sends_the_exact_body(tmp_path):
    seen: list = []

    def handler(request):
        seen.append(
            (
                request.method,
                request.url.path,
                json.loads(request.content),
                request.headers["Idempotency-Key"],
            )
        )
        return httpx.Response(201, json={"id": "j1"})

    server = _server(tmp_path, handler, review_tools=True)
    _call(server, {**ARGS, "idempotency_key": "k1"})
    assert seen == [
        (
            "POST",
            "/projects/proj1/code-reviews",
            {
                "repo": "app",
                "branch": "feature/x",
                "head_sha": SHA,
                "base_branch": "main",
                "tier": "sonnet",
                "destination_id": "anthropic",
                "budget_cents": 50,
                "focused_tests": ["tests/test_a.py"],
            },
            "k1",
        )
    ]


def test_rereview_body_carries_round_fields(tmp_path):
    seen: list = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(201, json={})

    server = _server(tmp_path, handler, review_tools=True)
    _call(server, {**ARGS, "previous_job_id": "j0", "recheck_findings": [2, 1]})
    assert seen[0]["previous_job_id"] == "j0" and seen[0]["recheck_findings"] == [2, 1]


BAD = [
    {"focused_tests": []},
    {"focused_tests": [f"tests/test_{i}.py" for i in range(21)]},
    {"focused_tests": ["tests/test_a.py", "tests/test_a.py"]},
    {"focused_tests": ["tests/"]},
    {"focused_tests": ["tests"]},
    {"focused_tests": ["/tests/test_a.py"]},
    {"focused_tests": ["tests/../tests/test_a.py"]},
    {"focused_tests": ["tests/test_*.py"]},
    {"focused_tests": ["tests/test_?.py"]},
    {"focused_tests": ["tests/test_[a].py"]},
    {"focused_tests": ["tests/helper.py"]},
    {"focused_tests": ["src/test_a.py"]},
    {"focused_tests": ["tests\\test_a.py"]},
    {"focused_tests": ["tests/test_a.txt"]},
    {"focused_tests": ["tests//test_a.py"]},
    {"head_sha": "A" * 40},
    {"head_sha": "a" * 39},
    {"head_sha": "main"},
    {"branch": "../x"},
    {"branch": "-x"},
    {"branch": "a..b"},
    {"branch": "a b"},
    {"branch": "a/"},
    {"base_branch": "x@{1}"},
    {"repo": "a/b"},
    {"repo": ""},
    {"tier": "gpt"},
    {"budget_cents": -1},
    {"budget_cents": 10001},
    {"recheck_findings": [1]},  # without previous_job_id
    {"previous_job_id": "j0"},  # missing recheck list
    {"previous_job_id": "j0", "recheck_findings": []},
    {"previous_job_id": "j0", "recheck_findings": [1, 1]},
    {"previous_job_id": "j0", "recheck_findings": [0]},
    {"previous_job_id": "j0", "recheck_findings": [-3]},
]


@pytest.mark.parametrize("override", BAD)
def test_validation_refusals_send_nothing(tmp_path, override):
    server = _server(tmp_path, _no_send, review_tools=True)
    with pytest.raises(ToolError):
        _call(server, {**ARGS, **override})
    with pytest.raises(ValueError):
        build_code_review_body(**{**ARGS, **override, "head_sha": {**ARGS, **override}["head_sha"]})


def test_registered_only_with_submit_scope(tmp_path):
    def names(actions):
        return _names(_server(tmp_path, _no_send, review_tools=True, review_actions=actions))

    assert TOOL in names(frozenset({"review:submit"}))
    assert TOOL not in names(frozenset({"review:read", "review:findings"}))
    assert TOOL in names(None)
    assert TOOL not in _names(_server(tmp_path, _no_send))


class _Fake:
    def __init__(self, tmp_path, response, fail=False):
        self.calls = []

        def handler(request):
            self.calls.append((request.url.path, json.loads(request.content)))
            if fail:
                return httpx.Response(409)
            return httpx.Response(201, json=response)

        self.client = _client(tmp_path, handler)


def test_cli_prints_job_round_state(tmp_path, capsys):
    fake = _Fake(
        tmp_path,
        {"id": "j9", "state": "queued", "code_review": {"source": {"round": 2}}},
    )
    code = review_cli.main(
        [
            "submit", "--repo", "app", "--branch", "feature/x", "--head", SHA,
            "--tier", "sonnet", "--budget-cents", "50",
            "--test", "tests/test_a.py", "--test", "tests/sub/test_b.py",
            "--previous", "j0", "--recheck", "3", "--recheck", "1",
        ],
        client_factory=lambda: fake.client,
    )  # fmt: skip
    out = capsys.readouterr()
    assert code == 0
    assert json.loads(out.out) == {"job_id": "j9", "round": 2, "state": "queued"}
    path, body = fake.calls[0]
    assert path == "/projects/proj1/code-reviews"
    assert body["focused_tests"] == ["tests/test_a.py", "tests/sub/test_b.py"]
    assert body["recheck_findings"] == [3, 1] and body["previous_job_id"] == "j0"
    assert "synthetic-token" not in out.out + out.err


def test_cli_validation_exits_2_and_sends_nothing(tmp_path, capsys):
    fake = _Fake(tmp_path, {})
    code = review_cli.main(
        [
            "submit", "--repo", "app", "--branch", "b", "--head", SHA, "--tier", "sonnet",
            "--budget-cents", "5", "--test", "tests/",
        ],
        client_factory=lambda: fake.client,
    )  # fmt: skip
    assert code == 2 and fake.calls == []
    assert "synthetic-token" not in capsys.readouterr().err


def test_cli_missing_test_exits_2(tmp_path):
    fake = _Fake(tmp_path, {})
    code = review_cli.main(
        ["submit", "--repo", "app", "--branch", "b", "--head", SHA, "--tier", "sonnet",
         "--budget-cents", "5"],
        client_factory=lambda: fake.client,
    )  # fmt: skip
    assert code == 2 and fake.calls == []


def test_cli_refusal_exits_1_without_credentials(tmp_path, capsys):
    fake = _Fake(tmp_path, {}, fail=True)
    code = review_cli.main(
        ["submit", "--repo", "app", "--branch", "b", "--head", SHA, "--tier", "sonnet",
         "--budget-cents", "5", "--test", "tests/test_a.py"],
        client_factory=lambda: fake.client,
    )  # fmt: skip
    out = capsys.readouterr()
    assert code == 1 and "synthetic-token" not in out.out + out.err


def test_cli_bad_argparse_exits_2():
    with pytest.raises(SystemExit) as caught:
        review_cli.main(["submit", "--repo", "app"])
    assert caught.value.code == 2
