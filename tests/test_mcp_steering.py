"""Steering tools (ADR 0055): paths, scope-gated registration, and the retry spec builder."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tessera_mcp.client import ControlClient  # noqa: E402
from tessera_mcp.config import Config  # noqa: E402
from tessera_mcp.server import CAMPAIGN_TOOL_SCOPES, create_server  # noqa: E402
from tessera_mcp.stage_retry import RetryRefused, build_retry_spec  # noqa: E402

IMAGE = "python@sha256:" + "a" * 64
STEERING = {
    "tessera_validate_campaign_spec",
    "tessera_prepare_stage_retry",
    "tessera_campaign_grants",
    "tessera_reduce_campaign_budget",
    "tessera_revoke_campaign_release",
    "tessera_pause_campaign",
    "tessera_resume_campaign",
    "tessera_limit_campaign_concurrency",
    "tessera_cancel_campaign_stage",
}


def _stage(key, inputs, outputs=("report",), exposure=0):
    return {
        "key": key,
        "provider": "local-docker",
        "runtime": "docker-cpu-v1",
        "image": IMAGE,
        "argv": ["python", "-c", "print(1)"],
        "max_duration_seconds": 60,
        "max_exposure_cents": exposure,
        "inputs": inputs,
        "outputs": [{"name": n, "path": n + ".json", "max_bytes": 100} for n in outputs],
    }


SNAP = {
    "kind": "snapshot",
    "name": "code",
    "snapshot_id": "snap",
    "digest": "b" * 64,
    "data_class": "public",
}


def _campaign(statuses, **over):
    spec = {
        "version": 2,
        "name": "chain",
        "budget_cap_cents": 10,
        "deadline_seconds": 3600,
        "stages": [
            _stage("a", [SNAP]),
            _stage(
                "b", [SNAP, {"kind": "stage", "name": "up", "stage": "a", "outputs": ["report"]}]
            ),
            _stage(
                "c", [SNAP, {"kind": "stage", "name": "up", "stage": "b", "outputs": ["report"]}]
            ),
        ],
    }
    return {
        "id": "campaign_1",
        "status": over.pop("status", "failed"),
        "settled_cents": over.pop("settled", 0),
        "spec": spec,
        "stages": [{"stage_key": k, "status": s} for k, s in statuses.items()],
        **over,
    }


def _outputs(*pairs):
    return {"outputs": [{"stage_key": k, "name": n, "sha256": d * 64} for k, n, d in pairs]}


# -- retry spec builder ---------------------------------------------------------------------------


def test_a_failed_stage_is_retried_with_its_succeeded_input_pinned_by_digest():
    campaign = _campaign({"a": "succeeded", "b": "failed", "c": "pending"})
    plan = build_retry_spec(campaign, _outputs(("a", "report", "d")))
    spec = plan["spec"]
    assert [s["key"] for s in spec["stages"]] == ["b", "c"]
    assert plan["retried_stages"] == ["b", "c"] and spec["name"] == "chain-retry"
    pinned = [i for i in spec["stages"][0]["inputs"] if i["kind"] == "artifact"]
    assert pinned == [
        {
            "kind": "artifact",
            "name": "up",
            "campaign_id": "campaign_1",
            "stage": "a",
            "output": "report",
            "sha256": "d" * 64,
        }
    ]
    assert spec["stages"][1]["inputs"][1]["kind"] == "stage"  # c still reads b, which re-runs
    assert plan["reused_outputs"] == [{"stage": "a", "output": "report", "sha256": "d" * 64}]
    assert "nothing was submitted" in plan["note"]
    assert campaign["spec"]["stages"][1]["inputs"][1]["kind"] == "stage"  # the source is untouched


def test_a_stage_that_read_a_rerun_stage_runs_again_even_if_it_had_succeeded():
    campaign = _campaign({"a": "succeeded", "b": "succeeded", "c": "failed"})
    plan = build_retry_spec(campaign, _outputs(("b", "report", "e")), stage_keys=["c"])
    assert plan["retried_stages"] == ["c"]
    rerun_b = build_retry_spec(campaign, _outputs(("a", "report", "d")), stage_keys=["b"])
    assert rerun_b["retried_stages"] == ["b", "c"]  # c consumed b


def test_the_cap_never_exceeds_the_original_and_covers_the_retried_stages():
    campaign = _campaign({"a": "succeeded", "b": "failed", "c": "pending"}, settled=3)
    campaign["spec"]["stages"][1]["max_exposure_cents"] = 4
    outputs = _outputs(("a", "report", "d"))
    assert build_retry_spec(campaign, outputs)["spec"]["budget_cap_cents"] == 7
    assert build_retry_spec(campaign, outputs, budget_cap_cents=5)["spec"]["budget_cap_cents"] == 5
    with pytest.raises(RetryRefused, match="more than the original"):
        build_retry_spec(campaign, outputs, budget_cap_cents=11)
    with pytest.raises(RetryRefused, match="cover the retried stages"):
        build_retry_spec(campaign, outputs, budget_cap_cents=3)


@pytest.mark.parametrize(
    ("campaign", "outputs", "message"),
    [
        (
            _campaign({"a": "running", "b": "pending", "c": "pending"}, status="running"),
            {},
            "Only a",
        ),
        (_campaign({"a": "succeeded", "b": "succeeded", "c": "succeeded"}), {}, "nothing to retry"),
        (_campaign({"a": "succeeded", "b": "failed", "c": "pending"}), _outputs(), "no single"),
        (
            _campaign({"a": "succeeded", "b": "failed", "c": "pending"}),
            _outputs(("a", "report", "d"), ("a", "report", "e")),
            "no single",
        ),
    ],
)
def test_the_builder_refuses_rather_than_guess(campaign, outputs, message):
    with pytest.raises(RetryRefused, match=message):
        build_retry_spec(campaign, outputs)


def test_v1_specs_and_unknown_or_multi_output_inputs_are_refused():
    v1 = _campaign({"a": "failed", "b": "pending", "c": "pending"})
    v1["spec"]["version"] = 1
    with pytest.raises(RetryRefused, match="v2"):
        build_retry_spec(v1, {})
    campaign = _campaign({"a": "succeeded", "b": "failed", "c": "pending"})
    with pytest.raises(RetryRefused, match="Unknown stage"):
        build_retry_spec(campaign, {}, stage_keys=["nope"])
    campaign["spec"]["stages"][1]["inputs"][1]["outputs"] = ["report", "more"]
    with pytest.raises(RetryRefused, match="several outputs"):
        build_retry_spec(campaign, _outputs(("a", "report", "d")))


# -- tools ----------------------------------------------------------------------------------------


def _server(tmp_path, seen, reply, actions=None):
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
        body = reply(request) if callable(reply) else reply
        return httpx.Response(200, json=body)

    client = ControlClient(
        Config("http://127.0.0.1:8765", reference, "proj1", "dest"), httpx.MockTransport(handler)
    )
    return create_server(client, campaign_tools=True, campaign_actions=actions)


def _call(server, name, arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    return result[1] if isinstance(result, tuple) else result


def test_steering_tools_exist_only_with_their_scopes_and_no_approval_or_raise_tool(tmp_path):
    forced = {t.name for t in asyncio.run(_server(tmp_path, [], {}).list_tools())}
    assert STEERING <= forced
    # No tool approves unconditionally. The one name with "approve" in it acts only under a
    # person's signed delegation and only when its written rule is met (ADR 0122).
    delegated = {"tessera_delegated_approve_campaign"}
    assert not [
        n
        for n in forced
        if n not in delegated and ("approve" in n or "reconcile" in n or "raise" in n)
    ]
    assert {n for n in forced if "approve" in n} <= delegated
    read_only = _server(tmp_path, [], {}, actions=frozenset({"campaign:read"}))
    names = {t.name for t in asyncio.run(read_only.list_tools())}
    assert names & STEERING == set()  # the retry tool also needs artifact:read, to read outputs
    both = _server(tmp_path, [], {}, actions=frozenset({"campaign:read", "artifact:read"}))
    names = {t.name for t in asyncio.run(both.list_tools())}
    assert names & STEERING == {"tessera_prepare_stage_retry"}
    cancel = _server(tmp_path, [], {}, actions=frozenset({"campaign:cancel", "campaign:submit"}))
    names = {t.name for t in asyncio.run(cancel.list_tools())}
    assert names & STEERING == {
        "tessera_validate_campaign_spec",
        "tessera_reduce_campaign_budget",
        "tessera_revoke_campaign_release",
        "tessera_pause_campaign",
        "tessera_resume_campaign",
        "tessera_limit_campaign_concurrency",
        "tessera_cancel_campaign_stage",
    }
    assert {CAMPAIGN_TOOL_SCOPES[n] for n in STEERING} <= {
        "campaign:submit",
        "campaign:read",
        "campaign:logs",
        "campaign:cancel",
    }  # no new scope: enrollment is unchanged


def test_revoke_is_marked_destructive_and_the_other_writes_are_not(tmp_path):
    tools = {t.name: t for t in asyncio.run(_server(tmp_path, [], {}).list_tools())}
    assert tools["tessera_revoke_campaign_release"].annotations.destructiveHint is True
    assert tools["tessera_reduce_campaign_budget"].annotations.destructiveHint is False
    assert tools["tessera_validate_campaign_spec"].annotations.readOnlyHint is True


def test_an_output_that_is_not_custody_released_is_refused_not_pinned():
    campaign = _campaign({"a": "succeeded", "b": "failed", "c": "pending"})
    outputs = {
        "outputs": [{**_outputs(("a", "report", "d"))["outputs"][0], "custody_released": False}]
    }
    with pytest.raises(RetryRefused, match="custody-released"):
        build_retry_spec(campaign, outputs)


def test_steering_calls_hit_the_expected_control_paths(tmp_path):
    seen: list = []
    server = _server(tmp_path, seen, {"ok": True})
    spec = {
        "version": 2,
        "name": "n",
        "budget_cap_cents": 1,
        "deadline_seconds": 600,
        "stages": [_stage("a", [SNAP])],
    }
    _call(server, "tessera_validate_campaign_spec", {"spec": spec})
    _call(server, "tessera_campaign_grants", {"campaign_id": "campaign_1"})
    _call(
        server,
        "tessera_reduce_campaign_budget",
        {
            "campaign_id": "campaign_1",
            "expected_revision": 3,
            "budget_cap_cents": 2,
            "reason": "stop",
            "idempotency_key": "k-budget",
        },
    )
    _call(
        server,
        "tessera_revoke_campaign_release",
        {
            "campaign_id": "campaign_1",
            "release_id": "rel_1",
            "expected_revision": 1,
            "reason": "stale",
            "idempotency_key": "k-rel",
        },
    )
    assert [(m, p, k) for m, p, k, _ in seen] == [
        ("POST", "/projects/proj1/campaign-specs/validate", None),
        ("GET", "/campaigns/campaign_1/grants", None),
        ("POST", "/campaigns/campaign_1/budget-reduction", "k-budget"),
        ("POST", "/campaigns/campaign_1/releases/rel_1/revoke", "k-rel"),
    ]
    assert json.loads(seen[2][3]) == {
        "expected_revision": 3,
        "budget_cap_cents": 2,
        "reason": "stop",
    }


def test_prepare_stage_retry_reads_only_and_returns_the_spec_without_submitting(tmp_path):
    seen: list = []
    campaign = _campaign({"a": "succeeded", "b": "failed", "c": "pending"})

    def reply(request):
        return _outputs(("a", "report", "d")) if request.url.path.endswith("/outputs") else campaign

    server = _server(tmp_path, seen, reply)
    plan = _call(server, "tessera_prepare_stage_retry", {"campaign_id": "campaign_1"})
    assert plan["retried_stages"] == ["b", "c"]
    assert {m for m, *_ in seen} == {"GET"}  # it never posts: submission stays a separate step
    with pytest.raises(Exception, match="nothing to retry"):
        campaign["stages"] = [{"stage_key": k, "status": "succeeded"} for k in "abc"]
        _call(server, "tessera_prepare_stage_retry", {"campaign_id": "campaign_1"})


def test_pause_limit_and_stage_cancel_hit_the_expected_control_paths(tmp_path):
    seen: list = []
    server = _server(tmp_path, seen, {"ok": True})
    base = {"campaign_id": "campaign_1", "expected_revision": 4, "reason": "r"}
    _call(server, "tessera_pause_campaign", {**base, "idempotency_key": "k-p"})
    _call(server, "tessera_resume_campaign", {**base, "idempotency_key": "k-r"})
    _call(
        server,
        "tessera_limit_campaign_concurrency",
        {**base, "max_concurrent_stages": 2, "idempotency_key": "k-l"},
    )
    _call(
        server,
        "tessera_cancel_campaign_stage",
        {**base, "stage_key": "s2", "idempotency_key": "k-c"},
    )
    assert [(m, p, k) for m, p, k, _ in seen] == [
        ("POST", "/campaigns/campaign_1/pause", "k-p"),
        ("POST", "/campaigns/campaign_1/resume", "k-r"),
        ("POST", "/campaigns/campaign_1/concurrency-limit", "k-l"),
        ("POST", "/campaigns/campaign_1/stages/s2/cancel", "k-c"),
    ]
    assert json.loads(seen[2][3]) == {
        "expected_revision": 4,
        "max_concurrent_stages": 2,
        "reason": "r",
    }
    assert json.loads(seen[3][3]) == {"expected_revision": 4, "reason": "r"}
