import json
import sqlite3

import pytest

from tessera_control import remote_execution as core
from tessera_control import remote_execution_hooks as hooks_mod
from tessera_control.remote_execution_salad import SaladRouteHooks
from tessera_control.store import Conflict


def test_hooks_resolved_per_target():
    assert type(hooks_mod.hooks_for("kubernetes")) is hooks_mod.RouteHooks
    for name in ("salad_container", "salad_inference"):
        assert isinstance(hooks_mod.hooks_for(name), SaladRouteHooks)
    assert isinstance(hooks_mod.cleanup_hooks(), SaladRouteHooks)


def test_policy_refusal_comes_from_hook(monkeypatch):
    store = object.__new__(core.RemoteExecutionStore)
    store.policies = []
    seen = []

    class Refusing(hooks_mod.RouteHooks):
        def refuse(self, proposal):
            seen.append(proposal)
            raise Conflict("hook refused")

    monkeypatch.setattr(core, "hooks_for", lambda target: Refusing())
    proposal = {"selection": {"target": "kubernetes"}}
    with pytest.raises(Conflict, match="hook refused"):
        store._policy(proposal)
    assert seen == [proposal]


def test_salad_inference_refusal_message_preserved():
    store = object.__new__(core.RemoteExecutionStore)
    store.policies = []
    with pytest.raises(Conflict, match="^Salad inference is reserved, not executable.$"):
        store._policy({"selection": {"target": "salad_inference"}})


def test_admit_workload_goes_through_hook_and_maps_value_error(monkeypatch):
    class Hooks(hooks_mod.RouteHooks):
        def admit_workload(self, db, project_id, selection, worker, *, create_shape):
            raise ValueError("too big")

    monkeypatch.setattr(core, "hooks_for", lambda target: Hooks())
    with pytest.raises(Conflict, match="Workload route refused: too big."):
        core._admit_workload(None, "p", {"target": "kubernetes"})


def _events_db(group):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE events (event_type TEXT, payload_json TEXT)")
    payload = {"proposal": {"selection": {"salad": group}}}
    db.execute("INSERT INTO events VALUES ('remote_execution.state', ?)", (json.dumps(payload),))
    return db


def test_salad_duplicate_group_guard_and_admission_order():
    group = {"organization": "o", "project": "p", "group": "g"}
    op = {"project_id": "pr", "worker_principal_id": "w"}
    calls = []

    def admit(db, project_id, selection, worker):
        calls.append((project_id, worker))

    hooks = SaladRouteHooks()
    with pytest.raises(Conflict, match="Salad group already reserved; one immutable group"):
        hooks.on_propose(_events_db(group), op, {"salad": group}, admit)
    assert calls == [("pr", "w")]
    other = dict(group, group="other")
    hooks.on_propose(_events_db(group), op, {"salad": other}, admit)
    hooks.on_propose(_events_db(group), op, {"salad": None}, admit)
    assert len(calls) == 2


def test_get_projection_and_cleanup_group_via_hook():
    hooks = SaladRouteHooks()
    state = {"proposal": {"selection": {"target": "salad_container"}}}
    hooks.project_state(state)
    assert "proxy_safety" in state
    other = {"proposal": {"selection": {"target": "salad_inference"}}}
    hooks.project_state(other)
    assert "proxy_safety" not in other
    assert hooks.cleanup_group({"salad": {"group": "g"}}) == "g"
    assert hooks.cleanup_group({}) is None
    with pytest.raises(ValueError):
        hooks.parse_cleanup_audit({"unknown": 1})
