"""One agent steers a campaign over tessera_mcp only and approves it under a delegation (ADR 0122).

Real control app (in process, temp state), the real campaign runner and a fake worker on the
``local-docker`` provider: no network and no Docker. The only human acts, marked ``HUMAN``, are
signing the approval delegation and the provider trust set-up. Every agent step is an MCP tool
call, and two generic checks run over all of them:

- every acting step leaves a recorded event or receipt, and every read-only step changes no
  stored state (a fingerprint of every table is compared around the call);
- no response the agent received carries a key matching ``approv.*principal``, while the stored
  approval row and the person's view of the campaign do carry it (so the check is not vacuous).

Two agent projections are on branches not yet merged here: the redacted receipt view
(``tessera_control.receipt_agent_view``) and the delegated-approve response projection
(``DelegatedDecisionStore.agent_view``). Until each is present the no-approver check skips only
the tools it covers; once merged, the check covers them automatically.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import re
import sqlite3
from typing import Any

import httpx
import pytest

pytest.importorskip("mcp")

from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402
from test_approval_delegations import later, terms  # noqa: E402
from test_campaign_standing import Sched  # noqa: E402
from test_mcp_steering_e2e import AGENT_ACTIONS, _spec, _stage  # noqa: E402

from tessera_control import campaign_gates  # noqa: E402
from tessera_control import campaign_provider_worker as worker  # noqa: E402
from tessera_control import campaign_providers as cp  # noqa: E402
from tessera_control.api import create_app  # noqa: E402
from tessera_control.approval_delegations import ApprovalDelegationCommands  # noqa: E402
from tessera_control.delegated_decisions import DECIDE_SCOPE, DelegatedDecisionStore  # noqa: E402
from tessera_control.store import canonical_digest  # noqa: E402
from tessera_mcp.client import ControlClient  # noqa: E402
from tessera_mcp.config import Config  # noqa: E402
from tessera_mcp.server import create_server  # noqa: E402

APPROVER_KEY = re.compile(r"approv.*principal", re.I)
ACTIONS = sorted(set(AGENT_ACTIONS) | {DECIDE_SCOPE})

# Agent projections that live on unmerged branches: tool -> whether the projection is present.
# A tool whose projection is absent is skipped by the no-approver check (and listed); once the
# projection merges, the same check covers it with no edit here.
PENDING_PROJECTION = {
    "tessera_get_campaign_receipt": importlib.util.find_spec("tessera_control.receipt_agent_view")
    is not None,
    "tessera_campaign_wave_receipts": importlib.util.find_spec("tessera_control.receipt_agent_view")
    is not None,
    "tessera_delegated_approve_campaign": hasattr(DelegatedDecisionStore, "agent_view"),
}


@pytest.fixture(autouse=True)
def _reset():
    yield
    worker.register_from_config(None)
    campaign_gates.configure_go_authority(accept_automation_labelled=False)


def approver_keys(value: Any, path: str = "$") -> list[str]:
    """Every path in a response whose key matches ``approv.*principal`` (JSON text included)."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if (
                key == "redacted"
                and isinstance(item, list)
                and all(isinstance(x, str) for x in item)
            ):
                # The agent receipt view lists the NAMES of the paths it hid, by design; the
                # values are checked separately by response_values().
                continue
            if APPROVER_KEY.search(str(key)):
                found.append(f"{path}.{key}")
            found += approver_keys(item, f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            found += approver_keys(item, f"{path}[{index}]")
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            if re.search(r"approv\w*principal", value, re.I):
                found.append(f"{path}<text>")
        else:
            if isinstance(parsed, dict | list):
                found += approver_keys(parsed, path)
    elif hasattr(value, "text"):  # MCP TextContent
        found += approver_keys(value.text, path)
    return found


def response_strings(value: Any) -> list[str]:
    """Every string in a response, at any depth (MCP TextContent and JSON text included)."""
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in [str(k), *response_strings(v)]]
    if isinstance(value, list | tuple):
        return [s for item in value for s in response_strings(item)]
    if isinstance(value, str):
        return [value]
    if hasattr(value, "text"):
        return [value.text]
    return []


def find_key(value: Any, name: str) -> list[Any]:
    """Every value stored under ``name`` in a response (JSON text parsed)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    elif hasattr(value, "text"):
        return find_key(value.text, name)
    if isinstance(value, dict):
        return [v for k, item in value.items() for v in ([item] if k == name else [])] + [
            v for item in value.values() for v in find_key(item, name)
        ]
    if isinstance(value, list | tuple):
        return [v for item in value for v in find_key(item, name)]
    return []


class Recorder:
    """One enrolled agent; records what each MCP tool call left in the store."""

    def __init__(self, sched: Sched, tmp_path) -> None:
        env = sched.env
        self.env = env
        env.agent(ACTIONS, "steerer")
        reference = tmp_path / "agent.token"
        reference.write_text("synthetic-steerer-token-" + "x" * 30)
        reference.chmod(0o600)
        self.client = ControlClient(
            Config("http://127.0.0.1:8765", reference, env.project["id"], "planner.local"),
            httpx.ASGITransport(app=create_app(env.state)),
        )
        self.server = create_server(
            self.client, campaign_tools=True, campaign_actions=frozenset(ACTIONS)
        )
        self.read_only = {
            tool.name: bool(tool.annotations and tool.annotations.readOnlyHint)
            for tool in asyncio.run(self.server.list_tools())
        }
        self.steps: list[dict[str, Any]] = []

    # -- store observation --------------------------------------------------------------------

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.env.state / "tessera.sqlite3")

    def _mark(self) -> tuple[int, int]:
        with self._db() as db:
            event = db.execute("SELECT COALESCE(MAX(rowid), 0) FROM events").fetchone()[0]
            receipt = db.execute("SELECT COALESCE(MAX(rowid), 0) FROM campaign_receipts").fetchone()
        return event, receipt[0]

    def fingerprint(self) -> dict[str, str]:
        """A digest of every table's rows: a read-only step must leave all of them unchanged."""
        out: dict[str, str] = {}
        with self._db() as db:
            tables = [
                r[0]
                for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            for table in tables:
                rows = db.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall()  # noqa: S608
                out[table] = hashlib.sha256(repr(rows).encode()).hexdigest()
        return out

    # -- one step -----------------------------------------------------------------------------

    def __call__(self, name: str, **arguments):
        before_mark, before_state = self._mark(), self.fingerprint()
        error: ToolError | None = None
        try:
            raw = asyncio.run(self.server.call_tool(name, arguments))
        except ToolError as caught:
            error, raw = caught, str(caught)
        with self._db() as db:
            events = [
                r[0]
                for r in db.execute(
                    "SELECT event_type FROM events WHERE rowid > ? ORDER BY rowid",
                    (before_mark[0],),
                )
            ]
            receipts = [
                r[0]
                for r in db.execute(
                    "SELECT campaign_id FROM campaign_receipts WHERE rowid > ?", (before_mark[1],)
                )
            ]
        changed = sorted(k for k, v in self.fingerprint().items() if before_state.get(k) != v)
        self.steps.append(
            {
                "tool": name,
                "read_only": self.read_only[name],
                "events": events,
                "receipts": receipts,
                "changed": changed,
                "response": raw,
                "refused": error is not None,
            }
        )
        if error is not None:
            raise error
        return raw[1] if isinstance(raw, tuple) else raw

    # -- the two generic checks ---------------------------------------------------------------

    def assert_every_step_recorded(self) -> None:
        assert self.steps
        for step in self.steps:
            if step["read_only"]:
                assert not step["events"] and not step["receipts"], step["tool"]
                assert step["changed"] == [], (step["tool"], step["changed"])
            else:
                assert step["events"] or step["receipts"], (step["tool"], step["refused"])

    def assert_no_approver_in_responses(self, values: tuple[str, ...] = ()) -> list[str]:
        """Checks every response; returns the tools skipped because their projection is absent.

        No key names an approver, and none of ``values`` (the real approver and signer ids)
        appears anywhere in a response, including inside a view's ``redacted`` list."""
        assert all(values)
        skipped: set[str] = set()
        for step in self.steps:
            if PENDING_PROJECTION.get(step["tool"]) is False:
                skipped.add(step["tool"])
                continue
            found = approver_keys(step["response"])
            assert not found, (step["tool"], found)
            text = "\n".join(response_strings(step["response"]))
            leaked = [v for v in values if v in text]
            assert not leaked, (step["tool"], "approver value in response")
        return sorted(skipped)


def _delegate(env, per_run: int, ceiling: int) -> dict:
    """HUMAN: the owner signs a delegation whose written rule is a per-run and a total cap."""
    body = {
        "terms": terms(
            budget_ceiling_cents=ceiling, per_run_cap_cents=per_run, max_approvals=3, gate_rules=[]
        ),
        "expires_at": later(),
    }
    status, record = ApprovalDelegationCommands(env.state).create_approval_delegation(
        env.human(), env.project["id"], "delegate-1", canonical_digest(body), body
    )
    assert status == 201
    return record


def test_an_agent_approves_within_a_delegation_and_an_out_of_rule_campaign_stays_with_a_person(
    tmp_path,
):
    from provider_trust_fixture import DERIVED, install_trust

    sched = Sched(tmp_path)
    world, env = sched.world, sched.env
    cp.configure_providers({"local_docker": {"worker_principal_id": env.worker["id"]}})
    install_trust({"local-docker": (DERIVED, [env.worker["id"]])})
    delegation = _delegate(env, per_run=12, ceiling=40)
    steer = Recorder(sched, tmp_path)

    # -- 1. the agent finds the delegation it may act under ------------------------------------
    listed = steer("tessera_list_approval_delegations", status="active")
    assert delegation["id"] in json.dumps(listed)
    shown = steer("tessera_get_approval_delegation", delegation_id=delegation["id"])
    assert shown["delegation"]["terms"]["per_run_cap_cents"] == 12

    # -- 2. submit a campaign inside the rule and approve it under the delegation ---------------
    spec_a = _spec("delegated-in-rule", _stage(env, "s1"), _stage(env, "s2", upstream="s1"))
    assert steer("tessera_validate_campaign_spec", spec=spec_a)
    a = steer("tessera_submit_campaign", spec=spec_a, idempotency_key="submit-a")["id"]
    live = steer("tessera_get_campaign", campaign_id=a)
    assert live["status"] == "submitted"
    approved = steer(
        "tessera_delegated_approve_campaign",
        campaign_id=a,
        delegation_id=delegation["id"],
        expected_revision=live["revision"],
        expected_spec_digest=live["spec_digest"],
        idempotency_key="approve-a",
    )
    assert approved["approval"]["delegation_id"] == delegation["id"]
    assert steer.steps[-1]["events"]  # the delegated approval is journalled
    assert {"campaign.approved", "campaign.delegated_approval"} <= set(steer.steps[-1]["events"])

    # -- 3. the runner advances it to a terminal receipt; the agent reads it all ---------------
    world.tick_all(a)
    assert world.stages(a) == {"s1": "succeeded", "s2": "succeeded"}
    assert world.status(a) == "succeeded"
    assert steer("tessera_get_campaign", campaign_id=a)["status"] == "succeeded"
    assert steer("tessera_campaign_stage_status", campaign_id=a)
    logs = steer("tessera_campaign_logs", campaign_id=a)
    assert {att["stage_key"] for att in logs["attempts"]} == {"s1", "s2"}
    budget = steer("tessera_campaign_budget", campaign_id=a)
    assert budget["held_cents"] == 0 and budget["budget_cap_cents"] == 10
    grants = steer("tessera_campaign_grants", campaign_id=a)
    assert all(g["grant_status"] == "reconciled" for g in grants["grants"])
    receipt = steer("tessera_get_campaign_receipt", campaign_id=a)
    (decision,) = receipt["receipt"]["delegation"]["decisions"]
    assert decision["outcome"] == "approved" and decision["delegation_id"] == delegation["id"]
    assert steer("tessera_campaign_outputs", campaign_id=a)["outputs"]

    # -- 4. an out-of-rule campaign is refused and stays with a person --------------------------
    spec_b = _spec("delegated-out-of-rule", _stage(env, "s1"), cap=30)
    b = steer("tessera_submit_campaign", spec=spec_b, idempotency_key="submit-b")["id"]
    live_b = steer("tessera_get_campaign", campaign_id=b)
    with pytest.raises(ToolError) as refused:
        steer(
            "tessera_delegated_approve_campaign",
            campaign_id=b,
            delegation_id=delegation["id"],
            expected_revision=live_b["revision"],
            expected_spec_digest=live_b["spec_digest"],
            idempotency_key="approve-b",
        )
    assert "outside_delegation" in str(refused.value)
    assert "per_run_cap_exceeded" in str(refused.value)
    assert "campaign.delegation_escalated" in steer.steps[-1]["events"]
    assert world.status(b) == "submitted"
    assert not env.query("SELECT 1 FROM campaign_approvals WHERE campaign_id=?", b)
    for _ in range(5):
        world.runner.tick()
    assert world.status(b) == "submitted"  # nothing runs without a person
    assert steer("tessera_get_campaign", campaign_id=b)["status"] == "submitted"
    asked = steer("tessera_delegation_escalations")
    assert b in json.dumps(asked)
    owner_view = DelegatedDecisionStore(env.state).list_delegation_escalations(
        env.human(), env.project["id"]
    )
    assert [e["campaign_id"] for e in owner_view["escalations"]] == [b]

    # -- 5. the generic checks over every step ---------------------------------------------------
    steer.assert_every_step_recorded()
    acted = [s["tool"] for s in steer.steps if not s["read_only"]]
    assert acted == [
        "tessera_submit_campaign",
        "tessera_delegated_approve_campaign",
        "tessera_submit_campaign",
        "tessera_delegated_approve_campaign",
    ]
    # Non-vacuous: the approver is recorded and a person sees it.
    (stored,) = env.query("SELECT approving_principal_id FROM campaign_approvals")
    assert stored["approving_principal_id"]
    person = env.store.get_campaign(env.human(), a)
    assert approver_keys(person), "the person's view should name the approver"
    assert person["approvals"][0]["approving_principal_id"] == stored["approving_principal_id"]
    signer = tuple(
        v
        for v in (
            delegation.get("created_by_principal_id"),
            delegation.get("created_credential_id"),
        )
        if v
    )
    skipped = steer.assert_no_approver_in_responses((stored["approving_principal_id"], *signer))
    # The redaction is positively checked: the receipt view names the hidden approver path.
    if PENDING_PROJECTION["tessera_get_campaign_receipt"]:
        receipts = [s for s in steer.steps if s["tool"] == "tessera_get_campaign_receipt"]
        assert receipts
        for step in receipts:
            lists = find_key(step["response"], "redacted")
            assert lists and all("approval.approving_principal_id" in r for r in lists), lists
    assert set(skipped) == {t for t, present in PENDING_PROJECTION.items() if not present} & {
        s["tool"] for s in steer.steps
    }
