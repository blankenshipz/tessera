"""Who may change which provider entry through the control API (README, ADR 0081).

Guarantee under test: through the API a human manages only its own organization's Salad
profiles (checked against the database, in the store rather than only at the route), and
deployment-wide entries (local-docker, self-hosted, plugins) need the deployment's root owner.
Agents and workers never write, and provider trust is never edited by a registration.
The neighbouring cases already covered by tests/test_provider_registration_api.py are not
repeated here.
"""

from __future__ import annotations

import json

import pytest
from provider_trust_fixture import DERIVED
from test_provider_registration_api import ROUTE, World, add, profile

from tessera_control.provider_registration_api import (
    NAME_UNAVAILABLE,
    ProviderRegistrationStore,
    RegistrationBody,
)
from tessera_control.provider_trust import current_policy, install_policy, load_policy_file
from tessera_control.store import Forbidden, Store

OTHER_OWNER_TOKEN = "other-org-owner-" + "z" * 40  # seeded by World.other_org_human()


class Scoped(World):
    def other_org_human(self):
        """Seed organization B once per test; later calls reuse its session."""
        if "org_b_session" not in self.issued:
            self.issued["org_b_session"] = super().other_org_human()
        return self.issued["org_b_session"]

    def other_org_root(self):
        """Organization B's own root owner (local_owner, no parent), labelled as automation."""
        self.other_org_human()
        return {
            "Authorization": f"Bearer {OTHER_OWNER_TOKEN}",
            "X-Agent-Destination": "planner.local",
            "X-Tessera-Automation": "test-automation",
        }


@pytest.fixture
def world(tmp_path):
    return Scoped(tmp_path)


def _session(world, headers):
    return Store(world.env.state).authenticate(headers["Authorization"].removeprefix("Bearer "))


def _body(action, section, entry, key=None, **extra):
    body = {"action": action, "section": section, "entry": entry, **extra}
    if key is not None:
        body["key"] = key
    return body


# -- (1) organization-scoped Salad profiles: only the owning organization ------------------------


def test_another_organization_cannot_modify_a_profile_even_leaving_its_destination(world):
    """A plain field change (no destination field) by org B is refused like a takeover."""
    assert add(world).status_code == 200
    before = world.path.read_bytes()
    for dry_run in (True, False):
        response = world.post(
            world.other_org_human(),
            _body("update", "salad-profile", {"max_run_cents": 1}, "cpu-main", dry_run=dry_run),
            key=f"modify-{dry_run}",
        )
        assert response.status_code == 409 and response.json()["reason"] == NAME_UNAVAILABLE
        assert "max_run_cents" not in response.text  # nothing of A's entry is echoed back
    assert world.path.read_bytes() == before
    assert not [r for r in world.events("provider_config.changed") if "org_b" in r[1]]


def test_another_organization_cannot_add_a_profile_claiming_the_first_orgs_destination(world):
    """The organization comes from the authenticated principal, never the entry's claim."""
    add(world)
    before = world.path.read_bytes()
    entry = profile(world.env, name="claimed")  # names org A, A's project and A's worker
    response = world.post(world.other_org_human(), _body("add", "salad-profile", entry), key="c")
    assert response.status_code == 409 and "Unknown destination" in response.json()["reason"]
    assert world.path.read_bytes() == before


def test_delete_and_replace_are_not_api_operations(world):
    add(world)
    before = world.path.read_bytes()
    for action in ("delete", "replace", "remove"):
        body = _body(action, "salad-profile", profile(world.env), "cpu-main")
        assert world.post(world.human(), body, key="a-" + action).status_code == 422
    headers = {**world.human(), "Idempotency-Key": "m"}
    for method in ("put", "patch", "delete"):
        assert getattr(world.client, method)(ROUTE, headers=headers).status_code == 405
    assert world.path.read_bytes() == before


def test_the_ownership_check_is_in_the_store_not_only_the_route(world):
    """Calling the store directly (no HTTP route) still refuses another organization."""
    add(world)
    before = world.path.read_bytes()
    store = ProviderRegistrationStore(world.env.state, path=world.path)
    org_b = _session(world, world.other_org_human())
    status, body = store.register(
        org_b,
        "direct-b",
        RegistrationBody(
            action="update", section="salad-profile", key="cpu-main", entry={"max_run_cents": 1}
        ),
    )
    assert status == 409 and body["reason"] == NAME_UNAVAILABLE
    assert world.path.read_bytes() == before
    assert all(e.get("org_id") != world.env.owner["org_id"] for e in store.read(org_b)["entries"])
    # The owning organization, through the same store call, succeeds.
    status, body = store.register(
        _session(world, world.human()),
        "direct-a",
        RegistrationBody(
            action="update", section="salad-profile", key="cpu-main", entry={"max_run_cents": 9}
        ),
    )
    assert status == 200 and body["status"] == "written"


# -- (2) deployment-wide entries: only the deployment's root owner -------------------------------


@pytest.mark.parametrize(
    ("section", "key", "entry"),
    [("self-hosted", "lab-2", {"id": "lab-2"}), ("local-docker", None, {})],
)
def test_an_existing_deployment_wide_entry_is_updated_by_the_root_owner_only(
    world, section, key, entry
):
    entry = {**entry, "worker_principal_id": world.env.worker["id"]}
    created = world.post(world.root(), _body("add", section, entry), key="root-add")
    assert created.status_code == 200, created.text
    before = world.path.read_bytes()
    change = {"worker_slots": 2}
    for name, headers in (
        ("session", world.human()),  # a human of the bootstrap organization, not its root
        ("org-b-session", world.other_org_human()),
        ("org-b-root", world.other_org_root()),  # root owner of another organization
    ):
        response = world.post(headers, _body("update", section, change, key), key="u-" + name)
        assert response.status_code == 409, (name, response.text)
        assert "Deployment-wide" in response.json()["reason"]
    assert world.path.read_bytes() == before
    fine = world.post(world.root(), _body("update", section, change, key), key="root-upd")
    assert fine.status_code == 200, fine.text  # the deployment's root owner still may


def test_another_organizations_root_owner_cannot_add_deployment_wide_entries(world):
    root_b = world.other_org_root()
    for section, entry, key in (
        ("local-docker", {"worker_principal_id": world.env.worker["id"]}, None),
        ("self-hosted", {"id": "lab-9", "worker_principal_id": world.env.worker["id"]}, None),
        ("plugin", {}, "some_vendor"),
    ):
        response = world.post(root_b, _body("add", section, entry, key), key="b-" + section)
        assert response.status_code == 409 and "Deployment-wide" in response.json()["reason"]
    assert not world.path.exists()


# -- (3) agents and workers never write ---------------------------------------------------------


def test_an_agent_with_every_relevant_scope_cannot_add_or_update(world):
    add(world)
    before = world.path.read_bytes()
    agent = world.agent(["provider_trust:read", "campaign:read", "campaign:submit"])
    for action, entry, key in (
        ("add", profile(world.env, name="agent-made"), None),
        ("update", {"max_run_cents": 1}, "cpu-main"),
    ):
        response = world.post(agent, _body(action, "salad-profile", entry, key), key="ag-" + action)
        assert response.status_code == 403, response.text
    response = world.post(agent, _body("add", "self-hosted", {"id": "lab-3"}), key="ag-deployment")
    assert response.status_code == 403
    assert world.path.read_bytes() == before
    with pytest.raises(Forbidden):  # the store refuses it too, without the route
        ProviderRegistrationStore(world.env.state, path=world.path).register(
            _session(world, agent),
            "direct-agent",
            RegistrationBody(
                action="update", section="salad-profile", key="cpu-main", entry={"max_run_cents": 1}
            ),
        )
    assert world.path.read_bytes() == before


def test_a_registered_worker_credential_cannot_read_or_write(world):
    add(world)
    before = world.path.read_bytes()
    headers = {"Authorization": "Bearer worker-token", "X-Agent-Destination": "planner.local"}
    assert world.client.get(ROUTE, headers=headers).status_code in {401, 403}
    response = world.post(
        headers, _body("update", "salad-profile", {"max_run_cents": 1}, "cpu-main")
    )
    assert response.status_code in {401, 403}
    assert world.path.read_bytes() == before


# -- (4) provider trust is never edited by a registration ---------------------------------------


def _trust(world, tmp_path):
    path = tmp_path / "trust.json"
    entry = {"data_classes": list(DERIVED), "worker_principal_ids": ["w-1"]}
    path.write_text(json.dumps({"schema": "tessera.provider-trust.v1", "providers": {"x": entry}}))
    path.chmod(0o600)
    install_policy(load_policy_file(path))
    world.app.state.provider_trust_path = path
    return path


def test_a_registration_never_edits_provider_trust(world, tmp_path):
    trust = _trust(world, tmp_path)
    before, policy = trust.read_bytes(), current_policy()
    assert add(world).status_code == 200
    assert (
        world.post(
            world.root(),
            _body(
                "add", "self-hosted", {"id": "lab-2", "worker_principal_id": world.env.worker["id"]}
            ),
            key="sh",
        ).status_code
        == 200
    )
    assert trust.read_bytes() == before and current_policy() is policy
    assert "provider-trust" not in world.path.read_text()


@pytest.mark.parametrize(
    "field", ["data_classes", "worker_principal_ids", "allowed_license_labels"]
)
def test_trust_fields_in_an_entry_are_refused(world, tmp_path, field):
    trust = _trust(world, tmp_path)
    before = trust.read_bytes()
    value = ["public"] if field != "worker_principal_ids" else [world.env.worker["id"]]
    entry = profile(world.env, **{field: value})
    response = world.post(world.human(), _body("add", "salad-profile", entry), key="t-" + field)
    assert response.status_code == 409 and response.json()["status"] == "refused"
    shared = {"id": "lab-2", "worker_principal_id": world.env.worker["id"], field: value}
    response = world.post(world.root(), _body("add", "self-hosted", shared), key="r-" + field)
    assert response.status_code == 409 and response.json()["status"] == "refused"
    assert not world.path.exists() and trust.read_bytes() == before


@pytest.mark.parametrize("section", ["provider-trust", "provider_trust", "trust"])
def test_provider_trust_is_not_a_registrable_section_even_for_the_root_owner(
    world, tmp_path, section
):
    trust = _trust(world, tmp_path)
    before = trust.read_bytes()
    entry = {"data_classes": ["public"], "worker_principal_ids": [world.env.worker["id"]]}
    response = world.post(world.root(), _body("add", section, entry, "x"), key="s-" + section)
    assert response.status_code == 409 and response.json()["status"] == "refused"
    assert not world.path.exists() and trust.read_bytes() == before
