"""Model-spend limits, reservations and settlements over the control API (ADR 0090).

Synthetic state only: no model call, no network, no key."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from campaign_fixture import Env
from fastapi.testclient import TestClient

from tessera_control.api import create_app
from tessera_control.store import canonical_digest

DESTINATION = "planner.local"
RECORD = ["model_spend:record"]
SHA = "a" * 64


class World:
    def __init__(self, tmp_path):
        self.env = Env(tmp_path)
        self.client = TestClient(create_app(self.env.state))
        self.project = self.env.project["id"]
        self.n = 0
        self.issued: dict[str, dict] = {}

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
        headers = {"Authorization": f"Bearer {token}", "X-Agent-Destination": DESTINATION}
        self.issued[name] = headers
        return headers

    def human(self):
        return self.credential("human", kind="human_session")

    def agent(self, actions=RECORD):
        return self.credential(
            "agent-" + "-".join(a.replace(":", "_") for a in actions),
            kind="agent",
            project_id=self.project,
            external_agent={"actions": actions, "destination": DESTINATION},
        )

    def key(self):
        self.n += 1
        return {"Idempotency-Key": f"k{self.n}"}

    def limits(self, per_call=5, daily=20, provider="*", headers=None, **over):
        body = {
            "project_id": self.project,
            "provider_id": provider,
            "per_call_cents": per_call,
            "daily_cents": daily,
            **over,
        }
        return self.client.put(
            "/model-spend/limits", json=body, headers={**(headers or self.human()), **self.key()}
        )

    def reserve(self, seq=1, cents=1.0, run="run-1", provider="anthropic", headers=None, **over):
        body = {
            "project_id": self.project,
            "run_id": run,
            "seq": seq,
            "provider_id": provider,
            "model": "claude-test",
            "worst_case_cents": cents,
            **over,
        }
        return self.client.post(
            "/model-spend/reservations",
            json=body,
            headers={**(headers or self.agent()), **self.key()},
        )

    def settle(self, call_id, cents, sha=SHA, headers=None, key=None):
        return self.client.post(
            f"/model-spend/reservations/{call_id}/settlement",
            json={"actual_cents": cents, "receipt_sha256": sha},
            headers={**(headers or self.agent()), **(key or self.key())},
        )

    def release(self, call_id, headers=None):
        return self.client.post(
            f"/model-spend/reservations/{call_id}/release",
            json={"reason": "not_sent"},
            headers={**(headers or self.agent()), **self.key()},
        )

    def read(self, headers=None, project=None, days=7):
        return self.client.get(
            "/model-spend",
            params={"project_id": project or self.project, "days": days},
            headers=headers or self.human(),
        )

    def rows(self, sql, *args):
        with sqlite3.connect(self.env.state / "tessera.sqlite3") as db:
            db.row_factory = sqlite3.Row
            return db.execute(sql, args).fetchall()

    def events(self, kind):
        return [
            json.loads(r["payload_json"])
            for r in self.rows("SELECT payload_json FROM events WHERE event_type=?", kind)
        ]

    def other_org_human(self):
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


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_nothing_is_allowed_until_a_human_sets_a_limit(world):
    body = world.reserve().json()
    assert body == {"allowed": False, "code": "model_spend_not_configured", "day": body["day"]}
    (event,) = world.events("model_spend.refused")
    assert event["code"] == "model_spend_not_configured"
    assert world.rows("SELECT * FROM model_spend_calls") == []


def test_limits_are_human_only_and_validated(world):
    assert world.limits(headers=world.agent(["model_spend:read"])).status_code == 403
    assert world.limits(headers=world.agent(RECORD)).status_code == 403
    assert world.limits(per_call=9, daily=3).status_code in (400, 409, 422)
    assert world.limits(per_call=-1).status_code == 422
    assert world.limits(provider="Bad Provider").status_code == 422
    ok = world.limits(per_call=5, daily=20)
    assert ok.status_code == 200 and ok.json()["revision"] == 1
    again = world.limits(per_call=6, daily=20)
    assert again.json()["revision"] == 2 and again.json()["per_call_cents"] == 6
    assert world.events("model_spend.limits_set")


def test_a_reservation_within_the_caps_is_recorded_and_replays(world):
    world.limits()
    key = {"Idempotency-Key": "same"}
    first = world.client.post(
        "/model-spend/reservations",
        json={
            "project_id": world.project,
            "run_id": "run-1",
            "seq": 1,
            "provider_id": "anthropic",
            "model": "claude-test",
            "worst_case_cents": 2.5,
        },
        headers={**world.agent(), **key},
    )
    body = first.json()
    assert first.status_code == 200 and body["allowed"] is True and body["status"] == "reserved"
    assert body["reserved_micro_cents"] == 2_500_000
    assert body["limits"] == {"per_call_cents": 5, "daily_cents": 20}
    assert body["daily"]["reserved_cents"] == 2.5
    replay = world.client.post(
        "/model-spend/reservations",
        json={
            "project_id": world.project,
            "run_id": "run-1",
            "seq": 1,
            "provider_id": "anthropic",
            "model": "claude-test",
            "worst_case_cents": 2.5,
        },
        headers={**world.agent(), **key},
    )
    assert replay.json() == body  # the same Idempotency-Key replays the stored answer
    # The same (run, seq) under a new key returns the stored reservation, not a second one.
    again = world.reserve(seq=1, cents=2.5).json()
    assert again["call_id"] == body["call_id"]
    assert len(world.rows("SELECT * FROM model_spend_calls")) == 1
    assert world.events("model_spend.reserved")


def test_a_reservation_for_another_model_under_the_same_sequence_is_refused(world):
    world.limits()
    world.reserve(seq=1)
    assert world.reserve(seq=1, model="other-model").status_code == 409


def test_the_per_call_cap_refuses_before_anything_is_reserved(world):
    world.limits(per_call=2, daily=20)
    body = world.reserve(cents=2.01).json()
    assert body["allowed"] is False and body["code"] == "model_spend_cap_exceeded"
    assert body["cap"] == "per_call" and body["limit_cents"] == 2
    assert world.rows("SELECT * FROM model_spend_calls") == []
    assert world.events("model_spend.refused")


def test_the_daily_cap_counts_settled_spend_and_open_reservations(world):
    world.limits(per_call=6, daily=10)
    a = world.reserve(seq=1, cents=4).json()
    assert world.settle(a["call_id"], 3.5).status_code == 200
    b = world.reserve(seq=2, cents=4).json()  # 3.5 settled + 4 open = 7.5
    assert b["allowed"] is True
    c = world.reserve(seq=3, cents=3).json()  # 7.5 + 3 = 10.5 > 10
    assert c["allowed"] is False and c["cap"] == "daily" and c["would_be_cents"] == 10.5
    # Releasing the open reservation frees its budget.
    assert world.release(b["call_id"]).json()["status"] == "released"
    assert world.reserve(seq=3, cents=3).json()["allowed"] is True


def test_a_provider_limit_governs_that_provider_and_the_default_the_rest(world):
    world.limits(provider="*", per_call=1, daily=1)
    world.limits(provider="openai", per_call=5, daily=5)
    assert world.reserve(provider="openai", cents=4).json()["allowed"] is True
    assert world.reserve(seq=2, provider="anthropic", cents=2).json()["allowed"] is False
    assert world.reserve(seq=3, provider="anthropic", cents=0.9).json()["allowed"] is True
    # The default row's daily total does not include openai spend.
    assert world.reserve(seq=4, provider="anthropic", cents=0.2).json()["allowed"] is False


def test_settlement_records_actual_cost_the_receipt_digest_and_replays(world):
    world.limits(per_call=5, daily=20)
    call = world.reserve(cents=2).json()["call_id"]
    key = {"Idempotency-Key": "settle-1"}
    first = world.settle(call, 1.25, key=key)
    body = first.json()
    assert first.status_code == 200 and body["settled_micro_cents"] == 1_250_000
    assert body["over_reservation"] is False and body["cap_exceeded"] is False
    assert body["receipt_sha256"] == SHA and body["daily"]["settled_cents"] == 1.25
    assert world.settle(call, 1.25, key=key).json() == body  # key replay
    assert world.settle(call, 1.25).json()["status"] == "settled"  # same body, new key
    assert world.settle(call, 1.3).status_code == 409  # a different body is refused
    assert world.settle(call, 1.25, sha="b" * 64).status_code == 409
    assert world.release(call).status_code == 409
    (event,) = world.events("model_spend.settled")
    assert event["receipt_sha256"] == SHA and event["settled_micro_cents"] == 1_250_000


def test_actual_above_the_reservation_is_recorded_truthfully_and_flagged(world):
    world.limits(per_call=5, daily=6)
    call = world.reserve(cents=2).json()["call_id"]
    body = world.settle(call, 5.5).json()
    assert body["over_reservation"] is True and body["cap_exceeded"] is True  # above per-call
    call2 = world.reserve(seq=2, cents=0.6).json()
    assert call2["allowed"] is False  # 5.5 settled + 0.6 > 6
    assert world.read().json()["today"]["settled_cents"] == 5.5


def test_a_released_call_may_be_reserved_again_and_a_settled_one_may_not_be_released(world):
    world.limits()
    first = world.reserve(seq=1).json()["call_id"]
    assert world.release(first).status_code == 200
    assert world.release(first).json()["status"] == "released"  # idempotent
    assert world.settle(first, 1).status_code == 409
    retry = world.reserve(seq=1).json()
    assert retry["allowed"] is True and retry["call_id"] != first
    assert [r["attempt"] for r in world.rows("SELECT attempt FROM model_spend_calls")] == [1, 2]


def test_reads_report_limits_totals_history_and_open_reservations(world):
    world.limits(per_call=5, daily=20)
    a = world.reserve(seq=1, cents=2).json()["call_id"]
    world.settle(a, 1.5)
    world.reserve(seq=2, cents=3)
    body = world.read().json()
    assert body["limits"] == [
        {"provider_id": "*", "per_call_cents": 5, "daily_cents": 20, "revision": 1}
    ]
    assert body["today"] == {"settled_cents": 1.5, "reserved_cents": 3, "calls": 2}
    assert body["by_day"] == [{"day": body["day"], "settled_cents": 1.5, "calls": 1}]
    assert body["by_provider"] == {"anthropic": {"settled_cents": 1.5, "calls": 1}}
    (open_row,) = body["open_reservations"]
    assert open_row["seq"] == 2 and open_row["reserved_cents"] == 3
    # An agent reads only with the read scope.
    assert world.read(headers=world.agent(["model_spend:read"])).status_code == 200
    assert world.read(headers=world.agent(RECORD)).status_code == 403
    assert world.client.get("/model-spend", params={"project_id": world.project}).status_code == 401


def test_recording_needs_the_record_scope_and_a_matching_destination(world):
    world.limits()
    assert world.reserve(headers=world.agent(["model_spend:read"])).status_code == 403
    assert world.reserve(headers=world.human()).status_code == 403
    wrong = {**world.agent(), "X-Agent-Destination": "elsewhere"}
    assert world.reserve(headers=wrong).status_code == 403
    assert world.client.post("/model-spend/reservations", json={}).status_code == 401


def test_another_organization_cannot_read_reserve_or_limit_the_project(world):
    world.limits()
    call = world.reserve().json()["call_id"]
    other = world.other_org_human()
    assert world.read(headers=other).status_code == 404
    assert world.limits(headers=other).status_code == 404
    spy = world.client.post(
        f"/model-spend/reservations/{call}/release",
        json={"reason": "not_sent"},
        headers={**other, **world.key()},
    )
    assert spy.status_code in (403, 404)
    assert world.rows("SELECT status FROM model_spend_calls")[0]["status"] == "reserved"


def test_an_agent_cannot_use_another_projects_id(world):
    world.limits()
    assert world.reserve(project_id="project_elsewhere").status_code == 404


def test_no_row_or_event_holds_a_secret_or_a_prompt(world):
    world.limits()
    call = world.reserve().json()["call_id"]
    world.settle(call, 1)
    text = json.dumps(
        [dict(r) for r in world.rows("SELECT * FROM model_spend_calls")]
        + world.events("model_spend.reserved")
        + world.events("model_spend.settled")
    )
    for word in ("prompt", "completion", "api-key", "x-api-key", "sk-ant", "Bearer"):
        assert word not in text


# -- a person resolves an open reservation (ADR 0090) ------------------------------------------


def resolve(
    world, call_id, outcome="not_sent", reason="the provider never answered", headers=None, **over
):
    body = {"outcome": outcome, "reason": reason, **over}
    return world.client.post(
        f"/model-spend/reservations/{call_id}/resolution",
        json=body,
        headers={**(headers or world.human()), **world.key()},
    )


def test_a_person_releases_an_open_reservation_and_the_day_is_free_again(world):
    world.limits(per_call=3, daily=3)
    first = world.reserve(seq=1, cents=3).json()
    assert world.reserve(seq=2, cents=1).json()["allowed"] is False  # the open one counts
    done = resolve(world, first["call_id"], "not_sent")
    assert done.status_code == 200
    body = done.json()
    assert body["status"] == "released" and body["resolution"] == "person_released"
    assert body["settled_micro_cents"] == 0 and body["over_reservation"] is False
    assert body["cap_exceeded"] is False and body["daily"] == {
        "settled_cents": 0,
        "reserved_cents": 0,
    }
    assert world.reserve(seq=2, cents=1).json()["allowed"] is True
    (event,) = world.events("model_spend.resolved")
    assert event["outcome"] == "not_sent" and event["reason"] == "the provider never answered"
    assert event["call_id"] == first["call_id"] and event["resolution"] == "person_released"
    row = world.rows("SELECT * FROM model_spend_calls WHERE id=?", first["call_id"])[0]
    assert row["resolution"] == "person_released" and row["resolved_by_principal_id"]
    assert row["resolution_reason"] == "the provider never answered"


def test_a_person_settles_an_open_reservation_as_spent_even_above_the_cap(world):
    world.limits(per_call=2, daily=3)
    call = world.reserve(seq=1, cents=2).json()["call_id"]
    done = resolve(world, call, "spent", actual_cents=3.5, reason="invoice shows the call ran")
    body = done.json()
    assert done.status_code == 200 and body["status"] == "settled"
    assert body["resolution"] == "person_settled" and body["settled_micro_cents"] == 3_500_000
    assert body["over_reservation"] is True and body["cap_exceeded"] is True
    assert body["daily"]["settled_cents"] == 3.5 and body["daily"]["reserved_cents"] == 0
    assert world.read().json()["today"]["settled_cents"] == 3.5
    assert world.read().json()["open_reservations"] == []


def test_resolution_is_human_only_and_tenant_scoped(world):
    world.limits()
    call = world.reserve(seq=1).json()["call_id"]
    assert resolve(world, call, headers=world.agent()).status_code == 403
    assert resolve(world, call, headers=world.other_org_human()).status_code == 404
    assert world.rows("SELECT status FROM model_spend_calls")[0]["status"] == "reserved"


def test_resolution_replays_and_a_different_second_resolution_conflicts(world):
    world.limits()
    call = world.reserve(seq=1, cents=1).json()["call_id"]
    key = world.key()
    body = {"outcome": "not_sent", "reason": "never sent"}
    url = f"/model-spend/reservations/{call}/resolution"
    first = world.client.post(url, json=body, headers={**world.human(), **key})
    again = world.client.post(url, json=body, headers={**world.human(), **key})
    assert first.status_code == again.status_code == 200 and first.json() == again.json()
    assert len(world.events("model_spend.resolved")) == 1
    # A new key with the same outcome is idempotent; another outcome is refused.
    assert resolve(world, call, "not_sent").status_code == 200
    assert resolve(world, call, "spent", actual_cents=1).status_code == 409
    assert len(world.events("model_spend.resolved")) == 1


def test_an_agent_resolved_call_cannot_be_resolved_again_by_a_person(world):
    world.limits()
    call = world.reserve(seq=1).json()["call_id"]
    assert world.release(call).status_code == 200
    assert resolve(world, call, "not_sent").status_code == 409
    row = world.rows("SELECT resolution FROM model_spend_calls")[0]
    assert row["resolution"] == "agent_released"


@pytest.mark.parametrize(
    "over",
    [
        {"outcome": "spent"},  # amount missing
        {"outcome": "not_sent", "actual_cents": 1},  # amount forbidden
        {"outcome": "spent", "actual_cents": -1},
        {"outcome": "other"},
        {"reason": ""},
        {"reason": "x" * 501},
        {"reason": "line\nbreak"},
    ],
)
def test_resolution_validation(world, over):
    world.limits()
    call = world.reserve(seq=1).json()["call_id"]
    body = {"outcome": "not_sent", "reason": "ok", **over}
    response = world.client.post(
        f"/model-spend/reservations/{call}/resolution",
        json=body,
        headers={**world.human(), **world.key()},
    )
    assert response.status_code == 422
    assert world.rows("SELECT status FROM model_spend_calls")[0]["status"] == "reserved"


NO_RUN = object()


def status(world, call_id, headers=None, run_id=NO_RUN):
    """An agent names its run (the supervisor's, ``run-1`` here) unless a test says otherwise; a
    human passes none unless a test says otherwise."""
    if run_id is NO_RUN:
        run_id = "run-1" if headers is None else None
    return world.client.get(
        f"/model-spend/reservations/{call_id}",
        params={} if run_id is None else {"run_id": run_id},
        headers=headers or world.agent(),
    )


def test_a_reservation_shows_its_status_and_who_resolved_it(world):
    world.limits(per_call=5, daily=20)
    open_call = world.reserve(seq=1, cents=1).json()["call_id"]
    expected_open = {
        "call_id": open_call,
        "status": "reserved",
        "resolution": None,
        "settled_micro_cents": None,
    }
    got = status(world, open_call)
    assert got.status_code == 200
    assert {k: got.json()[k] for k in expected_open} == expected_open and got.json()["day"]
    assert set(got.json()) == {*expected_open, "day"}  # nothing else: no reason, no content
    settled = world.reserve(seq=2, cents=2).json()["call_id"]
    assert world.settle(settled, 1.5).status_code == 200
    got = status(world, settled).json()
    assert (got["status"], got["resolution"], got["settled_micro_cents"]) == (
        "settled",
        "agent_settled",
        1_500_000,
    )
    released = world.reserve(seq=3, cents=1).json()["call_id"]
    assert world.release(released).status_code == 200
    got = status(world, released).json()
    assert (got["status"], got["resolution"], got["settled_micro_cents"]) == (
        "released",
        "agent_released",
        None,  # a released call settled nothing
    )


def test_a_persons_resolution_is_visible_to_the_agent_that_reads_the_call(world):
    world.limits(per_call=5, daily=20)
    unsent = world.reserve(seq=1, cents=1).json()["call_id"]
    spent = world.reserve(seq=2, cents=2).json()["call_id"]
    assert resolve(world, unsent, "not_sent", reason="never sent").status_code == 200
    assert resolve(world, spent, "spent", actual_cents=2.25, reason="ran").status_code == 200
    a, b = status(world, unsent).json(), status(world, spent).json()
    assert (a["status"], a["resolution"]) == ("released", "person_released")
    assert (b["status"], b["resolution"], b["settled_micro_cents"]) == (
        "settled",
        "person_settled",
        2_250_000,
    )
    assert "never sent" not in str(a) and "ran" not in b  # the reason text is not returned


def test_a_human_may_read_a_reservation_and_the_read_changes_nothing(world):
    world.limits()
    call = world.reserve(seq=1).json()["call_id"]
    events = world.rows("SELECT COUNT(*) AS n FROM events")[0]["n"]
    rows = world.rows("SELECT * FROM model_spend_calls")
    assert status(world, call, headers=world.human()).status_code == 200
    assert world.rows("SELECT COUNT(*) AS n FROM events")[0]["n"] == events
    assert [tuple(r) for r in world.rows("SELECT * FROM model_spend_calls")] == [
        tuple(r) for r in rows
    ]


def test_the_status_read_needs_the_record_scope_and_the_right_organization(world):
    world.limits()
    call = world.reserve(seq=1).json()["call_id"]
    assert status(world, call, headers=world.agent(["model_spend:read"])).status_code == 403
    assert status(world, call, headers=world.other_org_human()).status_code == 404
    assert status(world, "modelspend_" + "0" * 32).status_code == 404


def test_an_agent_reads_only_calls_of_its_own_run(world):
    world.limits()
    mine = world.reserve(seq=1, run="run-1").json()["call_id"]
    theirs = world.reserve(seq=1, run="run-2").json()["call_id"]
    assert status(world, mine, run_id="run-1").status_code == 200
    assert status(world, theirs, run_id="run-2").status_code == 200
    # Another run's call, named with the agent's own run id, is "not found": no confirmation.
    other = status(world, theirs, run_id="run-1")
    assert other.status_code == 404
    assert other.json() == status(world, "modelspend_" + "0" * 32, run_id="run-1").json()


def test_an_agent_must_name_its_run(world):
    world.limits()
    call = world.reserve(seq=1).json()["call_id"]
    assert status(world, call, run_id=None).status_code == 422


def test_a_human_may_omit_the_run_but_a_wrong_one_is_not_found(world):
    world.limits()
    call = world.reserve(seq=1, run="run-1").json()["call_id"]
    human = world.human()
    assert status(world, call, headers=human, run_id=None).status_code == 200
    assert status(world, call, headers=human, run_id="run-1").status_code == 200
    assert status(world, call, headers=human, run_id="run-9").status_code == 404
