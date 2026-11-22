"""ADR 0087 s4 / ADR 0111: watch decision requests are advisory records, never admitted."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
import test_execution as fx
from campaign_runner_harness import World, stage_spec
from fastapi.testclient import TestClient
from test_stage_watch_reports import Harness

from tessera_control.api import create_app
from tessera_control.execution import ExecutionStore
from tessera_control.stage_watch import watch_receipt_section
from tessera_control.stage_watch_decisions import DECISION_EVENT_TYPE
from tessera_control.stage_watch_limits import (
    MAX_DECISIONS_PER_OPERATION,
    PERMANENT_REASONS,
    REASON_STATUS,
    TRANSIENT_REASONS,
)
from tessera_control.store import _connect, _parse_utc

CONTRACT = "d" * 64
GIB = 2**30
MISMATCH = "Watch decision request does not match the external operation."
NOT_FOUND = "External operation was not found."
ORDER = "Watch decision requests cannot be recorded out of order."
BUDGET = "Watch decision request budget exhausted."


def _post(client, operation_id, body, key, token):
    response = client.post(
        f"/worker/operations/{operation_id}/watch-decisions",
        json=body,
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
    )
    # Every refusal carries a closed reason whose status is the shared table's; a schema
    # error (422) and an auth failure carry none.
    if response.status_code in (404, 409, 425):
        reply = response.json()
        assert set(reply) == {"detail", "reason"}, reply
        assert reply["reason"] in PERMANENT_REASONS | TRANSIENT_REASONS
        assert response.status_code == REASON_STATUS[reply["reason"]]
    return response


def _body(lease, issued, seq, kind, reason, at, **overrides) -> dict:
    body = {
        "lease_id": lease["id"],
        "expected_epoch": lease["epoch"],
        "kind": kind,
        "reason": reason,
        "observed_at": (issued + timedelta(seconds=at)).isoformat(),
        "decision_seq": seq,
        "contract_digest": CONTRACT,
    }
    if kind == "resize_request":
        body["requested_memory_bytes"] = 2 * GIB
    return body | overrides


def _state_without_events(state: Path) -> dict[str, list]:
    """Every table except the event log and the idempotency records, plus every other event."""
    with _connect(state / "tessera.sqlite3") as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
            if row[0] not in {"events", "idempotency"}
        ]
        snapshot = {
            table: sorted(map(tuple, connection.execute(f"SELECT * FROM {table}")), key=repr)
            for table in tables
        }
        snapshot["events"] = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM events WHERE event_type != ? ORDER BY cursor",
                (DECISION_EVENT_TYPE,),
            )
        ]
    return snapshot


def _decision_events(state: Path) -> list[dict]:
    with _connect(state / "tessera.sqlite3") as connection:
        rows = connection.execute(
            "SELECT payload_json FROM events WHERE event_type=? ORDER BY cursor",
            (DECISION_EVENT_TYPE,),
        ).fetchall()
    return [json.loads(row["payload_json"]) for row in rows]


class Decisions(Harness):
    """The report harness's plain (non-campaign) operation, posting decisions."""

    def body(  # type: ignore[override]
        self,
        seq: int = 1,
        kind: str = "retry_request",
        reason: str = "transient_exit",
        at: float = 0,
        **overrides,
    ) -> dict:
        return _body(self.lease, self.issued, seq, kind, reason, at, **overrides)

    def post(self, body: dict, *, key: str = "k1", token: str = "worker-token", op=None):
        return _post(self.client, op or self.operation_id, body, key, token)

    def events(self) -> list[dict]:
        return _decision_events(self.state)

    def fill(self, count: int) -> None:  # type: ignore[override]
        """Record ``count`` decisions directly as events (seq 1.., observed_at advancing 60 s)."""
        with _connect(self.state / "tessera.sqlite3") as connection:
            operation = connection.execute(
                "SELECT * FROM external_operations WHERE operation_id=?", (self.operation_id,)
            ).fetchone()
            for seq in range(1, count + 1):
                at = self.issued + timedelta(seconds=60 * (seq - 1))
                ExecutionStore._event(
                    connection,
                    {"org_id": operation["org_id"], "id": operation["worker_principal_id"]},
                    operation["project_id"],
                    operation["task_id"],
                    DECISION_EVENT_TYPE,
                    self.lease["epoch"],
                    {
                        "grant_id": operation["grant_id"],
                        "lease_id": self.lease["id"],
                        "operation_id": self.operation_id,
                        "epoch": self.lease["epoch"],
                        "kind": "retry_request",
                        "reason": "transient_exit",
                        "decision_seq": seq,
                        "requested_memory_bytes": None,
                        "contract_digest": CONTRACT,
                        "observed_at": at.isoformat().replace("+00:00", "Z"),
                        "operational_only": True,
                        "observation_proof": "trusted_worker_assertion",
                        "admission": "not_admitted",
                        "bounds": {"within_envelope": False, "blocked_by": "no_campaign_stage"},
                    },
                )
            connection.commit()
        self.advance_clock(60 * count + 120)


@pytest.fixture
def watch(tmp_path: Path) -> Decisions:
    return Decisions(tmp_path)


class CampaignDecisions:
    """A running campaign whose stage attempt has a live worker operation."""

    def __init__(self, tmp_path: Path, **stage_extra) -> None:
        self.world = World(tmp_path)
        env = self.world.env
        spec = env.spec()
        spec["stages"] = [stage_spec(env, "s1", **stage_extra)]
        self.campaign = self.world.campaign(spec)
        self.attempt = self._run_until_operation()
        self.state = env.state
        operation = env.query(
            "SELECT * FROM external_operations WHERE operation_id=?", self.attempt["operation_id"]
        )[0]
        self.lease = dict(
            env.query("SELECT * FROM execution_leases WHERE id=?", operation["lease_id"])[0]
        )
        self.issued = _parse_utc(self.lease["issued_at"])
        self.client = TestClient(create_app(self.state))

    def _run_until_operation(self):
        for _ in range(60):
            self.world.runner.tick()
            (attempt,) = self.world.attempts(self.campaign["id"])
            if attempt["operation_id"]:
                return attempt
            self.world.poller.run(max_cycles=1)
        raise AssertionError("no operation")

    def post(self, kind="retry_request", reason="transient_exit", seq=1, key=None, **overrides):
        body = _body(self.lease, self.issued, seq, kind, reason, 0, **overrides)
        return _post(
            self.client, self.attempt["operation_id"], body, key or f"d{seq}", "worker-token"
        )

    def bounds(self) -> dict:
        return _decision_events(self.state)[-1]["bounds"]

    def section(self) -> dict:
        connection = _connect(self.state / "tessera.sqlite3")
        try:
            return watch_receipt_section(connection, self.campaign["id"])
        finally:
            connection.close()


RETRY_ALLOWED = {"retry": {"max_attempts": 2, "transient_exit_codes": [3]}}


# -- accepted requests ---------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["retry_request", "resize_request"])
def test_accepted_request_records_one_unadmitted_event_and_nothing_else(
    tmp_path: Path, kind: str
) -> None:
    run = CampaignDecisions(tmp_path, **RETRY_ALLOWED)
    before = _state_without_events(run.state)
    reason = "transient_exit" if kind == "retry_request" else "oom_killed"
    response = run.post(kind, reason)
    assert response.status_code == 201, response.text
    within = kind == "retry_request"
    assert response.json() == {
        "recorded": True,
        "admitted": False,
        "within_envelope": within,
        "kind": kind,
        "decision_seq": 1,
        "operation_id": run.attempt["operation_id"],
    }
    (event,) = _decision_events(run.state)
    bounds = event.pop("bounds")
    assert event == {
        "grant_id": run.attempt["grant_id"],
        "lease_id": run.lease["id"],
        "operation_id": run.attempt["operation_id"],
        "epoch": run.lease["epoch"],
        "kind": kind,
        "reason": reason,
        "decision_seq": 1,
        "requested_memory_bytes": None if within else 2 * GIB,
        "contract_digest": CONTRACT,
        "observed_at": run.issued.isoformat().replace("+00:00", "Z"),
        "operational_only": True,
        "observation_proof": "trusted_worker_assertion",
        "admission": "not_admitted",
    }
    assert bounds["within_envelope"] is within
    # No attempt, grant, hold, budget, custody or other event row changed.
    assert _state_without_events(run.state) == before


def test_retry_inside_the_approved_retry_policy(tmp_path: Path) -> None:
    run = CampaignDecisions(tmp_path, **RETRY_ALLOWED)
    assert run.post().json()["within_envelope"] is True
    assert run.bounds() == {
        "within_envelope": True,
        "blocked_by": None,
        "stage_key": "s1",
        "attempt": 1,
        "attempts_used": 1,
        "max_attempts": 2,
        "infra_retries": 0,
        "scientific_attempts_used": 1,
        "infra_retries_used": 0,
    }
    # A lost probe is never inside: the outcome is unknown.
    assert run.post(reason="probe_lost", seq=2).json()["within_envelope"] is False
    assert run.bounds()["blocked_by"] == "outcome_unknown"
    # A campaign that is no longer running has no retry to give.
    run.world.env.execute("UPDATE campaigns SET status='stopping'")
    assert run.post(seq=3).json()["within_envelope"] is False
    assert run.bounds()["blocked_by"] == "campaign_not_running"
    assert {e["admission"] for e in _decision_events(run.state)} == {"not_admitted"}


@pytest.mark.parametrize(
    ("retry", "blocked_by"),
    [
        ({}, "retry_policy_exhausted"),
        ({"max_attempts": 2}, "transient_exit_not_declared"),
        # Attempts are left only as infrastructure retries; the scientific bound is spent.
        (
            {"max_attempts": 1, "transient_exit_codes": [3], "infra_retries": 1},
            "scientific_retries_exhausted",
        ),
    ],
)
def test_retry_outside_the_approved_policy_is_recorded_not_within(
    tmp_path: Path, retry: dict, blocked_by: str
) -> None:
    run = CampaignDecisions(tmp_path, retry=retry)
    response = run.post()
    assert response.status_code == 201
    assert response.json()["admitted"] is False
    assert response.json()["within_envelope"] is False
    assert run.bounds()["blocked_by"] == blocked_by
    assert len(_decision_events(run.state)) == 1


def test_resize_is_never_within_while_the_spec_states_no_memory_cap(tmp_path: Path) -> None:
    run = CampaignDecisions(tmp_path, **RETRY_ALLOWED)
    for seq, reason in ((1, "oom_killed"), (2, "resource_limit")):
        response = run.post("resize_request", reason, seq=seq, requested_memory_bytes=1)
        assert response.status_code == 201
        assert response.json()["within_envelope"] is False
        assert run.bounds() == {
            "within_envelope": False,
            "blocked_by": "cap_unknown",
            "cap_unknown": True,
            "memory_cap_bytes": None,
        }


def test_operation_outside_any_campaign_is_recorded_not_within(watch: Decisions) -> None:
    before = _state_without_events(watch.state)
    response = watch.post(watch.body())
    assert response.status_code == 201
    assert response.json()["within_envelope"] is False
    assert watch.events()[0]["bounds"] == {
        "within_envelope": False,
        "blocked_by": "no_campaign_stage",
    }
    assert (
        watch.post(watch.body(seq=2, kind="resize_request", reason="oom_killed"), key="k2").json()[
            "within_envelope"
        ]
        is False
    )
    assert watch.events()[1]["bounds"]["cap_unknown"] is True
    assert _state_without_events(watch.state) == before


# -- closed schema -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b | {"extra": 1},
        lambda b: b | {"requested_memory_bytes": GIB},  # retry with bytes
        lambda b: b | {"kind": "resize_request", "reason": "oom_killed"},  # resize without
        lambda b: (
            b | {"kind": "resize_request", "reason": "oom_killed", "requested_memory_bytes": 0}
        ),
        lambda b: (
            b
            | {
                "kind": "resize_request",
                "reason": "oom_killed",
                "requested_memory_bytes": 2**40 + 1,
            }
        ),
        lambda b: (
            b | {"kind": "resize_request", "reason": "oom_killed", "requested_memory_bytes": "1"}
        ),
        lambda b: (
            b | {"kind": "resize_request", "reason": "oom_killed", "requested_memory_bytes": True}
        ),
        lambda b: b | {"kind": "reroute_request"},
        lambda b: b | {"reason": "slow"},
        lambda b: b | {"decision_seq": 1001},
        lambda b: b | {"decision_seq": -1},
        lambda b: b | {"decision_seq": "1"},
        lambda b: b | {"expected_epoch": -1},
        lambda b: b | {"contract_digest": "D" * 64},
        lambda b: b | {"observed_at": "2026-01-01T00:00:00"},
        lambda b: {k: v for k, v in b.items() if k != "contract_digest"},
    ],
)
def test_closed_schema_refuses_anything_else(watch: Decisions, mutate) -> None:
    assert watch.post(mutate(watch.body())).status_code == 422
    assert watch.events() == []


def test_bounds_of_the_schema_are_inclusive(watch: Decisions) -> None:
    # A null is the same as absent for a retry; decision_seq 0 and 1000 are both accepted.
    assert watch.post(watch.body(seq=0, requested_memory_bytes=None), key="k0").status_code == 201
    top = watch.body(
        seq=1000, kind="resize_request", reason="oom_killed", requested_memory_bytes=2**40
    )
    assert watch.post(top).status_code == 201
    assert [event["requested_memory_bytes"] for event in watch.events()] == [None, 2**40]


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("retry_request", "oom_killed"),
        ("retry_request", "resource_limit"),
        ("resize_request", "transient_exit"),
        ("resize_request", "probe_lost"),
    ],
)
def test_pairing_outside_the_closed_set_is_decision_not_allowed(
    watch: Decisions, kind: str, reason: str
) -> None:
    watch.refused(
        watch.body(kind=kind, reason=reason),
        "decision_not_allowed",
        f"A {kind} may not give the reason {reason}.",
    )
    assert watch.events() == []


# -- fences --------------------------------------------------------------------------------


def test_each_fence_names_its_reason(watch: Decisions) -> None:
    fx._worker(watch.state, watch.owner, watch.project["id"], "other-token")
    watch.refused(watch.body(), "unknown_operation", NOT_FOUND, op="0" * 32)
    watch.refused(watch.body(), "unknown_operation", NOT_FOUND, token="other-token")
    watch.refused(watch.body(lease_id="other-lease"), "lease_mismatch", MISMATCH, key="k2")
    watch.refused(
        watch.body(expected_epoch=watch.lease["epoch"] + 1),
        "stale_epoch",
        "Watch decision request epoch is no longer current.",
        key="k3",
    )
    watch.refused(
        watch.body(at=-60),
        "before_lease",
        "Watch decision timestamp is outside the accepted window.",
        key="k4",
    )
    watch.refused(
        watch.body(at=3600),
        "too_early",
        "observed_at is ahead of the control clock; retry",
        key="k5",
    )
    owner = (watch.state / "owner.header").read_text().strip().split(": ", 1)[1]
    response = watch.client.post(
        f"/worker/operations/{watch.operation_id}/watch-decisions",
        json=watch.body(),
        headers={"Authorization": owner, "Idempotency-Key": "k6"},
    )
    assert response.status_code in {401, 403} and "reason" not in response.json()
    assert watch.events() == []


def test_grant_refusals_name_their_reason(watch: Decisions) -> None:
    fx._worker(watch.state, watch.owner, watch.project["id"], "other-token")
    with _connect(watch.state / "tessera.sqlite3") as connection:
        connection.execute("UPDATE execution_grants SET worker_principal_id='worker-other-token'")
        connection.commit()
    watch.refused(watch.body(), "grant_mismatch", MISMATCH, key="g1")
    with _connect(watch.state / "tessera.sqlite3") as connection:
        connection.execute("UPDATE execution_grants SET worker_principal_id='worker-worker-token'")
        connection.execute("UPDATE execution_grants SET status='reconciled'")
        connection.commit()
    watch.refused(
        watch.body(),
        "grant_reconciled",
        "Execution authority has already been reconciled.",
        key="g2",
    )
    assert watch.events() == []


def test_worker_outside_the_grant_project_is_wrong_worker(watch: Decisions) -> None:
    with _connect(watch.state / "tessera.sqlite3") as connection:
        org_id = connection.execute("SELECT org_id FROM projects").fetchone()[0]
        connection.execute(
            "INSERT INTO projects (id, org_id, name, created_at) VALUES (?, ?, ?, ?)",
            ("other-project", org_id, "other", "2026-01-01T00:00:00Z"),
        )
        connection.execute(
            "UPDATE credentials SET project_id='other-project' "
            "WHERE principal_id='worker-worker-token'"
        )
        connection.commit()
    watch.refused(watch.body(), "wrong_worker", "Project was not found.")
    assert watch.events() == []


def test_fenced_operation_is_worker_fenced(watch: Decisions) -> None:
    with _connect(watch.state / "tessera.sqlite3") as connection:
        op = connection.execute(
            "SELECT * FROM external_operations WHERE operation_id=?", (watch.operation_id,)
        ).fetchone()
        connection.execute(
            """INSERT INTO execution_recovery_reconciliations
            (id, org_id, project_id, task_id, grant_id, lease_id, epoch, operation_id,
             original_worker_principal_id, identity_loss_reason, state, revision,
             created_at, updated_at)
            VALUES ('rec', ?, ?, ?, ?, ?, ?, ?, ?, 'worker_retired', 'identity_lost', 1,
             '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')""",
            (
                op["org_id"],
                op["project_id"],
                op["task_id"],
                op["grant_id"],
                op["lease_id"],
                op["epoch"],
                op["operation_id"],
                op["worker_principal_id"],
            ),
        )
        connection.commit()
    watch.refused(
        watch.body(),
        "worker_fenced",
        "Original worker identity is fenced pending independent recovery.",
    )


def test_too_early_is_transient_then_accepted(watch: Decisions) -> None:
    store = watch.app.state.stage_watch
    store._now = lambda: watch.issued + timedelta(seconds=10)
    late = watch.body(at=16)  # 6 s ahead, past the 5 s skew
    assert watch.post(late, key="late").status_code == 425
    assert watch.events() == []
    store._now = lambda: watch.issued + timedelta(seconds=12)
    accepted = watch.post(late, key="late")
    assert accepted.status_code == 201 and accepted.json()["recorded"] is True
    assert len(watch.events()) == 1


def test_out_of_order_and_replay(watch: Decisions) -> None:
    first = watch.post(watch.body(seq=5, at=2), key="a")
    assert first.status_code == 201
    # The exact body under the same key replays the stored response, with no second event.
    replay = watch.post(watch.body(seq=5, at=2), key="a")
    assert replay.status_code == 201 and replay.json() == first.json()
    # The same decision_seq under another key is out of order, as is a lower one or an
    # earlier observed_at.
    watch.refused(watch.body(seq=5, at=3), "out_of_order", ORDER, key="b")
    watch.refused(watch.body(seq=4, at=3), "out_of_order", ORDER, key="c")
    watch.refused(watch.body(seq=6, at=1), "out_of_order", ORDER, key="d")
    watch.refused(
        watch.body(seq=6, at=3),
        "key_conflict",
        "Idempotency key was already used with a different request.",
        key="a",
    )
    assert watch.post(watch.body(seq=6, at=2), key="e").status_code == 201
    assert [event["decision_seq"] for event in watch.events()] == [5, 6]
    # A replay of an accepted request still returns its stored 201 once control would refuse.
    with _connect(watch.state / "tessera.sqlite3") as connection:
        connection.execute("UPDATE execution_grants SET status='reconciled'")
        connection.commit()
    assert watch.post(watch.body(seq=5, at=2), key="a").json() == first.json()
    assert len(watch.events()) == 2


def test_decision_budget_is_sixteen(watch: Decisions) -> None:
    assert MAX_DECISIONS_PER_OPERATION == 16
    watch.fill(MAX_DECISIONS_PER_OPERATION - 1)
    at = 60 * MAX_DECISIONS_PER_OPERATION
    last = watch.body(seq=MAX_DECISIONS_PER_OPERATION, at=at)
    assert watch.post(last, key="last").status_code == 201
    watch.refused(
        watch.body(seq=MAX_DECISIONS_PER_OPERATION + 1, at=at),
        "decision_budget_exhausted",
        BUDGET,
        key="over",
    )
    assert len(watch.events()) == MAX_DECISIONS_PER_OPERATION


def test_decisions_and_reports_keep_separate_sequences(watch: Decisions) -> None:
    report = {
        "lease_id": watch.lease["id"],
        "expected_epoch": watch.lease["epoch"],
        "kind": "stuck",
        "observed_at": (watch.issued + timedelta(seconds=1)).isoformat(),
        "seq": 9,
        "counters": {},
        "kill_switch_engaged": False,
        "contract_digest": CONTRACT,
    }
    response = watch.client.post(
        f"/worker/operations/{watch.operation_id}/watch-reports",
        json=report,
        headers={"Authorization": "Bearer worker-token", "Idempotency-Key": "r"},
    )
    assert response.status_code == 201
    assert watch.post(watch.body(seq=1, at=0)).status_code == 201


def test_reason_table_includes_the_decision_reasons() -> None:
    assert set(REASON_STATUS) == PERMANENT_REASONS | TRANSIENT_REASONS
    assert not PERMANENT_REASONS & TRANSIENT_REASONS
    for reason in ("decision_budget_exhausted", "decision_not_allowed"):
        assert reason in PERMANENT_REASONS and REASON_STATUS[reason] == 409


# -- receipt -------------------------------------------------------------------------------


def test_receipt_lists_decisions_only_when_recorded(tmp_path: Path) -> None:
    run = CampaignDecisions(tmp_path, **RETRY_ALLOWED)
    assert run.section() == {}
    # A report alone keeps the entry's earlier shape: no ``decisions`` key.
    report = {
        "lease_id": run.lease["id"],
        "expected_epoch": run.lease["epoch"],
        "kind": "stuck",
        "observed_at": run.issued.isoformat(),
        "seq": 1,
        "counters": {"silent_s": 4},
        "kill_switch_engaged": False,
        "contract_digest": CONTRACT,
    }
    response = run.client.post(
        f"/worker/operations/{run.attempt['operation_id']}/watch-reports",
        json=report,
        headers={"Authorization": "Bearer worker-token", "Idempotency-Key": "r1"},
    )
    assert response.status_code == 201
    (entry,) = run.section()["stage_watch"]["attempts"]
    assert "decisions" not in entry
    assert run.post().status_code == 201
    assert run.post("resize_request", "oom_killed", seq=2).status_code == 201
    observed = run.issued.isoformat().replace("+00:00", "Z")
    (entry,) = run.section()["stage_watch"]["attempts"]
    assert entry["reports_recorded"] == 1
    assert entry["decisions"] == [
        {
            "kind": "retry_request",
            "reason": "transient_exit",
            "decision_seq": 1,
            "requested_memory_bytes": None,
            "within_envelope": True,
            "admission": "not_admitted",
            "observed_at": observed,
        },
        {
            "kind": "resize_request",
            "reason": "oom_killed",
            "decision_seq": 2,
            "requested_memory_bytes": 2 * GIB,
            "within_envelope": False,
            "admission": "not_admitted",
            "observed_at": observed,
        },
    ]


def test_receipt_entry_for_decisions_without_reports(tmp_path: Path) -> None:
    run = CampaignDecisions(tmp_path, **RETRY_ALLOWED)
    assert run.post().status_code == 201
    section = run.section()["stage_watch"]
    (entry,) = section["attempts"]
    assert entry["reports_recorded"] == 0
    assert entry["first_seen"] is None and entry["last_seen"] is None
    assert entry["counts_by_kind"] == {"heartbeat": 0, "progress": 0, "stuck": 0, "failed": 0}
    assert [item["decision_seq"] for item in entry["decisions"]] == [1]
    assert section["claims_unverified"] is True
