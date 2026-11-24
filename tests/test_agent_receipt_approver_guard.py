"""Generic guard: no approver-principal key reaches an agent through the receipt view.

``receipt_agent_view.HIDDEN_PATHS`` is a fixed list. This guard walks every key of a real stored
receipt and of the agent view, so a new approver field added to ``campaign_receipts`` fails here
until it is hidden.
"""

from __future__ import annotations

import copy
import json
import re

from fastapi.testclient import TestClient
from salad_campaign_fixture import salad_provider_reset  # noqa: F401
from test_pending_approval_preapproved_visibility import (
    CLEAN,
    DEST,
    _preapproved_world,
    _read_all,
    _token,
)

from tessera_control.api import create_app
from tessera_control.receipt_agent_view import HIDDEN_PATHS, agent_receipt

APPROVER = re.compile(r"approv.*principal", re.I)


def keys(obj, path=""):
    """Yield ``(path, key)`` for every dict key at any depth; list items are marked ``[]``."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            here = f"{path}.{key}" if path else str(key)
            yield here, key
            yield from keys(value, here)
    elif isinstance(obj, list):
        for item in obj:
            yield from keys(item, path + "[]")


def offending(obj) -> list[str]:
    return [path for path, key in keys(obj) if APPROVER.search(str(key))]


def _stored_receipt(world) -> dict:
    [row] = world.env.query(
        "SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", world.campaign_id
    )
    return json.loads(row["receipt_json"])


def _world(tmp_path):
    world = _preapproved_world(tmp_path)
    world.run_to_outcome(guardian_final=CLEAN)
    world.tick_until(lambda: world.status() == "succeeded")
    return world


def test_the_agent_receipt_view_has_no_approver_principal_key(tmp_path):
    world = _world(tmp_path)
    stored = _stored_receipt(world)
    # Non-vacuous: the stored receipt does carry approver-principal keys.
    assert offending(stored)

    client = TestClient(create_app(world.env.state))
    agent = _read_all(world, client, {**_token("agent"), **DEST}, terminal=True)
    assert offending(agent["receipt"]) == []
    assert offending(agent_receipt(stored)[0]) == []


def test_the_guard_flags_an_unhidden_approver_key_in_a_synthetic_receipt(tmp_path):
    stored = _stored_receipt(_world(tmp_path))
    synthetic = copy.deepcopy(stored)
    synthetic["approval"]["approved_by_principal_id"] = "prn_x"
    synthetic["new_section"] = {"items": [{"approving_principal_id": "prn_y"}]}
    synthetic.setdefault("preapprovals", [{}])[0]["nested"] = {"approver_principal": "prn_z"}
    projected, _ = agent_receipt(synthetic)
    assert sorted(offending(projected)) == [
        "approval.approved_by_principal_id",
        "new_section.items[].approving_principal_id",
        "preapprovals[].nested.approver_principal",
    ]


def test_every_approver_key_in_the_stored_receipt_is_in_hidden_paths(tmp_path):
    stored = _stored_receipt(_world(tmp_path))
    found = offending(stored)
    assert found
    assert [path for path in found if path not in HIDDEN_PATHS] == []
