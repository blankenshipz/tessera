"""Guard: no response an agent reads names who approved (a key like approving_principal_id).

An enrolled agent sees what was approved (digest, class, expiry, delegation) but never the
approver's principal id. This walks each agent-facing response we can build cheaply and fails with
the path of any offending key. The owner's receipt is out of scope here.
"""

from __future__ import annotations

import re

from test_delegated_decisions_api import agent_headers, api, approve  # noqa: F401 (fixture)

APPROVER = re.compile(r"approv.*principal", re.I)


def keys(obj, path="$"):
    """Yield (path, key) for every dict key, recursively."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield f"{path}.{key}", str(key)
            yield from keys(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from keys(value, f"{path}[{index}]")


def offenders(body):
    return [path for path, key in keys(body) if APPROVER.search(key)]


def assert_no_approver(name, body):
    found = offenders(body)
    assert not found, f"{name} exposes approver identity to an agent at {found}"


def test_the_walker_finds_nested_keys():
    body = {"a": [{"b": {"Approving_Principal_Id": "x"}}], "ok": 1}
    assert offenders(body) == ["$.a[0].b.Approving_Principal_Id"]


def test_no_agent_facing_response_names_the_approver(api):  # noqa: F811
    world, client = api
    delegation = world.create()
    campaign = world.campaign()
    headers = agent_headers(world)
    # A gate decision row, so the list is not just an empty envelope.
    org = world.env.query("SELECT org_id FROM projects WHERE id=?", world.project)[0][0]
    world.env.execute(
        "INSERT INTO campaign_gate_decisions (id, org_id, project_id, campaign_id, "
        "campaign_spec_digest, decision, note, decided_by_principal_id, decided_credential_id, "
        "decision_class, created_at) VALUES ('gatedec_guard1', ?, ?, ?, ?, 'go', 'n', ?, NULL, "
        "'owner_delegated', '2026-01-01T00:00:00Z')",
        org,
        world.project,
        campaign["id"],
        campaign["spec_digest"],
        world.row(delegation["id"])["created_by_principal_id"],
    )
    approved = approve(client, headers, campaign, delegation)
    assert approved.status_code == 200, approved.text
    view = client.get(f"/campaigns/{campaign['id']}", headers=headers)
    decisions = client.get(f"/campaigns/{campaign['id']}/gate-decisions", headers=headers)
    assert view.status_code == 200 and decisions.status_code == 200
    assert view.json()["approvals"], "the live view must carry an approval for this to bite"
    assert decisions.json()["gate_decisions"], "the list must carry a decision for this to bite"
    assert_no_approver("delegated approve response", approved.json())
    assert_no_approver("live campaign view", view.json())
    assert_no_approver("gate decisions list", decisions.json())
