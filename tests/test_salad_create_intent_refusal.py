"""A create or start the trusted server would refuse is refused before any intent exists.

No network and no provider: the dispatcher runs against a recording fake store, the unclaimed
proof against a hand-made in-memory schema, and the executor against a fake control."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tessera_control import remote_execution_salad as salad
from tessera_control.remote_execution_salad import (
    PREFLIGHT_INTENT_MAX_AGE_SECONDS,
    preflight_refusal,
)
from tessera_control.salad_precreate import PreflightStale
from tessera_control.salad_server import ProviderReadUnavailable
from tessera_control.salad_unclaimed import unclaimed_proof
from tessera_control.store import Conflict
from tessera_worker import remote_dispatch
from tessera_worker.remote_dispatch import RemoteDispatcher
from tessera_worker.salad_remote import SaladTrustedAdapter


def stamp(age: float = 0.0) -> str:
    moment = datetime.now(UTC) - timedelta(seconds=age)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def fresh_preflight(**changes):
    value = {
        "observed_at": stamp(),
        "available": True,
        "quota_ok": True,
        "group_absent": True,
        "state": "stopped",
        "provider_id": "prov-1",
    }
    return value | changes


# -- the shared use-time check ----------------------------------------------------------------


def test_preflight_refusal_accepts_a_fresh_admissible_create_and_start():
    assert preflight_refusal(fresh_preflight(), "create", None) is None
    assert preflight_refusal(fresh_preflight(group_absent=False), "start", "prov-1") is None


@pytest.mark.parametrize("age", [-5, 5.5, 60])
def test_preflight_refusal_reports_stale_outside_the_age_window(age):
    assert preflight_refusal(fresh_preflight(observed_at=stamp(age)), "create", None) == (
        "preflight_stale"
    )


def test_preflight_refusal_freshness_boundary_follows_max_age():
    old = fresh_preflight(observed_at=stamp(4))
    assert preflight_refusal(old, "create", None) is None
    limit = PREFLIGHT_INTENT_MAX_AGE_SECONDS
    assert preflight_refusal(old, "create", None, max_age=limit) == "preflight_stale"
    assert (
        preflight_refusal(fresh_preflight(observed_at=stamp(2)), "create", None, max_age=limit)
        is None
    )


@pytest.mark.parametrize("field", ["available", "quota_ok"])
@pytest.mark.parametrize("action", ["create", "start"])
def test_preflight_refusal_capacity_unavailable(field, action):
    preflight = fresh_preflight(**{field: False}, state="stopped")
    assert preflight_refusal(preflight, action, "prov-1") == "capacity_unavailable"


def test_preflight_refusal_group_present_only_blocks_create():
    preflight = fresh_preflight(group_absent=False)
    assert preflight_refusal(preflight, "create", None) == "group_present"
    assert preflight_refusal(preflight, "observe", None) is None


def test_preflight_refusal_start_needs_the_stopped_group_it_names():
    assert preflight_refusal(fresh_preflight(state="running"), "start", "prov-1") == (
        "group_not_startable"
    )
    assert preflight_refusal(fresh_preflight(), "start", "prov-other") == "group_not_startable"


@pytest.mark.parametrize("action", ["observe", "stop", "delete"])
def test_preflight_refusal_ignores_capacity_for_other_actions(action):
    preflight = fresh_preflight(available=False, quota_ok=False, group_absent=False)
    assert preflight_refusal(preflight, action, None) is None


def test_claim_proxy_still_uses_the_shared_check():
    assert salad.PREFLIGHT_MAX_AGE_SECONDS == 5
    assert PREFLIGHT_INTENT_MAX_AGE_SECONDS < salad.PREFLIGHT_MAX_AGE_SECONDS


# -- SaladTrustedAdapter.intent_refusal -------------------------------------------------------


def bare_adapter(preflight, provider_id="prov-1"):
    adapter = object.__new__(SaladTrustedAdapter)
    adapter._preflight = preflight
    adapter._route_id = "route-1"
    store = SimpleNamespace(get=lambda worker, route: {"provider_id": provider_id})
    adapter._server = SimpleNamespace(store=store, worker={"id": "w"})
    return adapter


def test_intent_refusal_is_none_without_a_preflight_or_for_other_actions():
    assert bare_adapter(None).intent_refusal("create") is None
    stale = bare_adapter(fresh_preflight(available=False, observed_at=stamp(60)))
    for action in ("observe", "stop"):
        assert stale.intent_refusal(action) is None


def test_intent_refusal_applies_the_shared_check_with_the_intent_margin():
    assert bare_adapter(fresh_preflight()).intent_refusal("create") is None
    assert bare_adapter(fresh_preflight(available=False)).intent_refusal("create") == (
        "capacity_unavailable"
    )
    assert bare_adapter(fresh_preflight(group_absent=False)).intent_refusal("create") == (
        "group_present"
    )
    # Fresh enough for the claim (5 s) but not for the intent (3 s).
    assert bare_adapter(fresh_preflight(observed_at=stamp(4))).intent_refusal("create") == (
        "preflight_stale"
    )


def test_intent_refusal_for_start_compares_the_route_provider_id():
    assert bare_adapter(fresh_preflight()).intent_refusal("start") is None
    assert bare_adapter(fresh_preflight(), provider_id="other").intent_refusal("start") == (
        "group_not_startable"
    )


# -- the dispatcher ---------------------------------------------------------------------------


class Store:
    def __init__(self):
        self.calls = []
        self.state = {
            "proposal": {},
            "routing_package": {},
            "project_id": "p",
            "task_id": "t",
            "proposal_digest": "d",
            "effects": [{"id": "e1"}],
            "authority_status": "current_trusted_server_only",
            "desired": "run",
        }

    def get(self, worker, route_id):
        self.calls.append("get")
        return self.state

    def begin(self, worker, route_id, action, **kwargs):
        self.calls.append("begin")
        return self.state

    def record(self, worker, route_id, effect_id, readback):
        self.calls.append("record")
        return {"recorded": effect_id}


@pytest.fixture
def stubbed(monkeypatch):
    """The dispatcher's model validation and digests are not under test; stub them."""
    proposal = SimpleNamespace(
        selection=SimpleNamespace(target_digest="target", model_dump=lambda mode: {})
    )
    package = SimpleNamespace(proposal=proposal, org_id="org", project_id="p", task_id="t")
    monkeypatch.setattr(
        remote_dispatch, "RemoteProposal", SimpleNamespace(model_validate=lambda v: proposal)
    )
    monkeypatch.setattr(
        remote_dispatch, "RemoteExecutionPackage", SimpleNamespace(model_validate=lambda v: package)
    )
    monkeypatch.setattr(remote_dispatch, "canonical_digest", lambda value: "d")
    return proposal


class RefusingAdapter:
    """Trusted-shaped adapter whose ``intent_refusal`` plays back a script."""

    trusted_composition = True

    def __init__(self, store, refusals):
        self.trusted_store = store
        self.refusals = list(refusals)
        self.preflights = 0
        self.performed = 0

    def preflight_for(self, action, proposal):
        self.preflights += 1
        return SimpleNamespace(admissible=True, selection_digest="d", mode="trusted_server")

    def intent_refusal(self, action):
        return self.refusals.pop(0)

    def perform(self, action, proposal, state):
        self.performed += 1
        raise ProviderReadUnavailable("down")


def dispatcher(store, adapter):
    return RemoteDispatcher(store, {"org_id": "org"}, {"target": adapter})


@pytest.mark.parametrize("reason", ["capacity_unavailable", "group_present"])
def test_trusted_create_with_unavailable_capacity_commits_no_create_effect(stubbed, reason):
    store = Store()
    adapter = RefusingAdapter(store, [reason])
    with pytest.raises(Conflict):
        dispatcher(store, adapter).dispatch("route-1", "create")
    assert "begin" not in store.calls and "record" not in store.calls
    assert adapter.preflights == 1 and adapter.performed == 0


def test_stale_preflight_is_refused_before_intent(stubbed):
    store = Store()
    adapter = RefusingAdapter(store, ["preflight_stale", "preflight_stale"])
    with pytest.raises(PreflightStale) as caught:
        dispatcher(store, adapter).dispatch("route-1", "create")
    assert isinstance(caught.value, ProviderReadUnavailable)
    assert caught.value.reason == "timeout"
    assert adapter.preflights == 2
    assert "begin" not in store.calls


def test_stale_once_then_fresh_commits_the_intent_once(stubbed):
    store = Store()
    adapter = RefusingAdapter(store, ["preflight_stale", None])
    dispatcher(store, adapter).dispatch("route-1", "create")
    assert adapter.preflights == 2
    assert store.calls.count("begin") == 1
    assert adapter.performed == 1


def test_stale_then_a_real_refusal_is_a_conflict_without_intent(stubbed):
    store = Store()
    adapter = RefusingAdapter(store, ["preflight_stale", "capacity_unavailable"])
    with pytest.raises(Conflict):
        dispatcher(store, adapter).dispatch("route-1", "create")
    assert adapter.preflights == 2
    assert "begin" not in store.calls


def test_adapter_without_intent_refusal_behaves_as_before(stubbed):
    store = Store()

    class Plain:
        def __init__(self):
            self.preflights = 0

        def preflight(self, proposal):
            self.preflights += 1
            return SimpleNamespace(admissible=True, selection_digest="d", mode="offline")

        def perform(self, action, proposal, state):
            raise ProviderReadUnavailable("down")

    store.state["authority_status"] = "current_offline_only"
    adapter = Plain()
    assert not hasattr(adapter, "intent_refusal")
    dispatcher(store, adapter).dispatch("route-1", "create")
    assert adapter.preflights == 1
    assert store.calls.count("begin") == 1


# -- unclaimed_proof --------------------------------------------------------------------------

ORG, GRANT, OPERATION = "org-1", "grant-1", "op-1"


def make_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        """CREATE TABLE external_operations (
            operation_id TEXT, adapter_version TEXT, grant_id TEXT);
        CREATE TABLE events (
            cursor INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT, event_type TEXT,
            payload_json TEXT);"""
    )
    db.execute(
        "INSERT INTO external_operations VALUES (?,?,?)",
        (OPERATION, "kubernetes-job-v1", GRANT),
    )
    return db


def add_event(db, event_type, payload):
    db.execute(
        "INSERT INTO events (org_id, event_type, payload_json) VALUES (?,?,?)",
        (ORG, event_type, json.dumps(payload)),
    )


def add_route(db, effects, receipts, desired="stop"):
    add_event(
        db,
        "remote_execution.state",
        {
            "id": "route-1",
            "proposal": {"operation_id": OPERATION},
            "desired": desired,
            "effects": effects,
            "receipts": receipts,
        },
    )


def denied_receipt(**changes):
    return {
        "effect_id": "e1",
        "provenance": "provider",
        "proxy_reason": "admission_denied",
        "provider_id": None,
        "allocation_id": None,
        "state": "unknown",
        "termination_confirmed": False,
    } | changes


CREATE = [{"id": "e1", "action": "create"}]


def test_unclaimed_proof_accepts_a_create_the_server_refused_before_any_claim():
    db = make_db()
    add_route(db, CREATE, [denied_receipt()])
    refusal, proof = unclaimed_proof(db, ORG, GRANT)
    assert refusal is None
    assert proof["route_ids"] == ["route-1"]


def test_unclaimed_proof_refuses_when_a_claim_exists_for_the_operation():
    db = make_db()
    add_route(db, CREATE, [denied_receipt()])
    add_event(db, "salad_proxy.claim", {"operation_id": OPERATION, "action": "create"})
    assert unclaimed_proof(db, ORG, GRANT)[0] == "create_claimed"


def test_unclaimed_proof_refuses_a_create_effect_with_no_receipt():
    db = make_db()
    add_route(db, CREATE, [])
    assert unclaimed_proof(db, ORG, GRANT)[0] == "route_effect"


@pytest.mark.parametrize(
    "changes",
    [
        {"proxy_reason": "provider_unavailable"},
        {"provenance": "synthetic"},
        {"provider_id": "prov-1"},
        {"allocation_id": "alloc-1"},
        {"state": "running"},
        {"termination_confirmed": True},
    ],
)
def test_unclaimed_proof_refuses_a_receipt_that_does_not_prove_the_create_unsent(changes):
    db = make_db()
    add_route(db, CREATE, [denied_receipt(**changes)])
    assert unclaimed_proof(db, ORG, GRANT)[0] == "route_effect"


def test_unclaimed_proof_refuses_when_any_receipt_of_the_effect_is_not_a_denial():
    db = make_db()
    add_route(db, CREATE, [denied_receipt(), denied_receipt(proxy_reason="provider_unavailable")])
    assert unclaimed_proof(db, ORG, GRANT)[0] == "route_effect"


def test_unclaimed_proof_ignores_the_receipt_of_another_effect():
    db = make_db()
    add_route(db, CREATE, [denied_receipt(effect_id="e2")])
    assert unclaimed_proof(db, ORG, GRANT)[0] == "route_effect"


def test_unclaimed_proof_still_refuses_a_route_that_wants_to_run():
    db = make_db()
    add_route(db, CREATE, [denied_receipt()], desired="run")
    assert unclaimed_proof(db, ORG, GRANT)[0] == "route_wants_run"


# -- the executor -----------------------------------------------------------------------------


class Control:
    def __init__(self, state=None, error=None):
        self.state, self.error = state, error
        self.no_receipt_calls = []

    def route(self, route_id):
        if self.error is not None:
            raise self.error
        return self.state

    def record_no_receipt(self, attempt, route_id):
        self.no_receipt_calls.append((attempt, route_id))


ITEM = {"attempt_id": "att-1", "route_id": "route-1", "operation_id": None}


def make_executor(control, tmp_path):
    from tessera_control.campaign_salad_executor import SaladCampaignExecutor

    executor = object.__new__(SaladCampaignExecutor)
    executor.control = control
    executor.log = []
    executor.no_receipt = set()
    executor._no_receipt_resumed = set()
    executor.work_directory = tmp_path
    executor._cancel_unrecorded_since = {}
    executor._group_stop_given_up = set()
    executor.clock = lambda: 0.0
    executor._keepalive = lambda item: None
    executor._report_group_stop = lambda item: None
    return executor


def test_resume_reports_no_receipt_for_executing_attempt_without_effects_after_restart(tmp_path):
    control = Control({"effects": []})
    executor = make_executor(control, tmp_path)
    executor._resume(ITEM)
    executor._resume(ITEM)
    assert control.no_receipt_calls == [("att-1", "route-1")]


def test_resume_does_not_report_when_the_route_has_a_create_effect(tmp_path):
    control = Control({"effects": CREATE})
    executor = make_executor(control, tmp_path)
    executor._resume(ITEM)
    assert control.no_receipt_calls == []


def test_resume_does_not_report_when_the_route_is_unreadable(tmp_path):
    control = Control(error=RuntimeError("locked"))
    executor = make_executor(control, tmp_path)
    executor._resume(ITEM)
    assert control.no_receipt_calls == []


def test_cancel_unrecorded_is_not_held_for_a_route_without_effects(tmp_path):
    executor = make_executor(Control({"effects": []}), tmp_path)
    assert executor._cancel_unrecorded_held(ITEM, {"cancel_recorded": False}) is False
    assert executor._cancel_unrecorded_since == {}


def test_cancel_unrecorded_still_holds_for_a_create_effect_without_a_confirmed_stop(tmp_path):
    executor = make_executor(Control({"effects": CREATE}), tmp_path)
    assert executor._cancel_unrecorded_held(ITEM, {"cancel_recorded": False}) is True
    confirmed = {"cancel_recorded": False, "provider_stop_confirmed": True}
    assert executor._cancel_unrecorded_held(ITEM, confirmed) is False
