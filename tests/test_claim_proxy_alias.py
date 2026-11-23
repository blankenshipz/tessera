from types import SimpleNamespace

import pytest

from tessera_control import remote_execution as core
from tessera_control import remote_execution_hooks as hooks_mod
from tessera_control.remote_execution_salad import SaladRouteHooks
from tessera_control.store import Conflict


def _policy(target):
    return SimpleNamespace(target=SimpleNamespace(selection=SimpleNamespace(target=target)))


def _store():
    return object.__new__(core.RemoteExecutionStore)


def test_alias_warns_and_delegates_with_same_arguments(monkeypatch):
    store = _store()
    calls = []

    def fake(worker, ident, effect_id, policy, preflight, *, recheck=False):
        calls.append((worker, ident, effect_id, policy, preflight, recheck))
        return "state"

    monkeypatch.setattr(store, "claim_proxy", fake)
    with pytest.warns(DeprecationWarning, match="claim_salad_proxy is deprecated"):
        result = store.claim_salad_proxy("w", "r", "e", "pol", "pre", recheck=True)
    assert result == "state"
    assert calls == [("w", "r", "e", "pol", "pre", True)]


def test_claim_proxy_runs_the_target_hook_inside_mutate(monkeypatch):
    store = _store()
    seen = {}

    class Hooks(hooks_mod.RouteHooks):
        def claim_path(self, ident):
            return f"/internal/x/{ident}/claim"

        def claim_proxy(self, st, db, live, ident, effect_id, policy, preflight, recheck):
            seen["args"] = (st, db, live, ident, effect_id, preflight, recheck)
            return 201, "claimed"

    def mutate(worker, key, method, path, digest, command, audience):
        seen["path"] = path
        seen["audience"] = audience
        return command("DB", "LIVE")

    monkeypatch.setattr(core, "hooks_for", lambda target: Hooks())
    monkeypatch.setattr(store, "_mutate", mutate)
    assert store.claim_proxy("w", "r1", "e1", _policy("t"), "pre") == "claimed"
    assert seen["path"] == "/internal/x/r1/claim"
    assert seen["audience"] == "worker"
    assert seen["args"] == (store, "DB", "LIVE", "r1", "e1", "pre", False)


def test_salad_hook_path_and_default_refusal_are_unchanged():
    assert SaladRouteHooks().claim_path("abc") == "/internal/salad/abc/claim"
    store = _store()
    with pytest.raises(Conflict, match="Target has no proxy claim."):
        store.claim_proxy("w", "r", "e", _policy("kubernetes"), {})
    with pytest.warns(DeprecationWarning), pytest.raises(Conflict, match="no proxy claim"):
        store.claim_salad_proxy("w", "r", "e", _policy("kubernetes"), {})
