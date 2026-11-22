"""Provider registration routes (ADR 0081): human-only, audited, idempotent, checked."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from campaign_fixture import Env
from fastapi.testclient import TestClient

from tessera_control.api import create_app
from tessera_control.provider_registration_api import NAME_UNAVAILABLE
from tessera_control.store import canonical_digest

DESTINATION = "planner.local"
ROUTE = "/admin/campaign-providers"


def profile(env, **over):
    value = dict(
        name="cpu-main",
        org_id=env.owner["org_id"],
        project_id=env.project["id"],
        worker_principal_id=env.worker["id"],
        salad_organization="example-org",
        salad_project="example-project",
        country="US",
        cpu_rate={"vcpu_hour_usd": "0.005", "memory_gb_hour_usd": "0.001"},
        cpu_millicores=2000,
        memory_mb=4096,
        disk_mb=8192,
        route_max_seconds=4200,
        capability_environment_id="salad-cpu",
        artifact_destination="artifact-ref:quarantine",
        checkpoint_destination="checkpoint-ref:external",
        monitor_profile="a" * 64,
        watchdog_profile="b" * 64,
        custody_host="custody.example-tailnet.ts.net",
        credential_reference="credential-ref:salad-management",
        credential_version=1,
        allowed_image_digests=["sha256:" + "1" * 64],
        max_run_cents=50,
        max_task_cents=50,
    )
    value.update(over)
    return value


class World:
    def __init__(self, tmp_path, configured=True):
        self.env = Env(tmp_path)
        self.issued: dict = {}
        self.path = tmp_path / "providers.json"
        self.app = create_app(self.env.state)
        self.app.state.campaign_providers_path = self.path if configured else None
        self.client = TestClient(self.app)

    def credential(self, name, **body):
        if name in self.issued:
            return self.issued[name]
        body = {
            "audience": "control",
            "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            **body,
        }
        token = f"synthetic-{name}-token-" + "x" * 30
        self.env.plain.issue_credential(
            self.env.owner, f"cred-{name}", canonical_digest(body), body, token
        )
        self.issued[name] = {
            "Authorization": f"Bearer {token}",
            "X-Agent-Destination": DESTINATION,
        }
        return self.issued[name]

    def human(self):
        return self.credential("human", kind="human_session")

    def root(self):
        """The deployment's root owner credential (a human_session is not the root owner)."""
        header = (self.env.state / "owner.header").read_text().strip().split(": ", 1)[1]
        return {"Authorization": header, "X-Agent-Destination": DESTINATION}

    def other_org_human(self):
        """A human_session of a second organization (seeded like the cross-tenant admin test)."""
        from tessera_control.store import Store, _token_hash, utc_now

        token = "other-org-owner-" + "z" * 40
        with sqlite3.connect(self.env.state / "tessera.sqlite3") as db:
            db.execute("INSERT INTO organizations VALUES (?,?,?)", ("org_b", "B", utc_now()))
            db.execute(
                "INSERT INTO principals VALUES (?,?,?,?,?,?)",
                ("owner_b", "org_b", "human", _token_hash(token), None, utc_now()),
            )
            db.execute(
                "INSERT INTO credentials VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("cred_b", "owner_b", "local_owner", "control", None, None, None, None,
                 utc_now(), None),
            )  # fmt: skip
        owner_b = Store(self.env.state).authenticate(token)
        body = {
            "audience": "control",
            "kind": "human_session",
            "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        }
        session = "other-org-session-" + "y" * 40
        self.env.plain.issue_credential(owner_b, "cred-b", canonical_digest(body), body, session)
        return {"Authorization": f"Bearer {session}", "X-Agent-Destination": DESTINATION}

    def agent(self, actions):
        return self.credential(
            "agent-" + "-".join(a.replace(":", "_") for a in actions),
            kind="agent",
            project_id=self.env.project["id"],
            external_agent={"actions": actions, "destination": DESTINATION},
        )

    def post(self, headers, body, key="k1"):
        return self.client.post(ROUTE, headers={**headers, "Idempotency-Key": key}, json=body)

    def events(self, event_type):
        with sqlite3.connect(self.env.state / "tessera.sqlite3") as db:
            return db.execute(
                "SELECT decision, payload_json FROM operator_audit_events WHERE event_type=?",
                (event_type,),
            ).fetchall()


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def add(world, **over):
    body = {"action": "add", "section": "salad-profile", "entry": profile(world.env, **over)}
    return world.post(world.human(), body, key="add-" + over.get("name", "cpu-main"))


def test_a_human_adds_lists_and_updates_a_profile_with_audit(world):
    assert world.client.get(ROUTE, headers=world.human()).json()["entries"] == []
    response = add(world)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "written" and body["applies"].startswith("at the next serve start")
    assert oct(world.path.stat().st_mode & 0o777) == "0o600"
    listed = world.client.get(ROUTE, headers=world.human()).json()
    (entry,) = listed["entries"]
    assert entry["key"] == "cpu-main" and entry["resource_class"] == "cpu"
    assert entry["max_run_cents"] == 50 and entry["credential_reference"].startswith(
        "credential-ref:"
    )
    assert (
        listed["digest"] == body["after_digest"] and listed["recent_audit"][-1]["action"] == "add"
    )
    update = world.post(
        world.human(),
        {
            "action": "update",
            "section": "salad-profile",
            "key": "cpu-main",
            "entry": {"max_run_cents": 40},
            "expected_digest": listed["digest"],
        },
        key="upd",
    )
    assert update.status_code == 200, update.text
    assert update.json()["changed_fields"] == ["max_run_cents"]
    # Audited as names, digests and field names only.
    allowed = world.events("provider_config.changed")
    assert [r[0] for r in allowed] == ["allowed", "allowed"]
    payload = json.loads(allowed[1][1])
    assert payload["changed_fields"] == ["max_run_cents"] and "entry" not in payload
    # The trust readout names the exact entry to add (provider trust is not edited).
    assert body["trust"]["hint"].count('"salad"') == 1 and "merge" in body["trust"]["hint"]


def test_an_agent_can_read_with_scope_but_never_write(world):
    add(world)
    reader = world.agent(["provider_trust:read"])
    assert world.client.get(ROUTE, headers=reader).status_code == 200
    assert world.client.get(ROUTE, headers=world.agent(["campaign:read"])).status_code == 403
    body = {"action": "add", "section": "salad-profile", "entry": profile(world.env, name="x")}
    assert world.post(reader, body, key="a1").status_code == 403
    assert world.client.get(ROUTE).status_code == 401


def test_replay_returns_the_stored_response_and_writes_once(world):
    first = add(world)
    again = add(world)
    assert again.status_code == 200 and again.json() == first.json()
    assert len(world.events("provider_config.changed")) == 1


def test_a_refusal_is_a_409_and_audited_and_writes_nothing(world):
    body = {
        "action": "add",
        "section": "salad-profile",
        "entry": profile(world.env, salad_project="Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZmdoaWo"),
    }
    response = world.post(world.human(), body, key="bad")
    assert response.status_code == 409 and "secret" in response.json()["reason"]
    assert not world.path.exists()
    ((decision, raw),) = world.events("provider_config.refused")
    assert decision == "denied" and "Zm9v" not in raw
    # An unknown section is deployment-wide by definition: only the root owner reaches the
    # "unknown section" answer; any other human is told who may change it.
    body = {"action": "add", "section": "nope", "entry": {}}
    assert "Unknown section" in world.post(world.root(), body, key="unk").json()["reason"]
    other = world.post(world.human(), body, key="unk2")
    assert other.status_code == 409 and "Deployment-wide" in other.json()["reason"]


def test_an_unknown_project_or_worker_is_refused_as_an_unknown_destination(world):
    human = world.human()
    for over in ({"project_id": "project_nope"}, {"worker_principal_id": "worker_nope"}):
        body = {"action": "add", "section": "salad-profile", "entry": profile(world.env, **over)}
        response = world.post(human, body, key="d-" + next(iter(over)))
        assert response.status_code == 409 and "Unknown destination" in response.json()["reason"]
    assert not world.path.exists()


def test_a_stale_expected_digest_is_refused(world):
    add(world)
    body = {
        "action": "update",
        "section": "salad-profile",
        "key": "cpu-main",
        "entry": {"max_run_cents": 10},
        "expected_digest": "0" * 64,
    }
    response = world.post(world.human(), body, key="stale")
    assert response.status_code == 409 and "changed since" in response.json()["reason"]


def test_dry_run_validates_without_writing(world):
    body = {
        "action": "add",
        "section": "salad-profile",
        "entry": profile(world.env),
        "dry_run": True,
    }
    response = world.post(world.human(), body, key="dry")
    assert response.status_code == 200 and response.json()["status"] == "validated"
    assert not world.path.exists() and not world.events("provider_config.changed")


def test_without_a_configured_file_reads_say_so_and_writes_are_refused(tmp_path):
    world = World(tmp_path, configured=False)
    assert world.client.get(ROUTE, headers=world.human()).json()["configured"] is False
    body = {"action": "add", "section": "salad-profile", "entry": profile(world.env)}
    response = world.post(world.human(), body)
    assert response.status_code == 409 and "--campaign-providers" in response.json()["reason"]


def update(world, entry, **over):
    body = {"action": "update", "section": "salad-profile", "key": "cpu-main", "entry": entry}
    return world.post(world.human(), {**body, **over}, key="upd-" + canonical_digest(entry)[:8])


def test_an_update_that_changes_only_the_worker_is_checked_as_an_unknown_destination(world):
    add(world)
    before = world.path.read_bytes()
    response = update(world, {"worker_principal_id": "worker_of_another_org"})
    assert response.status_code == 409 and "Unknown destination" in response.json()["reason"]
    assert world.path.read_bytes() == before


def test_an_update_that_changes_only_the_organization_is_refused(world):
    add(world)
    before = world.path.read_bytes()
    response = update(world, {"org_id": "org_of_another_tenant"})
    assert response.status_code == 409 and "Unknown destination" in response.json()["reason"]
    assert world.path.read_bytes() == before
    # A harmless update (no destination field) still goes through on the merged entry.
    assert update(world, {"max_run_cents": 30}).status_code == 200


def test_a_non_string_destination_is_a_409_and_audited_never_a_500(world):
    for over in ({"project_id": ["a", "b"]}, {"worker_principal_id": {"x": 1}}, {"org_id": 7}):
        body = {"action": "add", "section": "salad-profile", "entry": profile(world.env, **over)}
        response = world.post(world.human(), body, key="t-" + next(iter(over)))
        assert response.status_code == 409, response.text
        assert "Unknown destination" in response.json()["reason"]
    assert not world.path.exists()
    assert len(world.events("provider_config.refused")) == 3


def test_an_organization_sees_its_own_profiles_and_not_another_tenants(world):
    add(world)
    document = json.loads(world.path.read_text())
    other = profile(world.env, name="their-profile", org_id="org_of_another_tenant")
    document["salad_profiles"]["profiles"].append(other)
    world.path.write_text(json.dumps(document))
    world.path.chmod(0o600)
    listed = world.client.get(ROUTE, headers=world.human()).json()
    assert [e["key"] for e in listed["entries"]] == ["cpu-main"]
    assert "their-profile" not in json.dumps(listed)


def test_a_corrupt_audit_file_does_not_break_the_listing(world):
    add(world)
    with open(str(world.path) + ".audit.jsonl", "a") as handle:
        handle.write("garbage that is not json\n")
    response = world.client.get(ROUTE, headers=world.human())
    assert response.status_code == 200
    assert (
        response.json()["entries"][0]["key"] == "cpu-main" and response.json()["recent_audit"] == []
    )


def test_an_identical_re_add_with_a_new_key_is_a_no_op_success(world):
    first = add(world)
    assert first.status_code == 200 and first.json()["written"] is True
    body = {"action": "add", "section": "salad-profile", "entry": profile(world.env)}
    again = world.post(world.human(), body, key="a-new-key")
    assert again.status_code == 200, again.text
    assert again.json()["written"] is False and again.json()["noop"] is True
    assert len(world.events("provider_config.changed")) == 1
    # A different entry under the same name is still an error: use update.
    changed = {**body, "entry": profile(world.env, max_run_cents=10)}
    assert world.post(world.human(), changed, key="third").status_code == 409


def test_a_key_that_is_not_a_plain_name_is_refused(world):
    add(world)
    response = update(world, {"max_run_cents": 20}, key="bad key/with slash")
    # No stored entry has that name, so the destination check (or the key check) refuses first.
    assert response.status_code == 409 and response.json()["status"] == "refused"


def test_a_worker_credential_can_neither_read_nor_write(world):
    headers = {"Authorization": "Bearer worker-token", "X-Agent-Destination": DESTINATION}
    assert world.client.get(ROUTE, headers=headers).status_code in {401, 403}
    body = {"action": "add", "section": "salad-profile", "entry": profile(world.env)}
    assert world.post(headers, body, key="w1").status_code in {401, 403}
    assert not world.path.exists()


def test_another_organization_cannot_take_over_or_probe_a_profile(world):
    add(world)
    before = world.path.read_bytes()
    theirs = world.other_org_human()
    takeover = world.post(
        theirs,
        {
            "action": "update",
            "section": "salad-profile",
            "key": "cpu-main",
            "entry": {"org_id": "org_b", "project_id": "project_b", "worker_principal_id": "w_b"},
        },
        key="take",
    )
    assert takeover.status_code == 409 and takeover.json()["reason"] == NAME_UNAVAILABLE
    probe = world.post(
        theirs,
        {"action": "add", "section": "salad-profile", "entry": profile(world.env, org_id="org_b")},
        key="probe",
    )
    assert probe.status_code == 409 and probe.json()["reason"] == NAME_UNAVAILABLE
    assert "exist" not in probe.json()["reason"] and world.path.read_bytes() == before


def test_the_owning_organization_still_adds_and_updates_its_own_profiles(world):
    assert add(world).status_code == 200
    assert update(world, {"max_run_cents": 25}).status_code == 200
    assert add(world, name="second").status_code == 200


@pytest.mark.parametrize(
    ("section", "entry"),
    [
        ("local-docker", {"worker_principal_id": "worker-x"}),
        ("self-hosted", {"id": "lab-2", "worker_principal_id": "worker-x"}),
        ("plugin", {}),
        ("not-a-section", {}),
    ],
)
def test_deployment_wide_sections_are_for_the_root_owner_only(world, section, entry):
    body = {"action": "add", "section": section, "entry": entry}
    if section == "plugin":
        body["key"] = "some_vendor"
    for headers in (world.human(), world.other_org_human()):
        response = world.post(headers, body, key="dw-" + canonical_digest([section, str(headers)]))
        assert response.status_code == 409 and "Deployment-wide" in response.json()["reason"]
    assert not world.path.exists()


def test_the_root_owner_names_only_registered_workers(world):
    for section, entry in (
        ("local-docker", {"worker_principal_id": "worker_of_nobody"}),
        ("self-hosted", {"id": "lab-2", "worker_principal_id": "worker_of_nobody"}),
    ):
        response = world.post(
            world.root(), {"action": "add", "section": section, "entry": entry}, key="r-" + section
        )
        assert response.status_code == 409 and "Unknown destination" in response.json()["reason"]
    assert not world.path.exists()
    ok = world.post(
        world.root(),
        {
            "action": "add",
            "section": "self-hosted",
            "entry": {"id": "lab-2", "worker_principal_id": world.env.worker["id"]},
        },
        key="ok",
    )
    assert ok.status_code == 200, ok.text
    # The readout is about the instance id, not the section name.
    assert ok.json()["trust"]["provider"] == "lab-2" and '"lab-2"' in ok.json()["trust"]["hint"]
    # An update is checked on the merged entry: a bad worker is refused, the rest passes.
    bad = world.post(
        world.root(),
        {
            "action": "update",
            "section": "self-hosted",
            "key": "lab-2",
            "entry": {"worker_principal_id": "worker_of_nobody"},
        },
        key="bad-upd",
    )
    assert bad.status_code == 409 and "Unknown destination" in bad.json()["reason"]
    fine = world.post(
        world.root(),
        {
            "action": "update",
            "section": "self-hosted",
            "key": "lab-2",
            "entry": {"worker_slots": 2},
        },
        key="fine-upd",
    )
    assert fine.status_code == 200, fine.text


def test_deployment_wide_worker_ids_are_redacted_for_everyone_but_the_root_owner(world):
    add(world)
    entry = {"id": "lab-2", "worker_principal_id": world.env.worker["id"]}
    body = {"action": "add", "section": "self-hosted", "entry": entry}
    assert world.post(world.root(), body, key="sh").status_code == 200
    rows = world.client.get(ROUTE, headers=world.human()).json()["entries"]
    by_key = {r["key"]: r for r in rows}
    assert (
        by_key["lab-2"]["scope"] == "deployment" and by_key["lab-2"]["worker_principal_id"] is None
    )
    assert by_key["cpu-main"]["scope"] == "organization"
    assert by_key["cpu-main"]["worker_principal_id"] == world.env.worker["id"]
    root_rows = world.client.get(ROUTE, headers=world.root()).json()["entries"]
    assert {r["key"]: r["worker_principal_id"] for r in root_rows}["lab-2"] == entry[
        "worker_principal_id"
    ]


def test_the_deployment_owner_is_the_bootstrap_organizations_root_owner_only():
    from tessera_control.provider_registration_api import ProviderRegistrationStore
    from tessera_control.store import LOCAL_ORG_ID

    root = {"role": "human", "kind": "local_owner", "parent_id": None}
    owner = ProviderRegistrationStore.deployment_owner
    assert owner({**root, "org_id": LOCAL_ORG_ID})
    assert not owner({**root, "org_id": "org_b"})
    assert not owner({**root, "org_id": LOCAL_ORG_ID, "kind": "human_session"})
    assert not owner({**root, "org_id": LOCAL_ORG_ID, "parent_id": "p"})


def test_a_profile_handed_to_another_org_after_a_listing_is_refused_end_to_end(world):
    add(world)
    assert world.client.get(ROUTE, headers=world.human()).json()["entries"]  # the human listed
    document = json.loads(world.path.read_text())
    document["salad_profiles"]["profiles"][0]["org_id"] = "org_b"  # another org now owns it
    world.path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    before = world.path.read_bytes()
    response = update(world, {"max_run_cents": 25})
    assert response.status_code == 409 and response.json()["reason"] == NAME_UNAVAILABLE
    assert world.path.read_bytes() == before


def test_the_ownership_check_reads_the_file_once_and_only_under_the_lock(world, monkeypatch):
    """Guards the fix: the API must not read the file for its own check before apply_change
    (that read is outside the lock); the one read is the locked one, whose document the
    authorize callback receives. Fails against an API that reads the file separately."""
    from tessera_control import provider_registration as registration

    add(world)
    reads = []
    real = registration.read_document

    def counting(path):
        reads.append(path)
        return real(path)

    monkeypatch.setattr(registration, "read_document", counting)
    response = update(world, {"max_run_cents": 25})
    assert response.status_code == 200
    assert len(reads) == 1


def test_a_dry_run_add_still_lets_a_caller_infer_that_a_name_is_taken(world):
    add(world)  # the first organization owns cpu-main
    theirs = world.other_org_human()

    def dry(name, key):
        entry = profile(world.env, name=name, org_id="org_b")
        body = {"action": "add", "section": "salad-profile", "entry": entry, "dry_run": True}
        return world.post(theirs, body, key=key)

    taken = dry("cpu-main", "dry-taken")
    assert taken.status_code == 409 and taken.json()["reason"] == NAME_UNAVAILABLE
    # A free name is answered differently (here by the destination check, which the other
    # organization's synthetic project fails): the two answers are distinguishable, which is the
    # inference ADR 0081 documents. Names share one namespace across the deployment.
    free = dry("never-used", "dry-free")
    assert free.json()["reason"] != NAME_UNAVAILABLE


def test_a_dry_run_add_of_a_free_name_validates_for_the_owning_organization(world):
    add(world)
    body = {
        "action": "add",
        "section": "salad-profile",
        "entry": profile(world.env, name="free-name"),
        "dry_run": True,
    }
    response = world.post(world.human(), body, key="dry-free-own")
    assert response.status_code == 200 and response.json()["status"] == "validated"
