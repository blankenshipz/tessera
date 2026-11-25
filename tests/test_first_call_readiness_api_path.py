"""The first-call readiness ``spend_caps`` and ``grant`` checks through the read-only control API
(ADR 0162): no control database access, only ``--control-url`` and the service's own agent token.

Real in-process control routes answer the reads; the verdicts must equal the database path's.
Synthetic only: no network, no keys.
"""

# ruff: noqa: F811
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from readiness_grant_seed import seed_control_state  # noqa: F401
from reasoning_grant_world import DESTINATION
from test_agent_authority import prepared  # noqa: F401
from test_first_call_readiness_grant import issued, profile_for
from test_reasoning_authority_grant import Bare

from tessera_agent import first_call_readiness as fcr


@pytest.fixture
def bare(tmp_path):
    world = Bare(tmp_path)
    world.limits(provider="anthropic", per_call=20, daily=100)
    return world


def fetcher_for(world, seen=None):
    """The transport over the real in-process app, as the service's agent identity."""

    def fetch(url, headers, timeout):
        if seen is not None:
            seen.append((url, dict(headers)))
        path = url.removeprefix("http://control.test")
        response = world.client.get(path, headers=headers)
        return response.status_code, response.content

    return fetch


def api_for(world, headers=None, fetcher=None):
    api = fcr._ControlApi(
        "http://control.test",
        headers if headers is not None else dict(world.agent),
        fetcher or fetcher_for(world),
    )
    return api


def caps(world, profile, **kw):
    return fcr._spend_caps(profile, None, world.project, api_for(world, **kw))


def grant_check(world, profile, **kw):
    return fcr._grant(profile, None, world.project, api_for(world, **kw))


def code(result) -> str:
    assert result["name"] == "grant" and result["status"] == "fail", result
    assert result["fix"], result
    return result["detail"].split(":", 1)[0]


# -- the new read-only route -----------------------------------------------------------------


def test_the_limits_route_serves_only_caps_to_the_record_scoped_service(bare):
    response = bare.client.get(f"/model-spend/limits?project_id={bare.project}", headers=bare.agent)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"project_id", "limits"}
    assert [(r["provider_id"], r["per_call_cents"], r["daily_cents"]) for r in body["limits"]] == [
        ("anthropic", 20, 100)
    ]


def test_the_limits_route_is_tenant_and_scope_checked(bare, tmp_path):
    url = f"/model-spend/limits?project_id={bare.project}"
    assert bare.client.get(url).status_code in {401, 403}
    other = bare.client.get("/model-spend/limits?project_id=not-this-project", headers=bare.agent)
    assert other.status_code in {403, 404}
    # An agent without a model_spend scope is refused.
    (tmp_path / "plain").mkdir()
    plain = Bare(tmp_path / "plain", actions=["authority:consume"])
    plain.limits(provider="anthropic", per_call=20, daily=100)
    response = plain.client.get(
        f"/model-spend/limits?project_id={plain.project}", headers=plain.agent
    )
    assert response.status_code == 403
    # The agent of one project/org cannot read another's caps.
    (tmp_path / "cross").mkdir()
    cross = Bare(tmp_path / "cross")
    cross.limits(provider="anthropic", per_call=7, daily=9)
    leak = cross.client.get(f"/model-spend/limits?project_id={bare.project}", headers=cross.agent)
    assert leak.status_code in {403, 404}
    # It changes nothing: PUT is still human only.
    put = bare.client.put(
        "/model-spend/limits",
        json={
            "project_id": bare.project,
            "provider_id": "anthropic",
            "per_call_cents": 1,
            "daily_cents": 1,
        },
        headers={**bare.agent, **bare.key()},
    )
    assert put.status_code == 403


def test_the_unauthenticated_slice_probe_still_sees_the_route(bare):
    status = fcr._default_opener  # noqa: F841 - the probe treats these as "route exists"
    for method_status in (401, 403, 405, 422):
        assert method_status in fcr._ROUTE_EXISTS
    assert bare.client.get("/model-spend/limits").status_code in fcr._ROUTE_EXISTS


# -- spend caps through the API ---------------------------------------------------------------


def test_spend_caps_ok_through_the_api_equal_the_database_path(bare):
    grant = issued(bare)
    profile = profile_for(grant)
    result = caps(bare, profile)
    assert result["status"] == "ok", result
    assert result == fcr._spend_caps(profile, bare.state, bare.project)


@pytest.mark.parametrize("cap", [25, 0.5])
def test_spend_caps_verdicts_equal_the_database_path(bare, cap):
    profile = profile_for(issued(bare), limits={"max_cost_cents_per_call": cap})
    assert caps(bare, profile) == fcr._spend_caps(profile, bare.state, bare.project)


def test_no_limit_row_fails_the_same_way(tmp_path):
    world = Bare(tmp_path)
    profile = profile_for({"id": "g", "revision": 1, "epoch": 1, "bounds": world_bounds(world)})
    result = caps(world, profile)
    assert result["status"] == "fail"
    assert result == fcr._spend_caps(profile, world.state, world.project)


def world_bounds(world):
    return {"subject_credential_id": world.credential, "destination": DESTINATION}


def test_a_wildcard_default_row_covers_the_provider(tmp_path):
    world = Bare(tmp_path)
    world.limits(provider="*", per_call=20, daily=100)
    profile = profile_for({"id": "g", "revision": 1, "epoch": 1, "bounds": world_bounds(world)})
    assert caps(world, profile)["status"] == "ok"


def test_a_credential_the_api_refuses_is_a_fail_not_a_pass(bare):
    profile = profile_for(issued(bare))
    result = caps(bare, profile, headers={"Authorization": "Bearer synthetic-wrong-token"})
    assert result["status"] == "fail" and "HTTP 401" in result["detail"]
    wrong_destination = {**bare.agent, "X-Agent-Destination": "other-destination"}
    result = caps(bare, profile, headers=wrong_destination)
    assert result["status"] == "fail" and "HTTP 403" in result["detail"]
    assert "model_spend:record" in result["fix"]


@pytest.mark.parametrize(
    "answer",
    [
        (200, b"not json"),
        (200, b"[]"),
        (200, b'{"limits": "x"}'),
        (
            200,
            b'{"limits": [{"provider_id": "anthropic", "per_call_cents": "5", "daily_cents": 9}]}',
        ),
        (
            200,
            b'{"limits": [{"provider_id": "anthropic", "per_call_cents": true, "daily_cents": 9}]}',
        ),
        (200, b'{"limits": [{"per_call_cents": 5, "daily_cents": 9}]}'),
        (200, b"x" * (fcr.MAX_API_BYTES + 1)),
        (500, b""),
        (404, b""),
    ],
)
def test_an_unusable_answer_is_a_fail(bare, answer):
    profile = profile_for(issued(bare))
    result = caps(bare, profile, fetcher=lambda *_: answer)
    assert result["status"] == "fail", result


def test_an_unreachable_control_plane_is_a_fail(bare):
    def boom(*_):
        raise OSError("synthetic-secret-detail")

    result = caps(bare, profile_for(issued(bare)), fetcher=boom)
    assert result["status"] == "fail"
    assert "synthetic-secret-detail" not in json.dumps(result)


# -- the grant through the API ----------------------------------------------------------------


def test_a_reasoning_grant_through_the_api_is_ok_and_equals_the_database_path(bare):
    grant = issued(bare)
    profile = profile_for(grant)
    result = grant_check(bare, profile)
    assert result["status"] == "ok", result
    assert result == fcr._grant(profile, bare.state, bare.project)


def revoke(world, grant):
    response = world.client.post(
        f"/agent-authority-grants/{grant['id']}/revoke",
        json={"expected_revision": grant["revision"], "expected_epoch": grant["epoch"]},
        headers={**world.owner, **world.key()},
    )
    assert response.status_code == 200, response.text


def test_a_revoked_grant_is_reported_the_same_way(bare):
    grant = issued(bare)
    revoke(bare, grant)
    result = grant_check(bare, profile_for(grant))
    assert code(result) == "grant_revoked"
    assert result == fcr._grant(profile_for(grant), bare.state, bare.project)


def test_a_dead_lineage_is_reported_from_the_effective_status(bare):
    grant = issued(bare)
    body = bare.client.get(f"/agent-authority-grants/{grant['id']}", headers=bare.agent).json()
    body["effective_status"] = "fenced"
    answer = (200, json.dumps(body).encode())
    result = grant_check(bare, profile_for(grant), fetcher=lambda *_: answer)
    assert code(result) == "grant_lineage_inactive"


def test_revision_scope_and_budget_findings_equal_the_database_path(bare):
    grant = issued(bare)
    for over in (
        {"revision": grant["revision"] + 1},
        {"epoch": grant["epoch"] + 1},
        {"destination": "elsewhere"},
        {"subject_credential_id": "cred_other"},
        {"limits": {"max_cost_cents_per_call": 10_000}},
        {"max_duration_seconds": 10**6},
    ):
        profile = profile_for(grant, **over)
        via_api = grant_check(bare, profile)
        assert via_api["status"] == "fail", over
        assert via_api == fcr._grant(profile, bare.state, bare.project), over


def test_a_wrong_project_is_a_scope_mismatch(bare):
    grant = issued(bare)
    result = fcr._grant(profile_for(grant), None, "another-project", api_for(bare))
    assert code(result) == "grant_scope_mismatch"


def test_an_unknown_grant_is_missing_and_a_profile_without_one_is_incomplete(bare):
    grant = issued(bare)
    assert code(grant_check(bare, profile_for(grant, grant_id="grant_unknown"))) == "grant_missing"
    profile = profile_for(grant)
    del profile["epoch"]
    assert code(grant_check(bare, profile)) == "grant_profile_incomplete"


def test_another_agents_grant_is_not_readable(bare, tmp_path):
    grant = issued(bare)
    (tmp_path / "other").mkdir()
    other = Bare(tmp_path / "other")
    fetch = fetcher_for(other)
    result = grant_check(bare, profile_for(grant), headers=dict(other.agent), fetcher=fetch)
    assert result["status"] == "fail"


def test_a_refused_credential_is_a_fail(bare):
    grant = issued(bare)
    result = grant_check(
        bare, profile_for(grant), headers={"Authorization": "Bearer synthetic-wrong-token"}
    )
    assert result["status"] == "fail" and "HTTP 401" in result["detail"]
    assert "authority:consume" in result["fix"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.pop("bounds"),
        lambda b: b.update(bounds="x"),
        lambda b: b.update(bounds={}),
        lambda b: b.update(id="grant_other"),
        lambda b: b.update(remaining_budget_cents="9"),
        lambda b: b.pop("effective_status"),
        lambda b: b.update(revision=None),
    ],
)
def test_a_malformed_grant_answer_fails_closed(bare, mutate):
    grant = issued(bare)
    body = bare.client.get(f"/agent-authority-grants/{grant['id']}", headers=bare.agent).json()
    mutate(body)
    answer = (200, json.dumps(body).encode())
    result = grant_check(bare, profile_for(grant), fetcher=lambda *_: answer)
    assert result["status"] == "fail" and result["detail"].startswith("grant_"), result


@pytest.mark.parametrize("answer", [(200, b"nope"), (200, b"[]"), (500, b""), (429, b"")])
def test_an_unusable_grant_read_is_a_fail(bare, answer):
    grant = issued(bare)
    result = grant_check(bare, profile_for(grant), fetcher=lambda *_: answer)
    assert result["status"] == "fail", result


def test_an_unreachable_control_plane_fails_the_grant_without_echoing_detail(bare):
    def boom(*_):
        raise OSError("synthetic-secret-detail")

    result = grant_check(bare, profile_for(issued(bare)), fetcher=boom)
    assert result["status"] == "fail"
    assert "synthetic-secret-detail" not in json.dumps(result)


def test_an_exhausted_budget_is_reported(bare):
    grant = issued(bare)
    body = bare.client.get(f"/agent-authority-grants/{grant['id']}", headers=bare.agent).json()
    body["remaining_budget_cents"] = 0
    answer = (200, json.dumps(body).encode())
    result = grant_check(bare, profile_for(grant), fetcher=lambda *_: answer)
    assert code(result) == "grant_budget_exhausted"


def test_an_expired_grant_is_reported(bare, monkeypatch):
    grant = issued(bare)

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(hours=3)

    monkeypatch.setattr(fcr, "datetime", Later)
    assert code(grant_check(bare, profile_for(grant))) == "grant_expired"


# -- the execution kind cannot be fully read with the agent credential ---------------------------


def test_an_execution_grant_through_the_api_is_unchecked_never_ok(prepared):
    # Bindings live in execution grants the agent credential cannot read: not a pass.
    body = {
        "id": "g1",
        "status": "active",
        "revision": 1,
        "epoch": 1,
        "project_id": "p",
        "subject_credential_id": "c",
        "effective_status": "active",
        "remaining_budget_cents": 50,
        "bounds": {
            "kind": "execution",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "budget_cents": 50,
            "subject_credential_id": "c",
            "destination": "d",
            "model": "m",
            "max_duration_seconds": 60,
            "actions": ["attempt:status"],
            "bindings": [{"execution_grant_id": "x"}],
        },
    }
    profile = {
        "grant_id": "g1",
        "revision": 1,
        "epoch": 1,
        "subject_credential_id": "c",
        "destination": "d",
        "model": {"providerID": "anthropic", "id": "m"},
        "max_duration_seconds": 60,
        "limits": {"max_cost_cents_per_call": 10},
    }
    api = fcr._ControlApi("http://x", {}, lambda *_: (200, json.dumps(body).encode()))
    result = fcr._grant(profile, None, "p", api)
    assert result["status"] == "unchecked", result


# -- wiring: run_checks, the token and the CLI ------------------------------------------------


def test_no_database_and_no_url_stays_unchecked(bare):
    profile = profile_for(issued(bare))
    assert fcr._spend_caps(profile, None, bare.project)["status"] == "unchecked"
    assert fcr._grant(profile, None, bare.project)["status"] == "unchecked"


def test_an_unreadable_token_leaves_both_checks_unchecked(bare, tmp_path):
    api = fcr._control_api(
        "http://control.test", {"destination": "d"}, tmp_path, "agent-token", None
    )
    assert api.unavailable
    profile = profile_for(issued(bare))
    assert fcr._spend_caps(profile, None, bare.project, api)["status"] == "unchecked"
    assert fcr._grant(profile, None, bare.project, api)["status"] == "unchecked"


def test_a_bad_control_url_never_reads(bare, tmp_path):
    token = tmp_path / "agent-token"
    token.write_text("synthetic-token")
    token.chmod(0o600)
    for url in ("ftp://x", "http://user:pw@host", "file:///x"):
        api = fcr._control_api(url, {"destination": "d"}, tmp_path, "agent-token", None)
        assert api.unavailable


def test_the_token_is_sent_only_to_the_two_reads_and_never_printed(bare, tmp_path):
    token = tmp_path / "agent-token"
    token.write_text(bare.token + "\n")
    token.chmod(0o600)
    seen = []
    api = fcr._control_api(
        "http://control.test/",
        {"destination": DESTINATION},
        tmp_path,
        "agent-token",
        fetcher_for(bare, seen),
    )
    assert not api.unavailable and bare.token not in repr(api)
    profile = profile_for(issued(bare))
    results = [
        fcr._spend_caps(profile, None, bare.project, api),
        fcr._grant(profile, None, bare.project, api),
    ]
    assert [r["status"] for r in results] == ["ok", "ok"], results
    assert bare.token not in json.dumps(results)
    assert [u for u, _ in seen] == [
        f"http://control.test/model-spend/limits?project_id={bare.project}",
        f"/agent-authority-grants/{profile['grant_id']}".join(["http://control.test", ""]),
    ]
    assert all(h["X-Agent-Destination"] == DESTINATION for _, h in seen)


def test_a_group_readable_token_file_is_refused(bare, tmp_path):
    token = tmp_path / "agent-token"
    token.write_text("synthetic-token")
    token.chmod(0o640)
    assert fcr._agent_token(tmp_path, "agent-token") is None
    token.chmod(0o600)
    token.write_text("has space")
    assert fcr._agent_token(tmp_path, "agent-token") is None
    token.write_text("synthetic-token\n")
    assert fcr._agent_token(tmp_path, "agent-token") == "synthetic-token"


# -- a single in-unit style run with no database path --------------------------------------------


def _seeded_fetcher(control, seen):
    """Answers the two reads from the seeded control state, as the control plane would."""
    import sqlite3

    def fetch(url, headers, timeout):
        seen.append((url, dict(headers)))
        db = sqlite3.connect(control / "tessera.sqlite3")
        db.row_factory = sqlite3.Row
        try:
            if "/model-spend/limits" in url:
                limits = [
                    {
                        "provider_id": r["provider_id"],
                        "revision": r["revision"],
                        "per_call_cents": r["per_call_micro"] / 1_000_000,
                        "daily_cents": r["daily_micro"] / 1_000_000,
                    }
                    for r in db.execute("SELECT * FROM model_spend_limits")
                ]
                return 200, json.dumps({"limits": limits}).encode()
            row = db.execute("SELECT * FROM agent_authority_grants").fetchone()
            body = dict(row)
            bounds = json.loads(body.pop("bounds_json"))
            body |= {
                "bounds": bounds,
                "remaining_budget_cents": bounds["budget_cents"],
                "effective_status": "active",
            }
            return 200, json.dumps(body).encode()
        finally:
            db.close()

    return fetch


from test_first_call_readiness import anthropic, world  # noqa: E402,F401


def test_one_run_with_only_a_control_url_reaches_ready(world):
    seen = []
    args = {**world["args"], "control_state_dir": None}
    report = fcr.run_checks(**args, fetcher=_seeded_fetcher(world["control"], seen))
    assert report["overall"] == "ready", report["checks"]
    assert report["unchecked"] == []
    assert len(seen) == 2 and all("Authorization" in h for _, h in seen)
    assert "synthetic-agent" not in json.dumps(report)


def test_the_database_path_still_wins_when_given(world):
    def never(*_):
        raise AssertionError("the API must not be read when a database path is given")

    report = fcr.run_checks(**world["args"], fetcher=never)
    assert report["overall"] == "ready", report["checks"]


def test_a_missing_token_without_a_database_is_not_fully_checked(world):
    (world["root"] / "agent-token").unlink()
    args = {**world["args"], "control_state_dir": None}
    report = fcr.run_checks(**args, fetcher=lambda *_: (200, b"{}"))
    assert report["overall"] == "not_fully_checked"
    assert report["unchecked"] == ["spend_caps", "grant"]


def test_a_refused_token_makes_the_run_not_ready(world):
    args = {**world["args"], "control_state_dir": None}
    report = fcr.run_checks(**args, fetcher=lambda *_: (403, b""))
    assert report["overall"] == "not_ready"
    assert {c["name"] for c in report["checks"] if c["status"] == "fail"} == {
        "spend_caps",
        "grant",
    }


def _exec_body(bindings_active):
    body = {
        "id": "g1",
        "status": "active",
        "revision": 1,
        "epoch": 1,
        "project_id": "p",
        "subject_credential_id": "c",
        "effective_status": "active",
        "remaining_budget_cents": 50,
        "bounds": {
            "kind": "execution",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "budget_cents": 50,
            "subject_credential_id": "c",
            "destination": "d",
            "model": "m",
            "max_duration_seconds": 60,
            "actions": ["attempt:status"],
            "bindings": [{"execution_grant_id": "x"}],
        },
    }
    if bindings_active is not None:
        body["bindings_active"] = bindings_active
    return body


def _exec_profile():
    return {
        "grant_id": "g1",
        "revision": 1,
        "epoch": 1,
        "subject_credential_id": "c",
        "destination": "d",
        "model": {"providerID": "anthropic", "id": "m"},
        "max_duration_seconds": 60,
        "limits": {"max_cost_cents_per_call": 10},
    }


@pytest.mark.parametrize(
    ("value", "status"),
    [(True, "ok"), (False, "fail"), (None, "unchecked"), ("yes", "unchecked"), (1, "unchecked")],
)
def test_execution_bindings_reported_by_the_api(value, status):
    body = _exec_body(value)
    api = fcr._ControlApi("http://x", {}, lambda *_: (200, json.dumps(body).encode()))
    result = fcr._grant(_exec_profile(), None, "p", api)
    assert result["status"] == status, result
    if value is False:
        assert "grant_binding_inactive" in json.dumps(result)


def test_the_control_route_reports_bindings_active_for_an_execution_grant(prepared):
    import sqlite3

    from test_agent_authority import issue

    state, client, owner, agent, _project, bounds, _worker = prepared
    grant = issue(prepared)
    url = f"/agent-authority-grants/{grant['id']}"
    assert client.get(url, headers=agent).json()["bindings_active"] is True
    assert client.get(url, headers=owner).json()["bindings_active"] is True
    binding = bounds["bindings"][0]["execution_grant_id"]
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute("UPDATE execution_grants SET epoch=epoch+1 WHERE id=?", (binding,))
    assert client.get(url, headers=agent).json()["bindings_active"] is False
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute("UPDATE execution_grants SET epoch=epoch-1 WHERE id=?", (binding,))
        db.execute("UPDATE execution_grants SET project_id='elsewhere' WHERE id=?", (binding,))
    assert client.get(url, headers=agent).json()["bindings_active"] is False


def test_a_reasoning_grant_gets_no_bindings_field(bare):
    grant = issued(bare)
    response = bare.client.get(f"/agent-authority-grants/{grant['id']}", headers=bare.agent)
    assert "bindings_active" not in response.json()
