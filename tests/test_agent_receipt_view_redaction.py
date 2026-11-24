"""The agent view of a campaign receipt hides approver identifiers; the stored receipt is whole.

A real runner drives a Salad campaign whose reviews are resolved under an exact-digest
pre-approval (ADR 0044), so its stored receipt carries ``approval.approving_principal_id`` and a
``preapprovals`` section. That stored row, and what a person reads, keep every identifier. An
external-scope agent reading the receipt through the route, ``tessera_get_campaign_receipt``,
``tessera_campaign_wave_receipts`` or any other read tool gets the marked projection
(``receipt_agent_view``): no approving principal, pre-approval id or digest, resolved
decision-request id or review digest, while the caps, expiry and resolved kinds stay.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from salad_campaign_fixture import salad_provider_reset  # noqa: F401
from test_pending_approval_preapproved_visibility import (
    CLEAN,
    DEST,
    READS,
    _leaks,
    _preapproved_world,
    _read_all,
    _secrets,
    _token,
)

from tessera_control.api import create_app
from tessera_control.receipt_agent_view import HIDDEN_PATHS, VIEW, agent_receipt
from tessera_control.store import canonical_digest

REMOVED = [
    "approval.approving_principal_id",
    "preapprovals[].id",
    "preapprovals[].source_digest",
    "preapprovals[].plan_digest",
    "preapprovals[].source_use_digest",
    "preapprovals[].resolutions[].subject_id",
    "preapprovals[].resolutions[].review_digest",
]


def _stored(world) -> tuple[str, str]:
    [row] = world.env.query(
        "SELECT receipt_json, receipt_digest FROM campaign_receipts WHERE campaign_id=?",
        world.campaign_id,
    )
    return row["receipt_json"], row["receipt_digest"]


def _mcp(world, tmp_path) -> dict[str, dict]:
    pytest.importorskip("mcp")
    from tessera_mcp.client import ControlClient
    from tessera_mcp.config import Config
    from tessera_mcp.server import create_server

    reference = tmp_path / "agent.token"
    reference.write_text("synthetic-agent-token-" + "x" * 30)
    reference.chmod(0o600)
    client = ControlClient(
        Config("http://127.0.0.1:8765", reference, world.env.project["id"], "planner.local"),
        httpx.ASGITransport(app=create_app(world.env.state)),
    )
    server = create_server(client, campaign_tools=True, campaign_actions=frozenset(READS))
    cid = world.campaign_id
    calls = {
        "tessera_get_campaign": {"campaign_id": cid},
        "tessera_campaign_report": {"campaign_id": cid, "detail": True},
        "tessera_get_campaign_receipt": {"campaign_id": cid},
        "tessera_campaign_wave_receipts": {"campaign_ids": [cid]},
        "tessera_campaign_outputs": {"campaign_id": cid},
        "tessera_campaign_logs": {"campaign_id": cid},
        "tessera_campaign_grants": {"campaign_id": cid},
        "tessera_campaign_retries": {"campaign_id": cid},
        "tessera_campaign_budget": {"campaign_id": cid},
        "tessera_campaign_holdout": {"campaign_id": cid},
        "tessera_campaign_stage_watch": {"campaign_id": cid},
        "tessera_campaign_gate_decisions": {"campaign_id": cid},
        "tessera_campaign_standing_status": {},
        "tessera_project_spend": {},
        "tessera_campaign_holdout_claims": {},
    }
    results = {}
    for name, arguments in calls.items():
        result = asyncio.run(server.call_tool(name, arguments))
        results[name] = result[1] if isinstance(result, tuple) else result
    return results


def _assert_agent_view(view: dict, stored_digest: str, human: dict) -> None:
    assert view["view"] == VIEW
    assert view["redacted"] == REMOVED
    assert view["receipt"]["view"] == VIEW
    # The stored digest is passed through as the name of the audit record, not recomputed.
    assert view["receipt_digest"] == stored_digest
    assert canonical_digest(view["receipt"]) != stored_digest
    receipt = view["receipt"]
    assert "approving_principal_id" not in receipt["approval"]
    # What was approved, and the pre-approval's scope, caps, expiry and resolved kinds, stay.
    full = human["receipt"]
    for key in ("id", "decision_class", "approved_at", "expires_at"):
        assert receipt["approval"][key] == full["approval"][key]
    assert receipt["spec_digest"] == full["spec_digest"]
    [entry] = receipt["preapprovals"]
    [whole] = full["preapprovals"]
    assert set(entry) == {
        "approval_id",
        "stage_key",
        "max_attempt_cents",
        "max_total_cents",
        "expires_at",
        "resolutions",
    }
    for key in set(entry) - {"resolutions"}:
        assert entry[key] == whole[key]
    assert entry["max_attempt_cents"] == entry["max_total_cents"] == 50
    assert sorted(r["kind"] for r in entry["resolutions"]) == [
        "plan",
        "source_assessment",
        "source_use",
    ]
    for kept, source in zip(entry["resolutions"], whole["resolutions"], strict=True):
        assert set(kept) == {"kind", "attempt", "cents", "decision_class"}
        assert kept == {k: source[k] for k in kept}
    # Nothing else of the receipt changed.
    rest = json.loads(json.dumps(full))
    rest["approval"].pop("approving_principal_id")
    rest.pop("preapprovals")
    mine = {k: v for k, v in receipt.items() if k not in {"preapprovals", "view"}}
    assert mine == rest


def test_an_agent_reads_the_marked_receipt_view_and_a_person_the_stored_receipt(tmp_path):
    world = _preapproved_world(tmp_path)
    world.run_to_outcome(guardian_final=CLEAN)
    world.tick_until(lambda: world.status() == "succeeded")
    secrets = _secrets(world)
    receipt_json, digest = _stored(world)
    stored = json.loads(receipt_json)
    assert canonical_digest(stored) == digest

    # Non-vacuous: the stored receipt names each identifier the agent view removes.
    salad = world.salad()
    assert stored["approval"]["approving_principal_id"] == world.human["id"]
    [pre] = stored["preapprovals"]
    for column in ("id", "source_digest", "plan_digest", "source_use_digest"):
        assert pre[column] == secrets[f"preapproval.{column}"]
    subjects = {r["kind"]: r["subject_id"] for r in pre["resolutions"]}
    assert subjects["plan"] == salad["plan_decision_id"]
    assert subjects["source_use"] == salad["source_decision_id"]
    assert all(r["review_digest"] for r in pre["resolutions"])
    in_store = _leaks(stored, secrets)
    for name in (
        "approval.approving_principal_id",
        "human.principal_id",
        "preapproval.id",
        "preapproval.plan_digest",
        "plan_decision_id",
        "source_decision_id",
    ):
        assert name in in_store

    client = TestClient(create_app(world.env.state))
    human = _read_all(world, client, _token("human"), terminal=True)
    agent = _read_all(world, client, {**_token("agent"), **DEST}, terminal=True)
    # A person reads the stored receipt whole, unmarked.
    assert human["receipt"] == {
        "campaign_id": world.campaign_id,
        "receipt": stored,
        "receipt_digest": digest,
        "created_at": human["receipt"]["created_at"],
    }
    _assert_agent_view(agent["receipt"], digest, human["receipt"])
    leaked = {name: found for name, body in agent.items() if (found := _leaks(body, secrets))}
    assert leaked == {}

    tools = _mcp(world, tmp_path)
    assert tools["tessera_get_campaign_receipt"] == agent["receipt"]
    [wave] = tools["tessera_campaign_wave_receipts"]["waves"]
    assert wave["receipt"] == agent["receipt"]
    _assert_agent_view(wave["receipt"], digest, human["receipt"])
    leaked = {name: found for name, body in tools.items() if (found := _leaks(body, secrets))}
    assert leaked == {}

    # The agent reads never touched the stored row.
    assert _stored(world) == (receipt_json, digest)


def test_the_projection_removes_every_listed_path_and_leaves_its_input_alone():
    receipt = {
        "spec_digest": "s" * 64,
        "approval": {
            "id": "appr_1",
            "approving_principal_id": "prn_owner",
            "smoke_grant_id": "sgrant_1",
            "decision_class": "human_direct",
            "expires_at": "2026-12-01T00:00:00Z",
        },
        "preapprovals": [
            {
                "id": "preapr_1",
                "approval_id": "appr_1",
                "stage_key": "g1",
                "source_digest": "a" * 64,
                "plan_digest": "b" * 64,
                "source_use_digest": "c" * 64,
                "max_attempt_cents": 5,
                "max_total_cents": 9,
                "expires_at": "2026-12-01T00:00:00Z",
                "resolutions": [
                    {
                        "kind": "plan",
                        "subject_id": "dreq_1",
                        "attempt": 1,
                        "review_digest": "b" * 64,
                        "cents": 4,
                        "decision_class": "campaign_delegated",
                    }
                ],
            }
        ],
        "submission": {
            "submission_approval_id": "subap_1",
            "approved_by_principal_id": "prn_owner",
            "per_run_budget_cap_cents": 7,
            "decisions": [{"decision": "approved", "actor_principal_id": "prn_owner"}],
        },
    }
    before = json.dumps(receipt, sort_keys=True)
    projected, removed = agent_receipt(receipt)
    assert json.dumps(receipt, sort_keys=True) == before
    assert removed == list(HIDDEN_PATHS)
    text = json.dumps(projected)
    for secret in ("prn_owner", "sgrant_1", "preapr_1", "dreq_1", "a" * 64, "b" * 64, "c" * 64):
        assert secret not in text
    assert projected["view"] == VIEW
    assert projected["approval"] == {
        "id": "appr_1",
        "decision_class": "human_direct",
        "expires_at": "2026-12-01T00:00:00Z",
    }
    assert projected["preapprovals"][0]["resolutions"] == [
        {"kind": "plan", "attempt": 1, "cents": 4, "decision_class": "campaign_delegated"}
    ]
    assert projected["submission"]["submission_approval_id"] == "subap_1"
    assert projected["submission"]["decisions"] == [{"decision": "approved"}]
    # A receipt without those sections is only marked.
    plain, nothing = agent_receipt({"approval": None, "stages": []})
    assert nothing == [] and plain == {"approval": None, "stages": [], "view": VIEW}
