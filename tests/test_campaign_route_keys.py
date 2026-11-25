"""Pins the route-state key set the desktop renderer and other readers rely on.

desktop/renderer.mjs renders ``route.go_no_go`` and ``route.proxy_safety``; a campaign
route with a live path must keep both (they describe control's built-in, disabled
activation, which stays blocked) and add ``live_path`` / ``builtin_proxy``.
"""

from __future__ import annotations

from test_campaign_route_live_path import seed
from test_remote_execution_routing import setup_route

from tessera_control.store import _connect

BLOCKED = "no_go_live_activation_blocked"


def _get(h):
    return h.store.get(h.journey.actor, h.route["id"])


def _seed_campaign(h):
    db = _connect(h.store.database_path)
    try:
        db.execute("PRAGMA foreign_keys = OFF")
        seed(db, route_id=h.route["id"])
    finally:
        db.close()


def test_campaign_salad_route_keeps_legacy_keys_and_adds_live_path(tmp_path):
    h = setup_route(tmp_path)
    plain_keys = set(_get(h))
    _seed_campaign(h)
    state = _get(h)
    assert plain_keys | {"live_path", "builtin_proxy"} == set(state)
    assert state["go_no_go"] == BLOCKED
    assert state["proxy_safety"]["transport"] == "disabled"
    assert state["proxy_safety"]["blocked_reasons"]
    assert state["control_surface"] == "campaign_executor_guardian"
    assert state["live_path"]["dispatcher"] == "salad_executor_guardian"
    assert state["live_path"]["authorizes_nothing"] is True
    proxy = state["builtin_proxy"]
    assert proxy["state"] == "disabled" and proxy["go_no_go"] == BLOCKED
    assert proxy["control_surface"] == "desktop_control_only"
    assert proxy["proxy_safety"] == state["proxy_safety"]


def test_non_campaign_salad_route_keys_unchanged(tmp_path):
    state = _get(setup_route(tmp_path))
    assert state["control_surface"] == "desktop_control_only"
    assert state["go_no_go"] == BLOCKED
    assert state["proxy_safety"]["transport"] == "disabled"
    assert "live_path" not in state and "builtin_proxy" not in state


def test_non_salad_route_has_no_proxy_safety(tmp_path):
    state = _get(setup_route(tmp_path, "kubernetes"))
    assert state["control_surface"] == "desktop_control_only"
    assert state["go_no_go"] == BLOCKED
    assert "proxy_safety" not in state
    assert "live_path" not in state and "builtin_proxy" not in state
