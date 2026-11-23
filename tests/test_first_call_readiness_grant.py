"""The first-call readiness ``grant`` check against the real control plane (ADR 0155).

Grants are issued, revoked and spent through the real in-process routes; the check reads the
control database read-only and must accept a reasoning grant (no execution running) and an
execution-bound grant alike, and name every reason the first call would be refused. Synthetic
only: no network, no keys.
"""

# ruff: noqa: F811
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from reasoning_grant_world import DESTINATION, ReasoningGrant
from test_agent_authority import prepared  # noqa: F401
from test_anthropic_runtime_integration import MODEL
from test_reasoning_authority_grant import Bare

from tessera_agent import first_call_readiness as fcr


@pytest.fixture
def bare(tmp_path):
    world = Bare(tmp_path)
    world.limits(provider="anthropic", per_call=20, daily=100)
    return world


def profile_for(grant: dict, **over) -> dict:
    """The fields of a reasoning profile the grant check reads, naming ``grant`` exactly."""
    profile = {
        "grant_id": grant["id"],
        "revision": grant["revision"],
        "epoch": grant["epoch"],
        "subject_credential_id": grant["bounds"]["subject_credential_id"],
        "destination": grant["bounds"]["destination"],
        "model": {"providerID": "anthropic", "id": MODEL},
        "max_duration_seconds": 60,
        "limits": {"max_cost_cents_per_call": 10},
    }
    return profile | over


def issued(bare, **over) -> dict:
    response = bare.issue(**over)
    assert response.status_code == 201, response.text
    return response.json()


def check(world, profile, project="same"):
    project = world.project if project == "same" else project
    return fcr._grant(profile, world.state, project)


def code(result) -> str:
    assert result["name"] == "grant" and result["status"] == "fail", result
    assert result["fix"], result
    return result["detail"].split(":", 1)[0]


def write(world, sql, *args):
    with sqlite3.connect(world.state / "tessera.sqlite3") as db:
        db.execute(sql, args)


# -- a reasoning grant: no execution running -------------------------------------------------


def test_a_reasoning_grant_with_nothing_running_is_ready_and_the_database_is_untouched(bare):
    grant = issued(bare)
    assert bare.rows("SELECT count(*) FROM execution_grants")[0][0] == 0
    path = bare.state / "tessera.sqlite3"
    before = path.read_bytes()
    result = check(bare, profile_for(grant))
    assert result["status"] == "ok", result
    assert "reasoning grant" in result["detail"]
    assert path.read_bytes() == before


def test_no_state_directory_or_no_profile_is_unchecked_not_a_pass(bare):
    grant = issued(bare)
    assert fcr._grant(profile_for(grant), None, bare.project)["status"] == "unchecked"
    assert fcr._grant(None, bare.state, bare.project)["status"] == "unchecked"


@pytest.mark.parametrize("field", ["grant_id", "revision", "epoch"])
def test_a_profile_that_does_not_name_the_grant_fully_fails(bare, field):
    grant = issued(bare)
    profile = profile_for(grant)
    del profile[field]
    assert code(check(bare, profile)) == "grant_profile_incomplete"
    assert code(check(bare, profile_for(grant, revision=True))) == "grant_profile_incomplete"


def test_a_profile_without_a_usable_per_call_bound_fails(bare):
    grant = issued(bare)
    for limits in ({}, {"max_cost_cents_per_call": 0}, {"max_cost_cents_per_call": "10"}):
        result = check(bare, profile_for(grant, limits=limits))
        assert code(result) == "grant_profile_incomplete"


def test_an_unknown_grant_id_is_missing(bare):
    grant = issued(bare)
    result = check(bare, profile_for(grant, grant_id="grant_unknown"))
    assert code(result) == "grant_missing"
    assert "tessera authority issue-reasoning" in result["fix"]


def test_a_revoked_grant_is_reported(bare):
    grant = issued(bare)
    fence = {"expected_revision": grant["revision"], "expected_epoch": grant["epoch"]}
    response = bare.client.post(
        f"/agent-authority-grants/{grant['id']}/revoke",
        json=fence,
        headers={**bare.owner, **bare.key()},
    )
    assert response.status_code == 200, response.text
    assert code(check(bare, profile_for(grant))) == "grant_revoked"


def test_an_expired_grant_is_reported(bare, monkeypatch):
    grant = issued(bare)

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(hours=3)

    monkeypatch.setattr(fcr, "datetime", Later)
    assert code(check(bare, profile_for(grant))) == "grant_expired"


def test_a_revoked_subject_credential_is_a_dead_lineage(bare):
    grant = issued(bare)
    write(
        bare,
        "UPDATE credentials SET revoked_at=? WHERE id=?",
        datetime.now(UTC).isoformat(),
        bare.credential,
    )
    assert code(check(bare, profile_for(grant))) == "grant_lineage_inactive"


def test_a_profile_at_another_revision_or_epoch_is_refused(bare):
    grant = issued(bare)
    for over in ({"revision": grant["revision"] + 1}, {"epoch": grant["epoch"] + 1}):
        assert code(check(bare, profile_for(grant, **over))) == "grant_revision_mismatch"


@pytest.mark.parametrize(
    ("over", "project", "field"),
    [
        ({}, "another-project", "project"),
        ({"destination": "elsewhere"}, "same", "destination"),
        ({"subject_credential_id": "credential_other"}, "same", "subject_credential_id"),
        ({"model": {"providerID": "anthropic", "id": "claude-other-x"}}, "same", "model"),
        ({"model": {"providerID": "openai", "id": MODEL}}, "same", "provider_id"),
        ({"max_duration_seconds": 7200}, "same", "max_duration_seconds"),
    ],
)
def test_a_grant_for_another_scope_than_the_profile_is_a_scope_mismatch(bare, over, project, field):
    grant = issued(bare)
    result = check(bare, profile_for(grant, **over), project)
    assert code(result) == "grant_scope_mismatch"
    assert field in result["detail"]


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"actions": ["model:call", "attempt:admit"]}, "actions"),
        ({"bindings": [{"execution_grant_id": "x"}]}, "bindings"),
    ],
)
def test_a_reasoning_grant_must_carry_exactly_model_call_and_no_binding(bare, change, field):
    """Issuance never writes these shapes; the check still refuses a row that has one."""
    grant = issued(bare)
    write(
        bare,
        "UPDATE agent_authority_grants SET bounds_json=? WHERE id=?",
        json.dumps(grant["bounds"] | change),
        grant["id"],
    )
    result = check(bare, profile_for(grant))
    assert code(result) == "grant_scope_mismatch" and field in result["detail"]


@pytest.mark.parametrize(
    ("bounds_json", "expected"),
    [
        ("not json", "grant_malformed"),
        ("[]", "grant_malformed"),
        ('{"kind": "reasoning"}', "grant_malformed"),
        ("KIND", "grant_kind_unsupported"),
    ],
)
def test_malformed_bounds_or_an_unknown_kind_fail_closed(bare, bounds_json, expected):
    grant = issued(bare)
    if bounds_json == "KIND":
        bounds_json = json.dumps(grant["bounds"] | {"kind": "operator"})
    sql = "UPDATE agent_authority_grants SET bounds_json=? WHERE id=?"
    write(bare, sql, bounds_json, grant["id"])
    assert code(check(bare, profile_for(grant))) == expected


def test_a_budget_below_one_call_at_the_cap_is_refused(bare):
    grant = issued(bare, budget_cents=9)  # the profile's per-call bound is 10 cents
    result = check(bare, profile_for(grant))
    assert code(result) == "grant_budget_below_call_cap"
    assert "max_cost_cents_per_call" in result["fix"]
    assert check(bare, profile_for(grant, limits={"max_cost_cents_per_call": 9}))["status"] == "ok"


def test_linked_model_spend_is_taken_out_of_the_remaining_budget(bare):
    grant = issued(bare, budget_cents=25)
    profile = profile_for(grant)
    assert check(bare, profile)["status"] == "ok"
    reserved = bare.reserve(grant["id"], worst=16)  # 9 cents left: below the 10-cent bound
    assert reserved.json()["allowed"] is True, reserved.text
    assert code(check(bare, profile)) == "grant_budget_below_call_cap"
    reserved = bare.reserve(grant["id"], worst=9, seq=2)  # nothing left
    assert reserved.json()["allowed"] is True, reserved.text
    assert code(check(bare, profile)) == "grant_budget_exhausted"


# -- the control database ----------------------------------------------------------------------


def test_a_database_without_the_grant_tables_fails(bare):
    grant = issued(bare)
    write(bare, "DROP TABLE agent_authority_grants")
    result = check(bare, profile_for(grant))
    assert result["status"] == "fail" and "older than" in result["detail"]


def test_a_missing_control_database_fails_and_nothing_is_created(bare, tmp_path):
    grant = issued(bare)
    empty = tmp_path / "empty"
    empty.mkdir()
    result = fcr._grant(profile_for(grant), empty, bare.project)
    assert result["status"] == "fail"
    assert list(empty.iterdir()) == []


def test_a_query_refused_for_access_is_unreadable(bare, monkeypatch):
    grant = issued(bare)
    real = sqlite3.connect

    class Refusing:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, *args):
            raise sqlite3.OperationalError("unable to open database file")

        def close(self):
            self.connection.close()

    monkeypatch.setattr(fcr.sqlite3, "connect", lambda *a, **k: Refusing(real(*a, **k)))
    result = check(bare, profile_for(grant))
    assert result == {
        "name": "grant",
        "status": "fail",
        "detail": fcr.UNREADABLE_CONTROL_DATABASE,
        "fix": None,
    }


def test_no_finding_carries_a_credential(bare):
    grant = issued(bare)
    results = [
        check(bare, profile_for(grant)),
        check(bare, profile_for(grant, grant_id="grant_unknown")),
        check(bare, profile_for(grant, revision=9)),
    ]
    text = json.dumps(results)
    owner = bare.owner["Authorization"].split(" ", 1)[1]
    assert bare.token not in text and owner not in text


# -- an execution-bound grant keeps its meaning -----------------------------------------------


def test_an_execution_bound_grant_is_ready_while_its_execution_grant_is_active(prepared):
    world = ReasoningGrant(prepared, MODEL)
    world.limits(per_call=20, daily=100)
    grant = world.issued
    assert "kind" not in grant["bounds"]
    profile = profile_for(grant)
    assert profile["destination"] == DESTINATION
    result = check(world, profile)
    assert result["status"] == "ok", result
    assert "execution grant" in result["detail"]
    execution = grant["bounds"]["bindings"][0]["execution_grant_id"]
    write(world, "UPDATE execution_grants SET revision=revision+1 WHERE id=?", execution)
    assert code(check(world, profile)) == "grant_binding_inactive"
