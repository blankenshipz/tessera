"""Audited cleanup of stopped Salad groups: fakes only, no network, no live provider."""

import copy
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from test_remote_execution_routing import command, dispatch
from test_salad_proxy import fixture as proxy_fixture

from tessera_control import salad_cleanup_cli, salad_group_cleanup
from tessera_control.remote_execution_models import ProviderReadback
from tessera_control.salad_group_cleanup import (
    AUDIT_EVENT,
    ELIGIBLE,
    GroupIdentityMismatch,
    GroupObservation,
    ProviderError,
    RouteSnapshot,
    SaladGroupCleanup,
    evaluate_route,
)
from tessera_control.store import DATABASE_NAME, Conflict, Forbidden, NotFound

T0 = datetime(2026, 1, 1, tzinfo=UTC)
ZERO = dict(allocating_count=0, creating_count=0, running_count=0, stopping_count=0)


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


# ---- fakes ---------------------------------------------------------------------------


class Crash(BaseException):
    """Simulates a process crash: not an Exception, so no handler may swallow it."""


class FakeProvider:
    """Exact-name groups. There is deliberately no usable listing."""

    live = False  # a fake, never a real provider

    def __init__(self):
        self.on_read = None
        self.groups, self.reads, self.deletes = {}, [], []
        self.list_calls = 0
        self.remove_on_delete = True
        self.delete_error = None
        self.read_error = set()
        self.mismatch = set()

    def add(self, name, ident, *, state="stopped", counts=None, pending=False, instances=()):
        self.groups[name] = dict(
            id=ident,
            state=state,
            counts=counts or dict(ZERO),
            pending=pending,
            instances=tuple(instances),
        )

    def list_groups(self):
        self.list_calls += 1
        raise AssertionError("cleanup must never list the provider's groups")

    @staticmethod
    def _name(state):
        return state["proposal"]["selection"]["salad"]["group"]

    def read_group(self, state):
        name = self._name(state)
        self.reads.append(name)
        if self.on_read is not None:
            self.on_read(len(self.reads))
        if name in self.read_error:
            raise ProviderError("provider_unavailable")
        if name in self.mismatch:
            raise GroupIdentityMismatch("mismatch")
        g = self.groups.get(name)
        if g is None:
            return GroupObservation(present=False, response_status=404)
        return GroupObservation(
            present=True,
            name=name,
            provider_id=g["id"],
            state=g["state"],
            instance_counts=dict(g["counts"]),
            pending_change=g["pending"],
            instances=g["instances"],
            response_status=200,
        )

    def delete(self, state):
        name = self._name(state)
        self.deletes.append(name)
        if self.delete_error is not None:
            raise self.delete_error
        g = self.groups.get(name)
        if self.remove_on_delete and g is not None and g["id"] == state["provider_id"]:
            del self.groups[name]
        return 202


def make_state(i, **changes):
    operation = f"{i:032x}"
    state = dict(
        id=f"route_{i}",
        org_id="org",
        project_id="proj",
        task_id="task",
        proposal=dict(
            operation_id=operation,
            selection=dict(target="salad_container", salad=dict(group="attempt-" + operation)),
        ),
        proposal_digest="d" * 64,
        revision=3,
        state="stopped",
        desired="stop",
        effects=[],
        receipts=[],
        provider_id=f"{i + 1000:032x}",
        allocation_id=None,
        reallocated=False,
        provider_status="stopped",
        termination_confirmed=True,
        cancellation="confirmed",
        cost_cents=None,
    )
    state.update(changes)
    return state


class StubStore:
    """In-memory route store with the begin/record invariants the cleanup relies on."""

    def __init__(self, entries):
        # entries: list of (state, recorded_at datetime)
        self.states = {s["id"]: copy.deepcopy(s) for s, _ in entries}
        self.at = {s["id"]: iso(at) for s, at in entries}
        self.audit, self.begins = [], []

    def route_snapshots(self, worker):
        rows = [RouteSnapshot(copy.deepcopy(s), self.at[i]) for i, s in self.states.items()]
        return sorted(rows, key=lambda r: (r.recorded_at, r.state["id"]))

    def begin(self, worker, ident, action, *, expected_revision=None):
        assert action == "delete"
        state = self.states[ident]
        if expected_revision is not None and state["revision"] != expected_revision:
            raise Conflict("Route changed since it was evaluated.")
        if not state["termination_confirmed"] or state["desired"] != "stop":
            raise Conflict("Confirmed termination and stop intent required before deletion.")
        if any(e["action"] == "delete" for e in state["effects"]):
            raise Conflict("Effect already attempted; read-only reconciliation required.")
        self.begins.append(ident)
        state["effects"].append(dict(id=f"{len(self.begins):032x}", action="delete"))
        state["state"] = "unknown"
        state["revision"] += 1
        return copy.deepcopy(state)

    def record(self, worker, ident, effect_id, readback):
        body = ProviderReadback.model_validate(readback).model_dump(mode="json")
        state = self.states[ident]
        assert state["effects"][-1]["id"] == effect_id
        state["state"], state["provider_status"] = body["state"], body["state"]
        state["termination_confirmed"] = body["termination_confirmed"]
        state["revision"] += 1
        state["cost_cents"] = body["cost_cents"]
        state["receipts"].append(dict(effect_id=effect_id, **body))

    def record_group_cleanup(self, worker, ident, audit):
        self.audit.append(dict(audit, route_id=ident))


WORKER = dict(id="worker", org_id="org")


def stub(entries, provider, *, min_age=0, now=None, **kw):
    store = StubStore(entries)
    now = now or T0 + timedelta(days=2)
    cleanup = SaladGroupCleanup(
        store, WORKER, provider, min_age_seconds=min_age, clock=lambda: now.timestamp(), **kw
    )
    return store, cleanup


def routes(n, provider, *, start=1, **changes):
    entries = []
    for i in range(start, start + n):
        state = make_state(i, **changes)
        provider.add(state["proposal"]["selection"]["salad"]["group"], state["provider_id"])
        entries.append((state, T0 + timedelta(minutes=i)))
    return entries


# ---- static eligibility ----------------------------------------------------------------


def _mut(**changes):
    return lambda s: s.update(changes)


def _unbound(s):
    s["provider_id"] = None


def _other_group(s):
    s["proposal"]["selection"]["salad"]["group"] = "attempt-" + "9" * 32


def _foreign_target(s):
    s["proposal"]["selection"]["target"] = "kubernetes"


def _delete_attempted(s):
    s["effects"].append(dict(id="e" * 32, action="delete"))
    s["state"] = "stopped"


SKIPS = [
    (_foreign_target, "not_salad_container_target"),
    (_mut(state="deleted"), "already_cleaned"),
    (_delete_attempted, "delete_already_attempted"),
    (_mut(state="unknown"), "unresolved_effect"),
    (_mut(state="running"), "not_stopped"),
    (_mut(termination_confirmed=False), "termination_not_confirmed"),
    (_mut(desired="run"), "stop_not_desired"),
    (_mut(reallocated=True), "reallocated"),
    (_unbound, "no_bound_provider_id"),
    (_mut(provider_id="not-a-uuid"), "provider_id_malformed"),
    (_other_group, "group_name_mismatch"),
]


def test_eligible_baseline_and_every_static_skip_reason():
    now = (T0 + timedelta(days=1)).timestamp()
    snap = RouteSnapshot(make_state(1), iso(T0))
    assert evaluate_route(snap, now=now, min_age_seconds=3600) == ELIGIBLE
    seen = set()
    for mutate, reason in SKIPS:
        state = make_state(1)
        mutate(state)
        got = evaluate_route(RouteSnapshot(state, iso(T0)), now=now, min_age_seconds=3600)
        assert got == reason
        seen.add(reason)
    recent = evaluate_route(
        RouteSnapshot(make_state(1), iso(T0)), now=T0.timestamp() + 5, min_age_seconds=3600
    )
    assert recent == "stop_too_recent"
    # Every static reason token except the server-scope one is exercised here.
    assert seen | {"stop_too_recent", "outside_server_scope"} == set(
        salad_group_cleanup.STATIC_REASONS
    )


def test_min_age_boundary_uses_the_injected_clock():
    snap = RouteSnapshot(make_state(1), iso(T0))
    at = T0.timestamp()
    assert evaluate_route(snap, now=at + 3599, min_age_seconds=3600) == "stop_too_recent"
    assert evaluate_route(snap, now=at + 3600, min_age_seconds=3600) == ELIGIBLE
    assert evaluate_route(snap, now=at, min_age_seconds=0) == ELIGIBLE


def test_name_only_and_unbound_routes_are_never_read_or_deleted():
    provider = FakeProvider()
    unbound = make_state(1, provider_id=None)  # lost create response: no bound id
    provider.add(unbound["proposal"]["selection"]["salad"]["group"], "a" * 32)
    renamed = make_state(2)
    renamed["proposal"]["selection"]["salad"]["group"] = "attempt-" + "8" * 32
    provider.add("attempt-" + "8" * 32, renamed["provider_id"])
    store, cleanup = stub([(unbound, T0), (renamed, T0)], provider)
    result = cleanup.apply(max_groups=5)
    assert provider.reads == [] and provider.deletes == []
    assert {d["reason"] for d in result["decisions"]} == {
        "no_bound_provider_id",
        "group_name_mismatch",
    }
    assert store.begins == []


# ---- foreign groups untouched by construction -------------------------------------------


def test_foreign_groups_are_untouched_and_never_listed():
    provider = FakeProvider()
    ours = routes(2, provider)  # A, B: eligible and verified
    busy = make_state(3)
    provider.add(
        busy["proposal"]["selection"]["salad"]["group"],
        busy["provider_id"],
        counts=dict(ZERO, running_count=1),
    )
    other_id = make_state(4)  # recorded id differs from the provider's group id
    provider.add(other_id["proposal"]["selection"]["salad"]["group"], "f" * 32)
    entries = ours + [(busy, T0 + timedelta(minutes=3)), (other_id, T0 + timedelta(minutes=4))]
    # Groups Tessera has no record of, including one that looks like ours.
    provider.add("attempt-" + "c" * 32, "1" * 32)
    provider.add("someone-elses-group", "2" * 32)
    before = copy.deepcopy(provider.groups)
    store, cleanup = stub(entries, provider)
    result = cleanup.apply(max_groups=10)
    assert provider.deletes == [
        make_state(1)["proposal"]["selection"]["salad"]["group"],
        make_state(2)["proposal"]["selection"]["salad"]["group"],
    ]
    assert provider.list_calls == 0
    assert "attempt-" + "c" * 32 not in provider.reads
    assert "someone-elses-group" not in provider.reads
    assert set(provider.groups) == set(before) - set(provider.deletes)
    assert all(provider.groups[k] == before[k] for k in provider.groups)
    for ident in ("route_3", "route_4"):
        assert store.states[ident]["effects"] == []  # never began a delete
    reasons = {d["route_id"]: d["reason"] for d in result["decisions"]}
    assert reasons["route_3"] == "group_has_instances"
    assert reasons["route_4"] == "identity_mismatch"
    assert result["run_stopped"] == "identity_mismatch"
    audited = {a["route_id"]: a["reason"] for a in store.audit if a["phase"] == "skip"}
    assert audited == {"route_3": "group_has_instances", "route_4": "identity_mismatch"}


# ---- apply-time verification ----------------------------------------------------------


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (dict(counts=dict(ZERO, creating_count=1)), "group_has_instances"),
        (dict(instances=({"state": "running"},)), "group_has_instances"),
        (dict(pending=True), "group_pending_change"),
        (dict(state="running"), "group_not_stopped"),
    ],
)
def test_drift_at_read_skips_with_audit_and_never_begins(setup, reason):
    provider = FakeProvider()
    (entry,) = routes(1, provider)
    name = entry[0]["proposal"]["selection"]["salad"]["group"]
    provider.add(name, entry[0]["provider_id"], **setup)
    store, cleanup = stub([entry], provider)
    result = cleanup.apply()
    assert provider.deletes == [] and store.begins == []
    assert result["decisions"][0]["reason"] == reason
    assert [a["phase"] for a in store.audit] == ["skip"]
    assert store.audit[0]["delete_status"] == "not_sent"


def test_group_absent_at_read_skips_without_delete():
    provider = FakeProvider()
    (entry,) = routes(1, provider)
    provider.groups.clear()
    store, cleanup = stub([entry], provider)
    assert cleanup.apply()["decisions"][0]["reason"] == "group_absent_at_read"
    assert provider.deletes == [] and store.begins == []


def test_provider_read_error_stops_the_whole_run():
    provider = FakeProvider()
    entries = routes(3, provider)
    provider.read_error.add(entries[0][0]["proposal"]["selection"]["salad"]["group"])
    store, cleanup = stub(entries, provider)
    result = cleanup.apply(max_groups=10)
    assert result["run_stopped"] == "provider_read_failed"
    assert provider.deletes == [] and len(provider.reads) == 1
    assert [d["reason"] for d in result["decisions"]] == [
        "provider_read_failed",
        "run_stopped",
        "run_stopped",
    ]


def test_first_delete_error_stops_run_and_is_never_retried():
    provider = FakeProvider()
    provider.delete_error = RuntimeError("boom")
    entries = routes(3, provider)
    store, cleanup = stub(entries, provider)
    result = cleanup.apply(max_groups=10)
    assert result["run_stopped"] == "delete_failed" and len(provider.deletes) == 1
    assert store.states["route_1"]["state"] == "unknown"  # unresolved, not retried
    again = cleanup.apply(max_groups=10)
    assert again["decisions"][0]["reason"] == "delete_already_attempted"
    assert provider.deletes.count(make_state(1)["proposal"]["selection"]["salad"]["group"]) == 1


def test_max_groups_bound_and_deterministic_order():
    provider = FakeProvider()
    entries = routes(4, provider)
    shuffled = [entries[2], entries[0], entries[3], entries[1]]
    store, cleanup = stub(shuffled, provider)
    result = cleanup.apply(max_groups=2)
    names = [e[0]["proposal"]["selection"]["salad"]["group"] for e in entries]
    assert provider.deletes == names[:2]
    assert [d["route_id"] for d in result["decisions"]] == [f"route_{i}" for i in (1, 2, 3, 4)]
    assert result["counts"]["deleted_confirmed"] == 2 and result["counts"]["deferred"] == 2
    cleanup.apply(max_groups=2)
    assert provider.deletes == names  # next run continues, never repeats


# ---- one delete only; receipts; audit ordering ----------------------------------------


def test_one_delete_per_route_and_unconfirmed_outcome_is_flagged_not_retried():
    provider = FakeProvider()
    provider.remove_on_delete = False  # 202 accepted but the group is still present
    (entry,) = routes(1, provider)
    store, cleanup = stub([entry], provider)
    result = cleanup.apply()
    assert result["decisions"][0]["decision"] == "delete_unconfirmed"
    outcome = [a for a in store.audit if a["phase"] == "outcome"][0]
    assert outcome["confirmed_gone"] is False and outcome["needs_reconciliation"] is True
    receipt = store.states["route_1"]["receipts"][-1]
    # An accepted but unreconciled delete never looks stopped.
    assert receipt["state"] == "unknown" and receipt["termination_confirmed"] is False
    assert receipt["proxy_reason"] == "accepted_pending"
    assert store.states["route_1"]["state"] == "unknown"
    for _ in range(3):
        cleanup.apply()
    assert len(provider.deletes) == 1
    with pytest.raises(Conflict):
        store.begin(WORKER, "route_1", "delete")


def test_receipt_and_audit_say_synthetic_and_claim_no_relief():
    provider = FakeProvider()
    (entry,) = routes(1, provider)
    store, cleanup = stub([entry], provider)
    cleanup.apply()
    receipt = store.states["route_1"]["receipts"][-1]
    assert receipt["provenance"] == "synthetic" and receipt["state"] == "deleted"
    assert receipt["cost_cents"] is None and receipt["usage_status"] == "unknown"
    assert all(a.get("readback_source") == "non_live_port" for a in store.audit)
    assert receipt["termination_confirmed"] is True and receipt["response_status"] == 404
    assert [a["phase"] for a in store.audit] == ["intent", "outcome"]
    assert store.audit[0]["effect_id"] == receipt["effect_id"]
    assert all(
        a["liability_released"] is False
        and a["cost_relief_claimed"] is False
        and a["receipt_marker"] == "cleanup of a stopped zero-instance group"
        for a in store.audit
    )
    assert store.audit[1]["confirmed_gone"] is True and store.audit[1]["dry_run"] is False


# ---- dry run ---------------------------------------------------------------------------


class NoProvider:
    def __getattr__(self, name):
        raise AssertionError("dry run touched the provider")


def test_plan_makes_no_provider_call_and_no_write_by_default():
    provider = FakeProvider()
    entries = routes(2, provider) + [(make_state(5, state="running"), T0)]
    store, cleanup = stub(entries, NoProvider())
    before = copy.deepcopy(store.states)
    plan = cleanup.plan()
    assert store.states == before and store.audit == [] and store.begins == []
    assert plan["dry_run"] is True and plan["provider_calls"] == 0
    assert plan["provider_readback_performed"] is False and "NOT performed" in plan["statement"]
    assert plan["counts"]["eligible"] == 2 and plan["counts"]["reasons"]["not_stopped"] == 1
    recorded = cleanup.plan(record_plan=True)
    assert recorded["recorded"] is True and len(store.audit) == 3
    assert all(a["dry_run"] is True and a["phase"] == "plan" for a in store.audit)


# ---- real store: receipts, audit rows, crash safety, read-only ------------------------


def stopped_route(tmp_path):
    h = proxy_fixture(tmp_path)
    dispatch(h, "create")
    dispatch(h, "start")
    command(h, "cancel")
    dispatch(h, "stop")
    h.transport.state = "stopped"
    assert dispatch(h, "observe")["termination_confirmed"]
    provider = FakeProvider()
    provider.add(h.route["proposal"]["selection"]["salad"]["group"], h.route["provider_id"])
    h.provider = provider
    h.cleanup = SaladGroupCleanup(h.store, h.worker, provider, min_age_seconds=0)
    return h


def events(h, event_type):
    with sqlite3.connect(h.store.database_path) as db:
        rows = db.execute(
            "SELECT payload_json FROM events WHERE event_type=? ORDER BY cursor", (event_type,)
        ).fetchall()
    return [json.loads(r[0]) for r in rows]


def test_real_store_delete_confirmed_receipt_and_intent_before_outcome(tmp_path):
    h = stopped_route(tmp_path)
    result = h.cleanup.apply()
    assert result["decisions"][0]["decision"] == "deleted"
    route = h.store.get(h.worker, h.route["id"])
    assert route["state"] == "deleted" and route["provider_status"] == "deleted"
    assert [e["action"] for e in route["effects"]].count("delete") == 1
    receipt = route["receipts"][-1]
    assert receipt["provenance"] == "synthetic" and receipt["cost_cents"] is None
    assert receipt["termination_confirmed"] is True and route["cost_cents"] is None
    assert receipt["effect_id"] == route["effects"][-1]["id"]
    assert receipt["request_id"] == receipt["effect_id"] and receipt["response_status"] == 404
    audit = events(h, AUDIT_EVENT)
    assert [a["phase"] for a in audit] == ["intent", "outcome"]
    assert audit[0]["provider_id"] == route["provider_id"]
    assert audit[0]["group"] == route["proposal"]["selection"]["salad"]["group"]
    assert audit[0]["route_digest"] == route["proposal_digest"]
    assert audit[1]["confirmed_gone"] is True and audit[1]["effect_id"] == receipt["effect_id"]
    # Intent is committed before the provider delete, outcome after the receipt.
    with sqlite3.connect(h.store.database_path) as db:
        order = [
            r[0]
            for r in db.execute(
                "SELECT event_type FROM events WHERE event_type IN (?,?) ORDER BY cursor",
                (AUDIT_EVENT, "remote_execution.state"),
            )
        ]
    tail = order[-4:]
    assert tail == [
        "remote_execution.state",  # begin: the durable intent commit
        "remote.group_cleanup_recorded",  # intent audit, carrying the effect id
        "remote_execution.state",  # receipt
        "remote.group_cleanup_recorded",  # outcome audit
    ]
    assert audit[0]["effect_id"] == receipt["effect_id"]
    assert h.cleanup.apply()["decisions"][0]["reason"] == "already_cleaned"
    assert len(h.provider.deletes) == 1


def test_real_store_crash_after_intent_leaves_evidence_and_never_redeletes(tmp_path):
    h = stopped_route(tmp_path)
    h.provider.delete_error = Crash()
    with pytest.raises(Crash):
        h.cleanup.apply()
    route = h.store.get(h.worker, h.route["id"])
    assert route["state"] == "unknown" and route["receipts"][-1]["state"] != "deleted"
    assert [a["phase"] for a in events(h, AUDIT_EVENT)] == ["intent"]
    h.provider.delete_error = None
    again = h.cleanup.apply()
    assert again["decisions"][0]["reason"] == "delete_already_attempted"
    assert h.provider.deletes == [route["proposal"]["selection"]["salad"]["group"]]
    with pytest.raises(Conflict):
        h.store.begin(h.worker, h.route["id"], "delete")


def test_real_store_unconfirmed_outcome_flags_route_without_second_delete(tmp_path):
    h = stopped_route(tmp_path)
    h.provider.remove_on_delete = False
    result = h.cleanup.apply()
    assert result["decisions"][0]["decision"] == "delete_unconfirmed"
    route = h.store.get(h.worker, h.route["id"])
    assert route["state"] == "unknown" and route["termination_confirmed"] is False
    assert route["receipts"][-1]["proxy_reason"] == "accepted_pending"
    assert route["receipts"][-1]["state"] == "unknown"
    assert events(h, AUDIT_EVENT)[-1]["needs_reconciliation"] is True
    outcome = events(h, AUDIT_EVENT)[-1]
    assert outcome["delete_status"] == "accepted_pending" and outcome["confirmed_gone"] is False
    # The route cannot be started with a delete pending, nor deleted a second time.
    with pytest.raises(Conflict):
        h.store.begin(h.worker, h.route["id"], "start", preflight_digest="f" * 64)
    with pytest.raises(Conflict):
        h.store.begin(h.worker, h.route["id"], "delete")
    h.cleanup.apply()
    h.cleanup.apply()
    assert len(h.provider.deletes) == 1


def test_real_store_foreign_group_with_matching_name_shape_is_never_touched(tmp_path):
    h = stopped_route(tmp_path)
    h.provider.add("attempt-" + "d" * 32, "3" * 32)
    h.cleanup.apply()
    assert h.provider.list_calls == 0 and "attempt-" + "d" * 32 in h.provider.groups
    assert h.provider.reads == [h.route["proposal"]["selection"]["salad"]["group"]] * 3


def test_real_store_dry_run_uses_readonly_connection_and_changes_nothing(tmp_path, monkeypatch):
    h = stopped_route(tmp_path)
    uris = []
    real = sqlite3.connect

    def spy(target, *a, **kw):
        uris.append(str(target))
        return real(target, *a, **kw)

    monkeypatch.setattr(salad_group_cleanup.sqlite3, "connect", spy)
    cleanup = SaladGroupCleanup(h.store, h.worker, NoProvider(), min_age_seconds=0)
    count = len(events(h, "remote_execution.state"))
    total = (
        sqlite3.connect(h.store.database_path).execute("SELECT count(*) FROM events").fetchone()[0]
    )
    mtimes = {p: os.stat(p).st_mtime_ns for p in h.store.database_path.parent.glob("*sqlite3*")}
    uris.clear()
    plan = cleanup.plan()
    seen = list(uris)
    assert plan["counts"]["eligible"] == 1 and plan["provider_calls"] == 0
    assert seen and all(u.endswith("?mode=ro") and u.startswith("file:") for u in seen)
    assert len(events(h, "remote_execution.state")) == count
    after = (
        sqlite3.connect(h.store.database_path).execute("SELECT count(*) FROM events").fetchone()[0]
    )
    assert after == total
    assert mtimes == {p: os.stat(p).st_mtime_ns for p in mtimes}


def test_apply_and_plan_refuse_to_run_inside_a_store_transaction(tmp_path):
    h = stopped_route(tmp_path)
    inside = []

    def attempt(db, live):
        for call in (h.cleanup.apply, h.cleanup.plan):
            with pytest.raises(Conflict):
                call()
            inside.append(call.__name__)
        return 200, {}

    h.store._mutate(h.worker, "txn", "POST", "/x", "0" * 64, attempt, audience="worker")
    assert inside == ["apply", "plan"] and h.provider.deletes == []


# ---- live calls are impossible -----------------------------------------------------------


class LiveLike:
    """Stands in for the real HTTP transport: no `live` mark (or a true one)."""

    def __init__(self, **attrs):
        self.calls = []
        self.__dict__.update(attrs)

    def send(self, *args):
        self.calls.append(args)
        raise AssertionError("a live-like transport must never be called")


def test_no_adapter_over_the_real_http_transport_exists():
    from tessera_control.salad_http import SaladHTTPTransport
    from tessera_control.salad_proxy import FakeSaladTransport

    assert not hasattr(salad_group_cleanup, "TransportGroupPort")
    assert not hasattr(SaladHTTPTransport, "live")
    assert FakeSaladTransport.live is False


@pytest.mark.parametrize("attrs", [{}, dict(live=True), dict(live=None), dict(live="false")])
def test_apply_refuses_ports_not_marked_non_live_before_any_read(attrs):
    for port in (LiveLike(**attrs),):
        store, cleanup = stub([(make_state(1), T0)], port)
        with pytest.raises(ProviderError):
            cleanup.apply()
        assert port.calls == [] and store.begins == [] and store.audit == []


def server_stopped(tmp_path, monkeypatch):
    from test_salad_server import fixture as server_fixture
    from test_salad_server import preflight, wire

    h = server_fixture(tmp_path, monkeypatch)
    wire(h, *preflight(h, absent=True), (201, h.provider_group))
    dispatch(h, "create")
    wire(h, *preflight(h, absent=False), (200, h.provider_group), (202, b""))
    dispatch(h, "start")
    command(h, "cancel")
    wire(h, (200, h.provider_group), (200, h.provider_group), (202, b""))
    dispatch(h, "stop")
    wire(h, (200, h.provider_group), (200, h.provider_group), (200, {"instances": []}))
    assert dispatch(h, "observe")["termination_confirmed"]
    return h


def test_server_hook_is_disabled_by_default_and_needs_its_own_store_and_worker(
    tmp_path, monkeypatch
):
    from test_salad_server import fixture as server_fixture

    h = server_fixture(tmp_path, monkeypatch)
    assert h.server.cleanup_settled() == dict(enabled=False, reason="cleanup_not_configured")
    assert h.server.cleanup_settled(port=FakeProvider())["enabled"] is False
    monkeypatch.setattr(h.server, "transport", NonLiveTransport())
    foreign = StubStore([])
    other = SaladGroupCleanup(foreign, h.worker, FakeProvider(), min_age_seconds=0)
    with pytest.raises(Conflict):
        h.server.cleanup_settled(other, port=FakeProvider())
    wrong_worker = SaladGroupCleanup(
        h.store, dict(h.worker, id="someone-else"), FakeProvider(), min_age_seconds=0
    )
    with pytest.raises(Conflict):
        h.server.cleanup_settled(wrong_worker, port=FakeProvider())


class NonLiveTransport:
    """A server transport that is explicitly not live; any call is a test failure."""

    live = False

    def __getattr__(self, name):
        raise AssertionError("the hook must never use the server's transport")


def test_server_hook_refuses_against_the_real_http_transport(tmp_path, monkeypatch):
    from test_salad_server import wire

    h = server_stopped(tmp_path, monkeypatch)
    cleanup = SaladGroupCleanup(h.store, h.worker, None, min_age_seconds=0)
    factory = wire(h, (200, h.provider_group), (200, h.provider_group), (202, b""))
    provider = FakeProvider()
    provider.add(h.route["proposal"]["selection"]["salad"]["group"], h.route["provider_id"])
    # Even a perfectly non-live port cannot record a delete on a real server's routes.
    for port in (provider, LiveLike(), LiveLike(live=True), h.server.transport):
        with pytest.raises(ProviderError, match="no activation gate"):
            h.server.cleanup_settled(cleanup, port=port)
    with pytest.raises(ProviderError):
        h.server.cleanup_settled(cleanup)  # no port: still refused first
    assert factory.calls == [] and provider.reads == [] and provider.deletes == []
    route = h.store.get(h.worker, h.route["id"])
    assert [e["action"] for e in route["effects"]].count("delete") == 0
    assert route["state"] == "stopped" and events(h, AUDIT_EVENT) == []


def test_server_hook_needs_an_explicit_non_live_port_even_on_a_non_live_server(
    tmp_path, monkeypatch
):
    h = server_stopped(tmp_path, monkeypatch)
    monkeypatch.setattr(h.server, "transport", NonLiveTransport())
    cleanup = SaladGroupCleanup(h.store, h.worker, None, min_age_seconds=0)
    with pytest.raises(ValueError):
        h.server.cleanup_settled(cleanup)
    for port in (LiveLike(), LiveLike(live=True)):
        with pytest.raises(ProviderError):
            h.server.cleanup_settled(cleanup, port=port)
    assert events(h, AUDIT_EVENT) == []


def test_server_hook_with_a_non_live_server_and_port_marks_the_readback(tmp_path, monkeypatch):
    h = server_stopped(tmp_path, monkeypatch)
    monkeypatch.setattr(h.server, "transport", NonLiveTransport())
    provider = FakeProvider()
    group = h.route["proposal"]["selection"]["salad"]["group"]
    provider.add(group, h.route["provider_id"])
    cleanup = SaladGroupCleanup(h.store, h.worker, None, min_age_seconds=0)
    result = h.server.cleanup_settled(cleanup, port=provider)
    assert result["decisions"][0]["decision"] == "deleted"
    assert provider.deletes == [group]
    route = h.store.get(h.worker, h.route["id"])
    assert route["receipts"][-1]["provenance"] == "synthetic"
    assert route["receipts"][-1]["state"] == "deleted"
    assert {a["readback_source"] for a in events(h, AUDIT_EVENT)} == {"non_live_port"}
    again = h.server.cleanup_settled(cleanup, port=provider)
    assert again["decisions"][0]["reason"] == "already_cleaned"


def test_server_hook_refuses_to_run_inside_a_store_transaction(tmp_path, monkeypatch):
    h = server_stopped(tmp_path, monkeypatch)
    monkeypatch.setattr(h.server, "transport", NonLiveTransport())
    provider = FakeProvider()
    cleanup = SaladGroupCleanup(h.store, h.worker, None, min_age_seconds=0)

    def attempt(db, live):
        with pytest.raises(Conflict):
            h.server.cleanup_settled(cleanup, port=provider)
        return 200, {}

    h.store._mutate(h.worker, "txn2", "POST", "/x", "0" * 64, attempt, audience="worker")
    assert provider.reads == [] and provider.deletes == []


# ---- TOCTOU between the verification read and the delete -------------------------------


def test_revision_change_between_snapshot_and_begin_is_refused_and_sends_no_delete():
    provider = FakeProvider()
    (entry,) = routes(1, provider)
    store, cleanup = stub([entry], provider)

    def human_changes_route(n):
        store.states["route_1"]["revision"] += 1  # e.g. run, start, stop again

    provider.on_read = human_changes_route
    result = cleanup.apply()
    assert result["decisions"][0]["reason"] == "begin_refused"
    assert provider.deletes == [] and store.begins == []
    assert store.states["route_1"]["effects"] == []
    assert [a["phase"] for a in store.audit] == ["skip"]  # no intent without an effect


def test_real_store_revision_bump_refuses_begin_without_a_delete_or_intent(tmp_path):
    h = stopped_route(tmp_path)
    revision = h.store.get(h.worker, h.route["id"])["revision"]
    with pytest.raises(Conflict, match="Route changed"):
        h.store.begin(h.worker, h.route["id"], "observe", expected_revision=revision - 1)
    h.provider.on_read = lambda n: (
        h.store.begin(h.worker, h.route["id"], "observe") if n == 1 else None
    )
    result = h.cleanup.apply()
    assert result["decisions"][0]["reason"] == "begin_refused"
    assert h.provider.deletes == []
    route = h.store.get(h.worker, h.route["id"])
    assert [e["action"] for e in route["effects"]].count("delete") == 0
    assert [a["phase"] for a in events(h, AUDIT_EVENT)] == ["skip"]
    # begin without expected_revision still behaves as before.
    h.provider.on_read = None
    assert h.store.begin(h.worker, h.route["id"], "observe")["effects"][-1]["action"] == "observe"


DRIFTS = [
    (lambda g: g.update(counts=dict(ZERO, running_count=1)), "group_has_instances"),
    (lambda g: g.update(pending=True), "group_pending_change"),
    (lambda g: g.update(state="running"), "group_not_stopped"),
    (lambda g: g.update(id="f" * 32), "identity_mismatch"),
    (lambda g: g.update(instances=({"state": "running"},)), "group_has_instances"),
]


@pytest.mark.parametrize(("drift", "reason"), DRIFTS)
def test_drift_at_the_second_read_sends_no_delete_and_consumes_the_one_shot(drift, reason):
    provider = FakeProvider()
    (entry,) = routes(1, provider)
    group = entry[0]["proposal"]["selection"]["salad"]["group"]
    store, cleanup = stub([entry], provider)
    provider.on_read = lambda n: drift(provider.groups[group]) if n == 2 else None
    result = cleanup.apply()
    assert provider.deletes == []
    assert result["decisions"][0]["decision"] == "delete_aborted_drift"
    assert result["decisions"][0]["reason"] == reason
    assert result["counts"]["delete_aborted_drift"] == 1
    route = store.states["route_1"]
    assert route["state"] == "unknown" and route["receipts"] == []
    assert [e["action"] for e in route["effects"]] == ["delete"]
    assert [a["phase"] for a in store.audit] == ["intent", "outcome"]
    assert store.audit[1]["decision"] == "delete_aborted_drift"
    assert store.audit[1]["delete_status"] == "not_sent" and store.audit[1]["effect_id"]
    drift_reset = provider.groups[group]
    drift_reset.update(state="stopped", pending=False, counts=dict(ZERO), instances=())
    provider.on_read = None
    again = cleanup.apply()
    assert again["decisions"][0]["reason"] == "delete_already_attempted"
    assert provider.deletes == []


@pytest.mark.parametrize("how", ["absent", "error", "mismatch"])
def test_second_read_failure_modes_send_no_delete(how):
    provider = FakeProvider()
    (entry,) = routes(1, provider)
    group = entry[0]["proposal"]["selection"]["salad"]["group"]
    store, cleanup = stub([entry], provider)

    def trip(n):
        if n == 2:
            if how == "absent":
                provider.groups.clear()
            elif how == "error":
                provider.read_error.add(group)
            else:
                provider.mismatch.add(group)

    provider.on_read = trip
    result = cleanup.apply()
    assert provider.deletes == []
    assert result["decisions"][0]["decision"] == "delete_aborted_drift"
    assert store.states["route_1"]["receipts"] == []


# ---- authorization before any provider read -------------------------------------------------


def _clone_state_event(
    h, *, org_id=None, operation_id=None, route_id="route_foreign", project_id=None
):
    with sqlite3.connect(h.store.database_path) as db:
        row = db.execute(
            "SELECT org_id, project_id, task_id, resource_revision, actor_principal_id, "
            "created_at, payload_json FROM events WHERE event_type=? "
            "AND json_extract(payload_json,'$.id')=? ORDER BY cursor DESC LIMIT 1",
            ("remote_execution.state", h.route["id"]),
        ).fetchone()
        payload = json.loads(row[6])
        payload["id"] = route_id
        if project_id:
            payload["project_id"] = project_id
        if operation_id:
            payload["proposal"]["operation_id"] = operation_id
            payload["proposal"]["selection"]["salad"]["group"] = "attempt-" + operation_id
        db.execute(
            "INSERT INTO events (org_id, project_id, task_id, event_type, resource_revision, "
            "actor_principal_id, created_at, payload_json) VALUES (?,?,?,?,?,?,?,?)",
            (
                org_id or row[0],
                project_id or row[1],
                row[2],
                "remote_execution.state",
                row[3],
                row[4],
                row[5],
                json.dumps(payload),
            ),
        )
        return payload


def test_only_the_calling_workers_own_routes_are_read_or_touched(tmp_path):
    h = stopped_route(tmp_path)
    own_group = h.route["proposal"]["selection"]["salad"]["group"]
    other_worker = _clone_state_event(h, operation_id="7" * 32, route_id="route_same_org")
    other_org = _clone_state_event(h, org_id="org_b", operation_id="8" * 32, route_id="route_b")
    for payload in (other_worker, other_org):
        h.provider.add(payload["proposal"]["selection"]["salad"]["group"], payload["provider_id"])
    snapshots = h.store.route_snapshots(h.worker)
    assert [s.state["id"] for s in snapshots] == [h.route["id"]]
    assert snapshots[0].revision == h.store.get(h.worker, h.route["id"])["revision"]
    plan = h.cleanup.plan()
    assert [d["route_id"] for d in plan["decisions"]] == [h.route["id"]]
    result = h.cleanup.apply(max_groups=10)
    assert [d["route_id"] for d in result["decisions"]] == [h.route["id"]]
    assert set(h.provider.reads) == {own_group} and h.provider.deletes == [own_group]
    for group in ("attempt-" + "7" * 32, "attempt-" + "8" * 32):
        assert group in h.provider.groups  # foreign groups untouched


def test_a_route_whose_operation_belongs_to_another_worker_is_never_returned(tmp_path):
    h = stopped_route(tmp_path)
    assert [s.state["id"] for s in h.store.route_snapshots(h.worker)] == [h.route["id"]]
    with sqlite3.connect(h.store.database_path) as db:
        db.execute("UPDATE external_operations SET worker_principal_id='another-worker'")
    assert h.store.route_snapshots(h.worker) == []
    assert h.cleanup.plan(record_plan=True)["decisions"] == []
    assert h.cleanup.apply()["decisions"] == [] and h.provider.reads == []


def test_a_route_in_a_project_the_worker_cannot_access_is_never_read_or_touched(tmp_path):
    h = stopped_route(tmp_path)
    # Same org and the worker's own operation, but recorded under another project.
    clone = _clone_state_event(h, project_id="proj_not_accessible", route_id="route_other_proj")
    h.provider.add(clone["proposal"]["selection"]["salad"]["group"], clone["provider_id"])
    assert [s.state["id"] for s in h.store.route_snapshots(h.worker)] == [h.route["id"]]
    result = h.cleanup.apply(max_groups=10)
    assert [d["route_id"] for d in result["decisions"]] == [h.route["id"]]
    assert h.provider.reads.count(h.route["proposal"]["selection"]["salad"]["group"]) == 3
    assert len(h.provider.reads) == 3 and len(h.provider.deletes) == 1


FORGERIES = [
    dict(liability_released=True),
    dict(cost_relief_claimed=True),
    dict(confirmed_gone="yes"),
    dict(readback_source="live_provider"),
    dict(smuggled="x"),
    dict(decision="Deleted Everything"),
    dict(reason="x" * 200),
    dict(effect_id="not-hex"),
    dict(delete_status="deleted"),
    dict(receipt_marker="something else"),
    dict(min_age_seconds=-1),
    dict(marker_sha256="zz"),
]


def _valid_audit(**changes):
    return dict(
        phase="skip",
        decision="skipped",
        reason="group_has_instances",
        delete_status="not_sent",
        confirmed_gone=None,
        dry_run=False,
        receipt_marker="cleanup of a stopped zero-instance group",
        liability_released=False,
        cost_relief_claimed=False,
        min_age_seconds=0,
        **changes,
    )


@pytest.mark.parametrize("forged", FORGERIES)
def test_a_forged_cleanup_audit_payload_is_refused(tmp_path, forged):
    h = stopped_route(tmp_path)
    before = len(events(h, AUDIT_EVENT))
    with pytest.raises(Conflict, match="unsupported shape"):
        h.store.record_group_cleanup(h.worker, h.route["id"], dict(_valid_audit(), **forged))
    assert len(events(h, AUDIT_EVENT)) == before


def test_valid_cleanup_audit_payloads_pass_and_cannot_claim_relief(tmp_path):
    h = stopped_route(tmp_path)
    out = h.store.record_group_cleanup(h.worker, h.route["id"], _valid_audit())
    assert out["liability_released"] is False and out["cost_relief_claimed"] is False
    full = _valid_audit(
        effect_id="a" * 32,
        needs_reconciliation=True,
        readback_source="non_live_port",
        marker_sha256="b" * 64,
    )
    full.update(phase="outcome", delete_status="sent", confirmed_gone=False)
    assert h.store.record_group_cleanup(h.worker, h.route["id"], full)["delete_status"] == "sent"
    h.cleanup.apply()  # the cleanup's own audits all validate
    assert {a["phase"] for a in events(h, AUDIT_EVENT)} >= {"skip", "outcome", "intent"}


def test_apply_tolerates_store_authorization_failures_per_route():
    class Denying(StubStore):
        def begin(self, worker, ident, action, **kw):
            if ident == "route_2":
                raise Forbidden("Wrong dispatcher worker.")
            return super().begin(worker, ident, action, **kw)

        def record_group_cleanup(self, worker, ident, audit):
            if ident == "route_2":
                raise Forbidden("Wrong dispatcher worker.")
            return super().record_group_cleanup(worker, ident, audit)

    provider = FakeProvider()
    entries = routes(3, provider)
    store = Denying(entries)
    cleanup = SaladGroupCleanup(
        store,
        WORKER,
        provider,
        min_age_seconds=0,
        clock=lambda: (T0 + timedelta(days=2)).timestamp(),
    )
    result = cleanup.apply(max_groups=10)
    reasons = [d["reason"] for d in result["decisions"]]
    # A revoked credential stops the run: the route after the refusal is not read.
    assert reasons == ["confirmed_gone", "not_authorized", "run_stopped"]
    assert result["run_stopped"] == "not_authorized"
    assert result["decisions"][1]["audited"] is False
    plan = cleanup.plan(record_plan=True)  # route_2's audit is refused; the plan completes
    assert plan["recorded"] is False and plan["unrecorded"] == 1
    assert len(plan["decisions"]) == 3


def _two_route_cleanup(store_cls):
    provider = FakeProvider()
    store = store_cls(routes(2, provider))
    cleanup = SaladGroupCleanup(
        store,
        WORKER,
        provider,
        min_age_seconds=0,
        clock=lambda: (T0 + timedelta(days=2)).timestamp(),
    )
    return provider, store, cleanup


@pytest.mark.parametrize(
    "exc", [Conflict("Late/out-of-order receipt"), Forbidden("x"), NotFound("x")]
)
def test_store_refusal_at_receipt_time_reports_delete_unrecorded_never_skipped(exc):
    class RecordDenied(StubStore):
        def record(self, *a, **kw):
            raise exc

    provider, store, cleanup = _two_route_cleanup(RecordDenied)
    result = cleanup.apply(max_groups=10)  # must not raise
    first, second = result["decisions"]
    assert first["decision"] == "delete_unrecorded" and first["reason"] == "receipt_not_recorded"
    assert second["reason"] == "run_stopped"
    assert result["run_stopped"] == "delete_unrecorded"
    assert result["counts"]["delete_unrecorded"] == 1
    assert len(provider.deletes) == 1  # the second route is never started
    (outcome,) = [a for a in store.audit if a["phase"] == "outcome"]
    assert outcome["delete_status"] == "sent" and outcome["needs_reconciliation"] is True
    assert store.states["route_1"]["state"] == "unknown"


def test_store_refusal_at_the_outcome_audit_reports_delete_unrecorded():
    class AuditOutcomeDenied(StubStore):
        def record_group_cleanup(self, worker, ident, audit):
            if audit["phase"] == "outcome":
                raise Forbidden("revoked")
            return super().record_group_cleanup(worker, ident, audit)

    provider, store, cleanup = _two_route_cleanup(AuditOutcomeDenied)
    result = cleanup.apply(max_groups=10)
    first = result["decisions"][0]
    assert first["decision"] == "delete_unrecorded" and first["reason"] == "outcome_not_audited"
    assert first["audited"] is False and result["run_stopped"] == "delete_unrecorded"
    assert len(provider.deletes) == 1
    assert store.states["route_1"]["receipts"][-1]["state"] == "deleted"


def test_receipt_conflict_from_a_real_interleaved_observe_is_reported_not_raised(tmp_path):
    h = stopped_route(tmp_path)
    real = h.store.record

    def interleave(worker, ident, effect_id, readback):
        h.store.begin(h.worker, ident, "observe")  # dispatch ran between begin and record
        return real(worker, ident, effect_id, readback)

    h.store.record = interleave
    result = h.cleanup.apply()
    assert result["decisions"][0]["decision"] == "delete_unrecorded"
    assert h.provider.deletes != [] and len(h.provider.deletes) == 1
    outcome = events(h, AUDIT_EVENT)[-1]
    assert outcome["phase"] == "outcome" and outcome["delete_status"] == "sent"
    assert outcome["needs_reconciliation"] is True
    h.store.record = real
    again = h.cleanup.apply()
    assert again["decisions"][0]["reason"] == "delete_already_attempted"
    assert len(h.provider.deletes) == 1


@pytest.mark.parametrize("exc", [Conflict("c"), Forbidden("f"), NotFound("n")])
def test_plan_record_tolerates_conflict_forbidden_and_notfound(exc):
    class Refusing(StubStore):
        def record_group_cleanup(self, worker, ident, audit):
            if ident == "route_1":
                raise exc
            return super().record_group_cleanup(worker, ident, audit)

    provider = FakeProvider()
    store = Refusing(routes(3, provider))
    cleanup = SaladGroupCleanup(
        store, WORKER, provider, min_age_seconds=0, clock=lambda: T0.timestamp() + 10**6
    )
    plan = cleanup.plan(record_plan=True)
    assert plan["recorded"] is False and plan["unrecorded"] == 1
    assert [a["route_id"] for a in store.audit] == ["route_2", "route_3"]


def test_skip_audit_conflict_and_unexpected_errors_keep_prior_decisions():
    class Flaky(StubStore):
        def record_group_cleanup(self, worker, ident, audit):
            if ident == "route_2" and audit["phase"] == "intent":
                raise RuntimeError("unexpected store failure")
            return super().record_group_cleanup(worker, ident, audit)

    provider, store, cleanup = _two_route_cleanup(Flaky)
    result = cleanup.apply(max_groups=10)  # must not raise
    first, second = result["decisions"]
    assert first["decision"] == "deleted"  # the earlier delete stays reported
    assert second["decision"] == "skipped" and second["reason"] == "unexpected_error"
    assert second["needs_reconciliation"] is True
    assert result["run_stopped"] == "unexpected_error"

    class SkipAuditConflict(StubStore):
        def begin(self, worker, ident, action, **kw):
            if ident == "route_2":
                raise Conflict("raced")
            return super().begin(worker, ident, action, **kw)

        def record_group_cleanup(self, worker, ident, audit):
            if audit["phase"] == "skip":
                raise Conflict("unsupported shape")
            return super().record_group_cleanup(worker, ident, audit)

    provider, store, cleanup = _two_route_cleanup(SkipAuditConflict)
    result = cleanup.apply(max_groups=10)  # must not raise
    assert result["decisions"][0]["decision"] == "deleted"
    assert result["decisions"][1]["reason"] == "begin_refused"
    assert result["decisions"][1]["audited"] is False


def test_begin_refusal_for_authorization_stops_the_run():
    class Revoked(StubStore):
        def begin(self, worker, ident, action, **kw):
            raise Forbidden("credential revoked")

    provider, store, cleanup = _two_route_cleanup(Revoked)
    result = cleanup.apply(max_groups=10)
    first, second = result["decisions"]
    assert first["reason"] == "not_authorized" and result["run_stopped"] == "not_authorized"
    assert second["reason"] == "run_stopped"
    assert provider.deletes == []


def test_confirmed_gone_is_audited_as_sent_and_unconfirmed_as_accepted_pending():
    provider, store, cleanup = _two_route_cleanup(StubStore)
    cleanup.apply(max_groups=1)
    (outcome,) = [a for a in store.audit if a["phase"] == "outcome"]
    assert outcome["confirmed_gone"] is True and outcome["delete_status"] == "sent"


def test_plan_recording_skips_non_salad_routes():
    provider = FakeProvider()
    entries = routes(2, provider)
    _foreign_target(entries[1][0])
    store = StubStore(entries)
    cleanup = SaladGroupCleanup(
        store, WORKER, provider, min_age_seconds=0, clock=lambda: T0.timestamp() + 10**6
    )
    plan = cleanup.plan(record_plan=True)
    assert len(plan["decisions"]) == 2 and plan["recorded"] is True
    assert [a["route_id"] for a in store.audit] == ["route_1"]


def test_real_store_accepts_and_records_sent_for_a_confirmed_delete(tmp_path):
    h = stopped_route(tmp_path)
    result = h.cleanup.apply()
    assert result["decisions"][0]["decision"] == "deleted"
    outcome = events(h, AUDIT_EVENT)[-1]
    assert outcome["phase"] == "outcome"
    assert outcome["delete_status"] == "sent" and outcome["confirmed_gone"] is True
    assert h.store.get(h.worker, h.route["id"])["state"] == "deleted"


def test_unexpected_failure_after_a_sent_delete_keeps_it_reported_as_sent():
    class Broken(StubStore):
        def record(self, *a, **kw):
            raise RuntimeError("store exploded")

    provider, store, cleanup = _two_route_cleanup(Broken)
    result = cleanup.apply(max_groups=10)  # must not raise
    first, second = result["decisions"]
    assert len(provider.deletes) == 1
    assert first["decision"] == "delete_unrecorded"  # never "skipped"
    assert first["delete_sent"] is True and first["needs_reconciliation"] is True
    assert first["error"] == "RuntimeError"
    assert result["run_stopped"] == "unexpected_error"
    assert result["counts"]["delete_unrecorded"] == 1
    assert second["reason"] == "run_stopped"


# ---- crash safety around begin and the intent audit --------------------------------------


def test_crash_between_begin_and_intent_audit_leaves_route_unknown_and_never_deletes(
    tmp_path, monkeypatch
):
    h = stopped_route(tmp_path)
    real = h.store.record_group_cleanup

    def crash_on_intent(worker, ident, audit):
        if audit["phase"] == "intent":
            raise Crash()
        return real(worker, ident, audit)

    monkeypatch.setattr(h.store, "record_group_cleanup", crash_on_intent)
    with pytest.raises(Crash):
        h.cleanup.apply()
    route = h.store.get(h.worker, h.route["id"])
    assert route["state"] == "unknown"
    assert [e["action"] for e in route["effects"]].count("delete") == 1
    assert route["effects"][-1]["action"] == "delete"
    assert events(h, AUDIT_EVENT) == [] and h.provider.deletes == []
    monkeypatch.undo()
    again = h.cleanup.apply()
    assert again["decisions"][0]["reason"] == "delete_already_attempted"
    assert h.provider.deletes == []


def test_failed_intent_audit_sends_no_delete():
    provider = FakeProvider()
    (entry,) = routes(1, provider)
    store, cleanup = stub([entry], provider)

    def refuse(worker, ident, audit):
        raise Conflict("audit refused")

    store.record_group_cleanup = refuse
    result = cleanup.apply()
    assert result["decisions"][0]["reason"] == "intent_not_recorded"
    assert provider.deletes == [] and result["run_stopped"] == "intent_not_recorded"


# ---- CLI --------------------------------------------------------------------------------


def test_cli_prints_plan_without_provider_and_has_no_apply_flag(tmp_path, capsys):
    h = stopped_route(tmp_path)
    state_dir = h.store.database_path.parent
    assert salad_cleanup_cli.main(["--state-dir", str(state_dir), "--min-age-seconds", "0"]) == 0
    out = capsys.readouterr().out
    assert "Eligible stopped groups" in out and h.route["proposal"]["operation_id"][:8] in out
    assert "no provider call was made" in out and "NOT performed" in out
    assert salad_cleanup_cli.main(["--state-dir", str(state_dir), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["counts"]["reasons"] == {"stop_too_recent": 1}
    assert data["provider_calls"] == 0 and data["changed_anything"] is False
    for flag in ("--apply", "--delete", "--yes"):
        with pytest.raises(SystemExit) as exit_info:
            salad_cleanup_cli.main(["--state-dir", str(state_dir), flag])
        assert exit_info.value.code == 64
    assert "apply" not in salad_cleanup_cli._Parser(prog="x").format_help()
    assert h.provider.deletes == []


def test_cli_missing_state_dir_exits_64_and_creates_nothing(tmp_path, capsys):
    missing = tmp_path / "nope"
    with pytest.raises(SystemExit) as exit_info:
        salad_cleanup_cli.main(["--state-dir", str(missing)])
    assert exit_info.value.code == 64 and not missing.exists()
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SystemExit) as exit_info:
        salad_cleanup_cli.main(["--state-dir", str(empty)])
    assert exit_info.value.code == 64 and list(empty.iterdir()) == []
    with pytest.raises(SystemExit) as exit_info:
        salad_cleanup_cli.main([])
    assert exit_info.value.code == 64


def test_cli_works_while_a_writer_holds_the_state_open(tmp_path, capsys):
    h = stopped_route(tmp_path)
    state_dir = h.store.database_path.parent
    writer = sqlite3.connect(state_dir / DATABASE_NAME, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("CREATE TABLE IF NOT EXISTS scratch_writer (x)")
        assert (
            salad_cleanup_cli.main(["--state-dir", str(state_dir), "--min-age-seconds", "0"]) == 0
        )
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert "Counts: considered=1 eligible=1" in capsys.readouterr().out


def test_console_script_is_declared():
    text = open(os.path.join(os.path.dirname(__file__), "..", "pyproject.toml")).read()
    assert 'tessera-salad-cleanup = "tessera_control.salad_cleanup_cli:main"' in text
