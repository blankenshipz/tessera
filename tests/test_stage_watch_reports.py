"""ADR 0087: worker stage watch reports are operational-only events, never evidence."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import test_execution as fx
from campaign_runner_harness import World, stage_spec
from fastapi.testclient import TestClient
from test_sandbox_egress_control import _package_of, _world

from tessera_control.api import create_app
from tessera_control.campaign_receipts import build_receipt
from tessera_control.execution import ExecutionStore
from tessera_control.stage_watch import EVENT_TYPE, _watch_events, watch_receipt_section
from tessera_control.stage_watch_limits import (
    MAX_REPORTS_PER_OPERATION,
    MIN_ROUTINE_INTERVAL_SECONDS,
    TERMINAL_RESERVE,
)
from tessera_control.store import _connect, _parse_utc, canonical_digest

CONTRACT = "e" * 64
UNTOUCHED_TABLES = (
    "execution_observations",
    "external_operations",
    "execution_grants",
    "execution_leases",
    "campaign_stage_attempts",
)


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.state, self.owner, self.worker, self.project, self.task, self.lease, op = _world(
            tmp_path, {}
        )
        self.operation_id = _package_of(tmp_path, op)["operation_id"]
        self.app = create_app(self.state)
        self.client = TestClient(self.app)
        self.issued = _parse_utc(self.lease["issued_at"])

    def body(self, seq: int = 1, kind: str = "progress", at: float = 0, **overrides) -> dict:
        return {
            "lease_id": self.lease["id"],
            "expected_epoch": self.lease["epoch"],
            "kind": kind,
            "observed_at": (self.issued + timedelta(seconds=at)).isoformat(),
            "seq": seq,
            "counters": {"elapsed_s": 12, "silent_s": 3},
            "kill_switch_engaged": False,
            "contract_digest": CONTRACT,
        } | overrides

    def post(self, body: dict, *, key: str = "k1", token: str = "worker-token", op=None):
        return self.client.post(
            f"/worker/operations/{op or self.operation_id}/watch-reports",
            json=body,
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
        )

    def events(self) -> list[dict]:
        with _connect(self.state / "tessera.sqlite3") as connection:
            rows = connection.execute(
                "SELECT payload_json FROM events WHERE event_type=? ORDER BY rowid", (EVENT_TYPE,)
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def snapshot(self) -> dict[str, list]:
        with _connect(self.state / "tessera.sqlite3") as connection:
            return {
                table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
                for table in UNTOUCHED_TABLES
            }

    def advance_clock(self, seconds: float) -> None:
        moment = datetime.now(UTC) + timedelta(seconds=seconds)
        self.app.state.stage_watch._now = lambda: moment

    def fill(self, count: int, kind: str = "progress", project_id: str | None = None) -> None:
        """Record ``count`` reports directly as events (seq 1.., observed_at advancing 60 s)."""
        with _connect(self.state / "tessera.sqlite3") as connection:
            operation = connection.execute(
                "SELECT * FROM external_operations WHERE operation_id=?", (self.operation_id,)
            ).fetchone()
            for seq in range(1, count + 1):
                at = self.issued + timedelta(seconds=60 * (seq - 1))
                ExecutionStore._event(
                    connection,
                    {"org_id": operation["org_id"], "id": operation["worker_principal_id"]},
                    project_id or operation["project_id"],
                    operation["task_id"] if project_id is None else None,
                    EVENT_TYPE,
                    self.lease["epoch"],
                    {
                        "grant_id": operation["grant_id"],
                        "lease_id": self.lease["id"],
                        "operation_id": self.operation_id,
                        "epoch": self.lease["epoch"],
                        "kind": kind,
                        "seq": seq,
                        "observed_at": at.isoformat().replace("+00:00", "Z"),
                        "counters": {},
                        "kill_switch_engaged": False,
                        "contract_digest": CONTRACT,
                        "operational_only": True,
                        "observation_proof": "trusted_worker_assertion",
                    },
                )
            connection.commit()
        # The next report may be dated after the last filled one.
        self.advance_clock(60 * count + 120)


@pytest.fixture
def watch(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def test_accepted_report_records_exactly_one_operational_event(watch: Harness) -> None:
    before = watch.snapshot()
    response = watch.post(watch.body(kind="stuck"))
    assert response.status_code == 201, response.text
    assert response.json() == {
        "recorded": True,
        "seq": 1,
        "kind": "stuck",
        "operation_id": watch.operation_id,
    }
    (event,) = watch.events()
    with _connect(watch.state / "tessera.sqlite3") as connection:
        grant_id = connection.execute(
            "SELECT grant_id FROM external_operations WHERE operation_id=?",
            (watch.operation_id,),
        ).fetchone()[0]
    assert event == {
        "grant_id": grant_id,
        "lease_id": watch.lease["id"],
        "operation_id": watch.operation_id,
        "epoch": watch.lease["epoch"],
        "kind": "stuck",
        "seq": 1,
        "observed_at": watch.issued.isoformat().replace("+00:00", "Z"),
        "counters": {"elapsed_s": 12, "silent_s": 3},
        "kill_switch_engaged": False,
        "contract_digest": CONTRACT,
        "operational_only": True,
        "observation_proof": "trusted_worker_assertion",
    }
    assert watch.snapshot() == before


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b | {"extra": 1},
        lambda b: b | {"counters": {"elapsed_s": 1, "note": 2}},
        lambda b: b | {"counters": {"elapsed_s": "12"}},
        lambda b: b | {"counters": {"mem_bytes": -1}},
        lambda b: b | {"counters": {"cpu_ms": 2**53 + 1}},
        lambda b: b | {"counters": {"restarts": 1.5}},
        lambda b: b | {"kind": "succeeded"},
        lambda b: b | {"seq": 1_000_001},
        lambda b: b | {"seq": "1"},
        lambda b: b | {"expected_epoch": -1},
        lambda b: b | {"kill_switch_engaged": "false"},
        lambda b: b | {"contract_digest": "E" * 64},
        lambda b: b | {"observed_at": "2026-01-01T00:00:00"},
        lambda b: {k: v for k, v in b.items() if k != "counters"},
    ],
)
def test_closed_schema_refuses_anything_else(watch: Harness, mutate) -> None:
    assert watch.post(mutate(watch.body())).status_code == 422
    assert watch.events() == []


def test_counter_upper_bound_is_inclusive(watch: Harness) -> None:
    body = watch.body(counters={"output_bytes": 2**53, "restarts": 0})
    assert watch.post(body).status_code == 201
    assert watch.events()[0]["counters"] == {"output_bytes": 2**53, "restarts": 0}


def test_wrong_worker_lease_epoch_and_resolved_grant_are_refused(watch: Harness) -> None:
    fx._worker(watch.state, watch.owner, watch.project["id"], "other-token")
    assert watch.post(watch.body(), token="other-token").status_code == 404
    assert watch.post(watch.body(), op="0" * 32).status_code == 404
    assert watch.post(watch.body(lease_id="other-lease"), key="k2").status_code == 409
    stale = watch.body(expected_epoch=watch.lease["epoch"] + 1)
    assert watch.post(stale, key="k3").status_code == 409
    early = watch.body(at=-60)
    assert watch.post(early, key="k4").status_code == 409
    future = watch.body(at=3600)
    assert watch.post(future, key="k5").status_code == 425
    owner = (watch.state / "owner.header").read_text().strip().split(": ", 1)[1]
    response = watch.client.post(
        f"/worker/operations/{watch.operation_id}/watch-reports",
        json=watch.body(),
        headers={"Authorization": owner, "Idempotency-Key": "k6"},
    )
    assert response.status_code in {401, 403}
    with _connect(watch.state / "tessera.sqlite3") as connection:
        connection.execute("UPDATE execution_grants SET status='reconciled'")
        connection.commit()
    assert watch.post(watch.body(), key="k7").status_code == 409
    assert watch.events() == []


def test_idempotent_replay_returns_stored_response_without_second_event(watch: Harness) -> None:
    first = watch.post(watch.body())
    replay = watch.post(watch.body())
    assert replay.status_code == 201 and replay.json() == first.json()
    assert len(watch.events()) == 1
    assert watch.post(watch.body(seq=2), key="k1").status_code == 409
    assert len(watch.events()) == 1


def test_out_of_order_seq_or_time_is_refused(watch: Harness) -> None:
    # ``stuck`` is never coalesced, so only the ordering rules decide here.
    assert watch.post(watch.body(kind="stuck", seq=5, at=2)).status_code == 201
    assert watch.post(watch.body(kind="stuck", seq=5, at=3), key="k2").status_code == 409
    assert watch.post(watch.body(kind="stuck", seq=4, at=3), key="k3").status_code == 409
    assert watch.post(watch.body(kind="stuck", seq=6, at=1), key="k4").status_code == 409
    assert watch.post(watch.body(kind="stuck", seq=6, at=2), key="k5").status_code == 201
    assert [event["seq"] for event in watch.events()] == [5, 6]


def test_heartbeats_coalesce_within_thirty_seconds(watch: Harness) -> None:
    watch.advance_clock(120)
    assert watch.post(watch.body(seq=1, kind="heartbeat", at=0)).status_code == 201
    fast = watch.post(watch.body(seq=2, kind="heartbeat", at=10), key="k2")
    assert fast.status_code == 200
    assert fast.json() == {
        "recorded": False,
        "reason": "coalesced",
        "seq": 2,
        "kind": "heartbeat",
        "operation_id": watch.operation_id,
    }
    # Other kinds are never coalesced.
    assert watch.post(watch.body(seq=3, kind="progress", at=11), key="k3").status_code == 201
    assert watch.post(watch.body(seq=4, kind="heartbeat", at=30), key="k4").status_code == 201
    assert [event["seq"] for event in watch.events()] == [1, 3, 4]


def test_heartbeats_cannot_outrun_the_control_clock(watch: Harness) -> None:
    assert watch.post(watch.body(seq=1, kind="heartbeat", at=0)).status_code == 201
    # A heartbeat dated 30 s ahead is in the future of the control clock: 425, nothing recorded.
    assert watch.post(watch.body(seq=2, kind="heartbeat", at=30), key="k2").status_code == 425
    assert len(watch.events()) == 1


def _idempotency_rows(watch: Harness, key: str) -> int:
    with _connect(watch.state / "tessera.sqlite3") as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM idempotency WHERE idempotency_key=?", (key,)
        ).fetchone()[0]


def test_report_ahead_of_the_control_clock_is_too_early_then_accepted(watch: Harness) -> None:
    store = watch.app.state.stage_watch
    store._now = lambda: watch.issued + timedelta(seconds=10)
    late = watch.body(seq=2, kind="failed", at=16)  # 6 s ahead, past the 5 s skew
    refused = watch.post(late, key="late")
    assert refused.status_code == 425
    assert refused.json() == {"detail": "observed_at is ahead of the control clock; retry"}
    assert watch.events() == []
    assert _idempotency_rows(watch, "late") == 0
    # 4 s ahead is inside the skew and accepted.
    assert watch.post(watch.body(seq=1, kind="progress", at=14), key="inside").status_code == 201
    # Before the lease was issued stays a permanent 409.
    assert watch.post(watch.body(seq=3, kind="failed", at=-1), key="past").status_code == 409
    # Once the control clock catches up, the identical body under the same key is accepted.
    store._now = lambda: watch.issued + timedelta(seconds=12)
    accepted = watch.post(late, key="late")
    assert accepted.status_code == 201 and accepted.json()["recorded"] is True
    assert watch.post(late, key="late").json() == accepted.json()
    assert [(event["seq"], event["kind"]) for event in watch.events()] == [
        (1, "progress"),
        (2, "failed"),
    ]


ROUTINE_BUDGET = MAX_REPORTS_PER_OPERATION - TERMINAL_RESERVE


def test_routine_reports_stop_at_the_routine_budget(watch: Harness) -> None:
    watch.fill(ROUTINE_BUDGET)
    at = 60 * ROUTINE_BUDGET
    for kind in ("heartbeat", "progress"):
        response = watch.post(watch.body(seq=ROUTINE_BUDGET + 1, kind=kind, at=at), key=kind)
        assert response.status_code == 409
        assert response.json()["detail"] == "Routine watch report budget exhausted."
    assert len(watch.events()) == ROUTINE_BUDGET


@pytest.mark.parametrize("kind", ["failed", "stuck"])
def test_terminal_kinds_use_the_reserve_after_routine_budget(watch: Harness, kind) -> None:
    watch.fill(ROUTINE_BUDGET)
    at = 60 * ROUTINE_BUDGET
    response = watch.post(watch.body(seq=ROUTINE_BUDGET + 1, kind=kind, at=at))
    assert response.status_code == 201, response.text
    assert watch.events()[-1]["kind"] == kind
    assert len(watch.events()) == ROUTINE_BUDGET + 1


def test_terminal_kinds_refused_at_the_total_cap(watch: Harness) -> None:
    watch.fill(MAX_REPORTS_PER_OPERATION - 1)
    seq, at = MAX_REPORTS_PER_OPERATION, 60 * MAX_REPORTS_PER_OPERATION
    # Routine is already past its budget; the last reserved slot still takes a ``stuck``.
    assert watch.post(watch.body(seq=seq, kind="progress", at=at), key="p").status_code == 409
    assert watch.post(watch.body(seq=seq, kind="stuck", at=at), key="s").status_code == 201
    response = watch.post(watch.body(seq=seq + 1, kind="failed", at=at), key="f")
    assert response.status_code == 409
    assert response.json()["detail"] == "Stage watch report budget exhausted."
    assert len(watch.events()) == MAX_REPORTS_PER_OPERATION


def test_progress_coalesces_per_kind_and_terminal_kinds_never_do(watch: Harness) -> None:
    watch.advance_clock(120)
    interval = MIN_ROUTINE_INTERVAL_SECONDS
    assert watch.post(watch.body(seq=1, kind="progress", at=0)).status_code == 201
    fast = watch.post(watch.body(seq=2, kind="progress", at=interval - 1), key="k2")
    assert fast.status_code == 200
    assert fast.json() == {
        "recorded": False,
        "reason": "coalesced",
        "seq": 2,
        "kind": "progress",
        "operation_id": watch.operation_id,
    }
    # Each routine kind has its own interval: a heartbeat is not paced by a progress.
    assert (
        watch.post(watch.body(seq=3, kind="heartbeat", at=interval - 1), key="k3").status_code
        == 201
    )
    for seq, kind in ((4, "stuck"), (5, "stuck"), (6, "failed")):
        response = watch.post(watch.body(seq=seq, kind=kind, at=interval - 1), key=f"k{seq}")
        assert response.status_code == 201
    assert watch.post(watch.body(seq=7, kind="progress", at=interval), key="k7").status_code == 201
    assert [event["seq"] for event in watch.events()] == [1, 3, 4, 5, 6, 7]


def test_worker_gap_counters_are_accepted_and_stored(watch: Harness) -> None:
    counters = {"elapsed_s": 5, "refused_reports": 2, "dropped_reports": 2**53}
    assert watch.post(watch.body(counters=counters)).status_code == 201
    assert watch.events()[0]["counters"] == counters
    for key, value in (("refused_reports", -1), ("dropped_reports", "1"), ("gaps", 1)):
        bad = watch.body(seq=2, at=60, counters={key: value})
        assert watch.post(bad, key=f"bad-{key}").status_code == 422


def _other_project(connection, org_id: str) -> str:
    connection.execute(
        "INSERT INTO projects (id, org_id, name, created_at) VALUES (?, ?, ?, ?)",
        ("other-project", org_id, "other", "2026-01-01T00:00:00Z"),
    )
    return "other-project"


def test_watch_lookup_reads_only_the_operations_project(watch: Harness) -> None:
    with _connect(watch.state / "tessera.sqlite3") as connection:
        org_id = connection.execute(
            "SELECT org_id FROM external_operations WHERE operation_id=?", (watch.operation_id,)
        ).fetchone()[0]
        other = _other_project(connection, org_id)
        connection.commit()
        plan = " ".join(
            str(tuple(row))
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT payload_json FROM events WHERE project_id = ? "
                "AND event_type = ? AND json_extract(payload_json, '$.operation_id') = ? "
                "ORDER BY cursor",
                (other, EVENT_TYPE, watch.operation_id),
            )
        )
        assert "events_by_project_cursor" in plan
    # Full-budget look-alikes under another project neither count nor order this operation.
    watch.fill(MAX_REPORTS_PER_OPERATION, kind="failed", project_id=other)
    response = watch.post(watch.body(seq=1, kind="progress", at=0))
    assert response.status_code == 201, response.text
    with _connect(watch.state / "tessera.sqlite3") as connection:
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        own = _watch_events(connection, watch.project["id"], watch.operation_id)
        connection.set_trace_callback(None)
        assert [event["seq"] for event in own] == [1]
        assert _watch_events(connection, other, watch.operation_id)[-1]["kind"] == "failed"
    assert any("FROM events" in sql and "project_id = " in sql for sql in statements)


def test_reports_never_change_execution_or_attempt_rows(watch: Harness) -> None:
    before = watch.snapshot()
    for seq, kind in enumerate(("heartbeat", "progress", "stuck", "failed"), start=1):
        response = watch.post(watch.body(seq=seq, kind=kind, kill_switch_engaged=True), key=kind)
        assert response.status_code == 201
    assert watch.snapshot() == before
    assert len(watch.events()) == 4


def _watch_event(
    connection, attempt, kind: str, seq: int, at: str, project_id: str | None = None, **extra
) -> None:
    grant = connection.execute(
        "SELECT * FROM execution_grants WHERE id=?", (attempt["grant_id"],)
    ).fetchone()
    project = connection.execute(
        "SELECT org_id FROM projects WHERE id=?", (grant["project_id"],)
    ).fetchone()
    payload = {
        "grant_id": attempt["grant_id"],
        "lease_id": "lease",
        "operation_id": attempt["operation_id"],
        "epoch": 1,
        "kind": kind,
        "seq": seq,
        "observed_at": at,
        "counters": {"silent_s": seq},
        "kill_switch_engaged": False,
        "contract_digest": CONTRACT,
        "operational_only": True,
        "observation_proof": "trusted_worker_assertion",
    } | extra
    ExecutionStore._event(
        connection,
        {"org_id": project["org_id"], "id": grant["worker_principal_id"]},
        project_id or grant["project_id"],
        grant["task_id"] if project_id is None else None,
        EVENT_TYPE,
        1,
        payload,
    )


def test_receipt_section_absent_without_reports_and_counted_with_them(tmp_path: Path) -> None:
    world = World(tmp_path)
    env = world.env
    spec = env.spec()
    spec["stages"] = [stage_spec(env, "s1")]
    campaign = world.campaign(spec)
    world.tick_all(campaign["id"])
    assert world.status(campaign["id"]) == "succeeded"
    (attempt,) = world.attempts(campaign["id"])
    assert attempt["operation_id"]
    stored = world.row("SELECT * FROM campaign_receipts WHERE campaign_id=?", campaign["id"])
    connection = _connect(env.state / "tessera.sqlite3")
    try:
        row = connection.execute("SELECT * FROM campaigns WHERE id=?", (campaign["id"],)).fetchone()
        receipt, digest = build_receipt(connection, row, "succeeded")
        assert "stage_watch" not in receipt
        assert digest == stored["receipt_digest"]
        assert watch_receipt_section(connection, campaign["id"]) == {}
        _watch_event(connection, attempt, "heartbeat", 1, "2026-01-01T00:00:00Z")
        _watch_event(connection, attempt, "stuck", 2, "2026-01-01T00:01:00Z")
        _watch_event(
            connection, attempt, "stuck", 3, "2026-01-01T00:02:00Z", kill_switch_engaged=True
        )
        _watch_event(connection, attempt, "progress", 4, "2026-01-01T00:03:00Z")
        # A report naming the operation under another grant is not joined.
        _watch_event(connection, attempt, "failed", 5, "2026-01-01T00:04:00Z", grant_id="x")
        # Nor is one written under another project, even with the same operation and grant.
        other = _other_project(connection, row["org_id"])
        _watch_event(connection, attempt, "failed", 6, "2026-01-01T00:05:00Z", project_id=other)
        connection.commit()
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        watch_receipt_section(connection, campaign["id"])
        connection.set_trace_callback(None)
        (query,) = [sql for sql in statements if "campaign_stage_attempts" in sql]
        plan = " ".join(
            str(tuple(item)) for item in connection.execute("EXPLAIN QUERY PLAN " + query)
        )
        assert "events_by_project_cursor" in plan
        receipt, digest = build_receipt(connection, row, "succeeded")
        assert digest != stored["receipt_digest"] and digest == canonical_digest(receipt)
        assert receipt["stage_watch"] == {
            "attempts": [
                {
                    "stage_key": "s1",
                    "attempt": attempt["attempt"],
                    "operation_id": attempt["operation_id"],
                    "counts_by_kind": {"heartbeat": 1, "progress": 1, "stuck": 2, "failed": 0},
                    "first_seen": "2026-01-01T00:00:00Z",
                    "last_seen": "2026-01-01T00:03:00Z",
                    "last_stuck_counters": {"silent_s": 3},
                    "last_failed_counters": None,
                    "last_counters_seen": {"silent_s": 4},
                    "kill_switch_engaged_seen": True,
                    "reports_recorded": 4,
                }
            ],
            "proof": "trusted_worker_assertion",
            "claims_unverified": True,
        }
    finally:
        connection.close()
