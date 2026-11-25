"""Measured cost of a long run's end-of-run hand-off on a control database with history.

Long runs were lost at the output hand-off while control's request log showed multi-second
gaps; short runs handed off fine. This drives the real development worker against the real
control app (as ``test_development_worker_service`` does), seeds a long attempt (observations
and lease renewals) into a database that also holds substantial unrelated history, and meters
every control request the hand-off makes:

* SQLite virtual-machine steps, counted with ``set_progress_handler`` (deterministic);
* the query plan of every statement the request ran (``EXPLAIN QUERY PLAN``), so a full scan
  of a large table is visible whatever the host's speed;
* wall time, printed for the report and never asserted.

Run with ``-s`` to see the table.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_development_worker_service import ApiChannel, _InspectingAdapter, _service
from test_execution import _grant_payload, _setup

from tessera_control.artifacts import ArtifactStore
from tessera_control.store import Store, _connect, canonical_digest

OUTPUT_BYTES = 8 * 1024 * 1024
STEP = 100  # progress-handler granularity (VM instructions per tick)
SMALL = {"events": 2_000, "observations": 500, "chunk_uploads": 5}
LARGE = {"events": 200_000, "observations": 50_000, "chunk_uploads": 300}
SHORT = {"observations": 500, "renewals": 50}  # about 8 minutes
LONG = {"observations": 3_000, "renewals": 300}  # about 50 minutes
# The tables a hand-off must never read in full: they grow with the installation's history.
GROWING = ("events", "execution_observations", "snapshot_chunk_uploads")


class Meter:
    """Count VM steps and capture statements per control request (one request at a time)."""

    def __init__(self) -> None:
        self.request: str | None = None
        self.sql: str | None = None
        self.steps: dict[str, list[int]] = defaultdict(list)
        self.walls: dict[str, list[float]] = defaultdict(list)
        self.statement_steps: dict[tuple[str, str], int] = defaultdict(int)
        self.samples: dict[tuple[str, str], str] = {}

    def wrap(self, real):
        def connect(*args, **kwargs):
            connection = real(*args, **kwargs)
            connection.set_progress_handler(self._tick, STEP)
            connection.set_trace_callback(self._trace)
            return connection

        return connect

    def _tick(self) -> int:
        if self.request is not None:
            self.steps[self.request][-1] += STEP
            self.statement_steps[(self.request, self.sql or "")] += STEP
        return 0

    def _trace(self, sql: str) -> None:
        if self.request is None:
            return
        shape = _shape(sql)
        self.sql = shape
        if shape and (self.request, shape) not in self.samples:
            self.samples[(self.request, shape)] = sql

    def begin(self, label: str) -> float:
        self.request = label
        self.sql = None
        self.steps[label].append(0)
        return time.perf_counter()

    def end(self, label: str, started: float) -> None:
        self.walls[label].append(time.perf_counter() - started)
        self.request = None


def _shape(sql: str) -> str:
    """A statement without its literals, or '' for transaction control and pragmas."""
    text = " ".join(sql.split())
    if not re.match(r"(?i)(SELECT|INSERT|UPDATE|DELETE|WITH)\b", text):
        return ""
    text = re.sub(r"(?i)X'[0-9a-f]*'", "?", text)
    text = re.sub(r"'(?:[^']|'')*'", "?", text)
    return re.sub(r"\b\d+(?:\.\d+)?\b", "?", text)[:400]


def _label(path: str, body: dict) -> str:
    label = re.sub(r"/(?=[A-Za-z0-9_-]*[0-9a-f]{12})[A-Za-z0-9_-]+", "/{id}", path)
    label = re.sub(r"/chunks/\d+$", "/chunks/{seq}", label)
    if label == "/worker/observations":
        state = body.get("observed_state")
        label += f"[{state}{',confirmed' if body.get('termination_confirmed') else ''}]"
    return label


class MeteredChannel(ApiChannel):
    def __init__(self, state, meter: Meter, on_reply=None):
        super().__init__(state)
        self.meter = meter
        self.on_reply = on_reply

    def post(self, path, body, key):
        label = _label(path, body)
        started = self.meter.begin(label)
        try:
            return super().post(path, body, key)
        finally:
            self.meter.end(label, started)
            if self.on_reply is not None:
                self.on_reply(path, body)


def _plan_with_room(tmp_path: Path):
    """The execution fixture with an approved plan that allows one ~8 MB output."""
    state, execution, owner, worker, project, task = _setup(tmp_path)
    with _connect(execution.database_path) as connection:
        snapshot_id, digest = connection.execute(
            """SELECT r.snapshot_id, s.digest FROM snapshot_releases r
            JOIN source_snapshots s ON s.id = r.snapshot_id"""
        ).fetchone()
    revision = {
        "expected_revision": task["revision"],
        "title": task["title"],
        "purpose": task["purpose"],
        "budget_cents": task["budget_cents"],
        "max_duration_seconds": task["max_duration_seconds"],
        "execution_spec": {
            "argv": ["python", "-c", "print('pilot')"],
            "input_snapshot_digest": digest,
            "pinned_skill_digests": [],
            "output_paths": ["result.txt"],
            "profile": "local-cpu",
            "network_enabled": False,
            "max_output_bytes": OUTPUT_BYTES + 1024 * 1024,
        },
    }
    _, revised = Store(state).revise_task_plan(
        owner, task["id"], "room-revise", canonical_digest(revision), revision
    )
    _, approved = Store(state).approve_task(
        owner,
        task["id"],
        "room-approve",
        canonical_digest({"expected_revision": revised["revision"]}),
        revised["revision"],
        revised["current_plan_digest"],
    )
    payload = _grant_payload(approved, worker, {"id": snapshot_id})
    _, grant = execution.issue_execution_grant(
        owner, project["id"], "room-grant", canonical_digest(payload), payload
    )
    return state, owner, worker, grant


def _hex(n: int = 32) -> str:
    return uuid.uuid4().hex[: min(n, 32)] + (uuid.uuid4().hex[: n - 32] if n > 32 else "")


def _seed_history(state: Path, grant: dict, size: dict[str, int]) -> None:
    """Unrelated history: other runs' events and observations, open chunk uploads."""
    connection = sqlite3.connect(state / "tessera.sqlite3")
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        org_id, actor = connection.execute(
            "SELECT org_id, actor_principal_id FROM events ORDER BY cursor DESC LIMIT 1"
        ).fetchone()
        now = datetime.now(UTC)
        stamp = (now - timedelta(days=3)).isoformat().replace("+00:00", "Z")
        kinds = (
            "execution.observation_reported",
            "worker.artifact_upload_progressed",
            "execution.lease_renewed",
            "execution.lease_acquired",
            "worker.artifact_quarantined",
            "execution.egress_journal_recorded",
            "execution.grant_cancel_requested",
        )
        weights = (30, 30, 25, 5, 5, 4, 1)
        pattern = [kind for kind, weight in zip(kinds, weights, strict=True) for _ in range(weight)]
        rows = []
        for n in range(size["events"]):
            operation, lease, other = _hex(), "lease_" + _hex(), "grant_" + _hex()
            payload = {
                "grant_id": other,
                "lease_id": lease,
                "operation_id": operation,
                "epoch": 1,
                "observed_state": "running",
                "desired_state": "admitted",
                "termination_confirmed": False,
                "observation_proof": "trusted_worker_assertion",
                "operational_only": True,
                "cancellation": {"state": "not_requested"},
                "detail": "x" * 200,
            }
            project = grant["project_id"] if n % 10 < 7 else "project_history"
            rows.append(
                (
                    org_id,
                    project,
                    None,
                    pattern[n % len(pattern)],
                    1,
                    actor,
                    stamp,
                    json.dumps(payload, separators=(",", ":"), sort_keys=True),
                )
            )
        connection.executemany(
            """INSERT INTO events (org_id, project_id, task_id, event_type, resource_revision,
            actor_principal_id, created_at, payload_json) VALUES (?,?,?,?,?,?,?,?)""",
            rows,
        )
        observations = []
        for n in range(size["observations"]):
            lease_n = n // 500
            observations.append(
                (
                    "observation_" + _hex(),
                    f"grant_history_{lease_n}",
                    f"lease_history_{lease_n}",
                    actor,
                    1,
                    f"{lease_n:032x}",
                    "admitted",
                    "running",
                    stamp,
                    "container",
                    0,
                    stamp,
                )
            )
        connection.executemany(
            """INSERT INTO execution_observations (id, grant_id, lease_id, worker_principal_id,
            epoch, operation_id, desired_state, observed_state, observed_at, provider_id,
            termination_confirmed, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            observations,
        )
        expires = (now + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        connection.executemany(
            """INSERT INTO snapshot_chunk_uploads (id, project_id, expected_digest,
            manifest_json, status, actor_principal_id, created_at, expires_at)
            VALUES (?,?,?,?, 'uploading', ?,?,?)""",
            [
                (
                    "chunk_upload_" + _hex(),
                    grant["project_id"],
                    _hex(64),
                    json.dumps(
                        {
                            "kind": "artifact",
                            "lease_id": f"lease_history_{n}",
                            "size": 1 << 20,
                            "files": [{"path": f"f{i}", "size": 1} for i in range(20)],
                        }
                    ),
                    actor,
                    stamp,
                    expires,
                )
                for n in range(size["chunk_uploads"])
            ],
        )
        connection.commit()
    finally:
        connection.close()


def _seed_run(state: Path, grant_id: str, size: dict[str, int]) -> None:
    """The attempt's own long history, cloned from its first real observation and renewal."""
    connection = sqlite3.connect(state / "tessera.sqlite3")
    try:
        connection.row_factory = sqlite3.Row
        first = connection.execute(
            "SELECT * FROM execution_observations WHERE grant_id=? ORDER BY rowid LIMIT 1",
            (grant_id,),
        ).fetchone()
        assert first is not None, "the worker reported no observation before the first sleep"
        columns = first.keys()
        rows = []
        for _ in range(size["observations"]):
            row = dict(first)
            row["id"] = "observation_" + _hex()
            rows.append(tuple(row[c] for c in columns))
        connection.executemany(
            f"INSERT INTO execution_observations ({','.join(columns)}) "
            f"VALUES ({','.join('?' * len(columns))})",
            rows,
        )
        renewal = connection.execute(
            """SELECT org_id, project_id, task_id, resource_revision, actor_principal_id,
            created_at, payload_json FROM events WHERE event_type IN
            ('execution.lease_renewed','execution.lease_acquired')
            AND instr(payload_json, ?) > 0 ORDER BY cursor DESC LIMIT 1""",
            (first["lease_id"],),
        ).fetchone()
        assert renewal is not None
        events = []
        for n in range(size["renewals"]):
            events.append(
                (
                    renewal["org_id"],
                    renewal["project_id"],
                    renewal["task_id"],
                    "execution.lease_renewed",
                    renewal["resource_revision"],
                    renewal["actor_principal_id"],
                    renewal["created_at"],
                    renewal["payload_json"],
                )
            )
            # Other projects keep working while this run is in flight.
            for _ in range(3):
                events.append(
                    (
                        renewal["org_id"],
                        "project_history",
                        None,
                        "execution.observation_reported",
                        1,
                        renewal["actor_principal_id"],
                        renewal["created_at"],
                        json.dumps({"operation_id": _hex(), "detail": "y" * 200, "n": n}),
                    )
                )
        connection.executemany(
            """INSERT INTO events (org_id, project_id, task_id, event_type, resource_revision,
            actor_principal_id, created_at, payload_json) VALUES (?,?,?,?,?,?,?,?)""",
            events,
        )
        connection.commit()
    finally:
        connection.close()


class _BigOutputAdapter(_InspectingAdapter):
    def start(self, operation):
        result = super().start(operation)
        content = hashlib.sha256(b"seed").digest() * (OUTPUT_BYTES // 32)
        Path(operation.development_output.directory, "result.txt").write_bytes(content)
        return result


def _scan_plans(state: Path, meter: Meter, connect) -> dict[tuple[str, str], list[str]]:
    """Every metered statement whose plan reads a growing table in full."""
    found: dict[tuple[str, str], list[str]] = {}
    connection = connect(state / "tessera.sqlite3")
    try:
        for (label, shape), sql in meter.samples.items():
            try:
                plan = connection.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
            except sqlite3.Error:
                continue
            scans = [
                row[3] for row in plan if re.match(r"SCAN ({})\b".format("|".join(GROWING)), row[3])
            ]
            if scans:
                found[(label, shape)] = scans
    finally:
        connection.close()
    return found


def _measure(tmp_path: Path, monkeypatch, history: dict, run: dict, *, late: bool = False):
    state, owner, worker, grant = _plan_with_room(tmp_path)
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute("DELETE FROM execution_leases")
    _seed_history(state, grant, history)
    meter = Meter()
    real_connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", meter.wrap(real_connect))

    def after_reply(path, body):
        if late and path == "/worker/observations" and body.get("termination_confirmed"):
            # The lease runs out right after the confirmed exit: every output request is
            # judged by the late path (``_exited_inside_grant``).
            with closing_db(state, real_connect) as connection:
                (expires,) = connection.execute(
                    "SELECT expires_at FROM execution_leases WHERE grant_id=?", (grant["id"],)
                ).fetchone()
            lapsed = datetime.fromisoformat(expires.replace("Z", "+00:00")) + timedelta(seconds=1)
            monkeypatch.setattr(ArtifactStore, "_now", staticmethod(lambda: lapsed))

    adapter = _BigOutputAdapter()
    channel = MeteredChannel(state, meter, after_reply)
    seeded = []

    def sleep(_):
        if not seeded:
            _seed_run(state, grant["id"], run)
            seeded.append(True)
            adapter.state = "exited"

    service = _service(
        tmp_path, state, grant, channel=channel, adapter=adapter, max_ticks=5, sleep=sleep
    )
    result = service.run()
    return state, result, meter, _scan_plans(state, meter, real_connect)


class closing_db:  # noqa: N801 - a tiny context manager
    def __init__(self, state, connect):
        self.connection = connect(state / "tessera.sqlite3")

    def __enter__(self):
        return self.connection

    def __exit__(self, *_):
        self.connection.close()


HANDOFF = re.compile(
    r"observations\[exited|artifact-uploads|project-artifacts|artifact-manifest|run-receipt"
    r"|egress-journal|image-build|public-fetch"
)
# Every worker request of the run, mid-run ones included (a running observation, a renewal).
WORKER = re.compile(r"^/worker/")


def _summary(meter: Meter) -> dict[str, tuple[int, int, float]]:
    """label -> (requests, max VM steps of one request, max wall ms of one request)."""
    return {
        label: (len(steps), max(steps), 1000 * max(meter.walls[label]))
        for label, steps in meter.steps.items()
    }


def _print_table(results: dict[str, dict[str, tuple[int, int, float]]]) -> None:
    labels = sorted({label for summary in results.values() for label in summary})
    names = list(results)
    print("\nper-request cost: max VM steps / max wall ms (n requests)")
    print("request".ljust(64) + "".join(name.rjust(30) for name in names))
    for label in labels:
        cells = []
        for name in names:
            value = results[name].get(label)
            cells.append(
                "-".rjust(30)
                if value is None
                else f"{value[1]:>12,} / {value[2]:>7.1f}ms ({value[0]})".rjust(30)
            )
        print(label[:63].ljust(64) + "".join(cells))


def _top_statements(meter: Meter, limit: int = 8) -> list[tuple[int, str, str]]:
    ranked = sorted(
        ((steps, label, shape) for (label, shape), steps in meter.statement_steps.items()),
        reverse=True,
    )
    return ranked[:limit]


@pytest.mark.parametrize("late", [False, True], ids=["inside-lease", "lease-lapsed"])
def test_handoff_cost_does_not_grow_with_history_or_run_length(tmp_path, monkeypatch, late):
    cases = {
        "small/short": (SMALL, SHORT),
        "small/long": (SMALL, LONG),
        "large/short": (LARGE, SHORT),
        "large/long": (LARGE, LONG),
    }
    results, scans, meters = {}, {}, {}
    for name, (history, run) in cases.items():
        directory = tmp_path / name.replace("/", "-")
        directory.mkdir()
        with monkeypatch.context() as patch:
            _, result, meter, found = _measure(directory, patch, history, run, late=late)
        assert result["state"] == "exited" and result["quarantined"] is True, result
        if late:
            # The late path is exercised: the lease had lapsed when the outputs arrived.
            assert meter.steps
        results[name], scans[name], meters[name] = _summary(meter), found, meter
    _print_table(results)
    print("\nheaviest statements, large/long:")
    for steps, label, shape in _top_statements(meters["large/long"]):
        print(f"{steps:>12,}  {label}  {shape[:160]}")
    run_scans = {key: value for key, value in scans["large/long"].items() if WORKER.search(key[0])}
    for (label, shape), plan in sorted(run_scans.items()):
        print(f"FULL SCAN  {label}: {plan}  {shape[:160]}")
    assert not run_scans, "a worker request reads a growing table in full"
    # Counted work, not wall time: a hand-off request on a 50 minute run inside a large
    # history costs about what it costs on a short run in a small one.
    small, large = results["small/short"], results["large/long"]
    for label, (_, steps, _) in large.items():
        if WORKER.search(label) and label in small:
            assert steps <= 4 * small[label][1] + 200_000, (label, steps, small[label][1])


def _history_db(tmp_path: Path) -> tuple[Path, dict]:
    state, _, _, worker, project, _ = _setup(tmp_path)
    grant = {"project_id": project["id"]}
    _seed_history(state, grant, {"events": 5_000, "observations": 5_000, "chunk_uploads": 50})
    return state, worker


def _plans_of(connection: sqlite3.Connection, call) -> list[tuple[str, list[str]]]:
    """Run ``call`` with a statement trace and return each statement's query plan."""
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    try:
        call()
    finally:
        connection.set_trace_callback(None)
    plans = []
    for sql in statements:
        if _shape(sql):
            detail = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql)]
            plans.append((_shape(sql), detail))
    return plans


def test_every_bounded_lookup_is_indexed_not_a_full_scan(tmp_path):
    """The lookups ADR 0190 bounds, called through the product code, each use an index."""
    from tessera_control import egress_journal, execution_run_receipts, external_operations
    from tessera_control.execution import ExecutionStore
    from tessera_control.image_build_report import image_build_for_operation
    from tessera_control.public_fetch_report import public_fetch_for_operation

    state, worker = _history_db(tmp_path)
    connection = _connect(state / "tessera.sqlite3")
    try:
        lease = {"id": "lease_history_3", "epoch": 1, "expires_at": "2026-01-01T00:00:00Z"}
        grant = {"expires_at": "2026-01-01T00:00:00Z"}
        now = datetime.now(UTC)
        operation = f"{3:032x}"
        execution = ExecutionStore(state)
        calls = {
            "cancellation": lambda: execution._cancellation_payload(
                connection, {"id": "grant_history_3", "status": "active", "updated_at": None}
            ),
            "egress journal": lambda: egress_journal.egress_journal_for_operation(
                connection, operation
            ),
            "image build": lambda: image_build_for_operation(connection, operation),
            "public fetch": lambda: public_fetch_for_operation(connection, operation),
            "confirmed exit code": lambda: external_operations.confirmed_exit_code(
                connection, operation
            ),
            "lease trust fields": lambda: execution_run_receipts._lease_trust_fields(
                connection, worker["org_id"], lease["id"]
            ),
            "receipt trust fields": lambda: execution_run_receipts._trust_fields(
                connection, {"org_id": worker["org_id"], "operation_id": operation}
            ),
            "exit inside grant": lambda: ArtifactStore._exited_inside_grant(
                connection, grant, lease, now, now
            ),
            "reserved bytes": lambda: ArtifactStore._reserved_bytes(connection, lease["id"]),
        }
        scans = {}
        for name, call in calls.items():
            for shape, detail in _plans_of(connection, call):
                full = [d for d in detail if re.match(r"SCAN ({})\b".format("|".join(GROWING)), d)]
                if full:
                    scans[(name, shape)] = full
        receipt_exit = connection.execute(
            """EXPLAIN QUERY PLAN SELECT min(recorded_at) FROM execution_observations
            WHERE lease_id=? AND epoch=? AND observed_state='exited'
            AND termination_confirmed=1""",
            (lease["id"], 1),
        ).fetchall()
        assert any("execution_observations_confirmed_exit" in row[3] for row in receipt_exit)
        for name in ("latest", "provider"):
            sql = (
                "SELECT observed_at FROM execution_observations WHERE lease_id = ? "
                "ORDER BY rowid DESC LIMIT 1"
                if name == "latest"
                else "SELECT provider_id FROM execution_observations WHERE lease_id = ? "
                "AND provider_id IS NOT NULL ORDER BY rowid DESC LIMIT 1"
            )
            plan = connection.execute("EXPLAIN QUERY PLAN " + sql, (lease["id"],)).fetchall()
            assert any("execution_observations_by_lease" in row[3] for row in plan), plan
    finally:
        connection.close()
    assert not scans, scans


def _old_exited_inside_grant(connection, grant, lease, now, registered_at=None):
    """The pre-ADR 0190 reduction over every observation of the lease, kept as the oracle."""
    from tessera_control.artifacts import (
        MAX_EXTENDED_HOLD_SECONDS,
        OUTPUT_REGISTRATION_GRACE_SECONDS,
        _parse_utc,
    )

    rows = connection.execute(
        """SELECT observed_state, termination_confirmed, recorded_at FROM execution_observations
        WHERE lease_id=? AND epoch=? ORDER BY rowid""",
        (lease["id"], lease["epoch"]),
    ).fetchall()
    if not rows or rows[-1]["observed_state"] != "exited" or not rows[-1]["termination_confirmed"]:
        return False
    exited = min(
        _parse_utc(row["recorded_at"])
        for row in rows
        if row["observed_state"] == "exited" and row["termination_confirmed"]
    )
    within = min(_parse_utc(grant["expires_at"]), _parse_utc(lease["expires_at"]))
    if registered_at is not None:
        registered = _parse_utc(registered_at) if isinstance(registered_at, str) else registered_at
        if registered > exited + timedelta(seconds=OUTPUT_REGISTRATION_GRACE_SECONDS):
            return False
    return exited <= within and now < exited + timedelta(seconds=MAX_EXTENDED_HOLD_SECONDS)


def test_bounded_exit_inside_grant_matches_the_full_reduction(tmp_path):
    """Old (every row) and new (latest row plus confirmed exits) agree on seeded histories."""
    import random

    state, _, _, _, _, _ = _setup(tmp_path)
    connection = _connect(state / "tessera.sqlite3")
    connection.execute("PRAGMA foreign_keys=OFF")
    rng = random.Random(20261125)
    base = datetime(2026, 11, 25, 6, 0, tzinfo=UTC)

    def stamp(seconds: float) -> str:
        # Both of control's recorded formats: with and without fractional seconds.
        moment = base + timedelta(seconds=seconds)
        text = moment.isoformat(timespec=rng.choice(["seconds", "microseconds"]))
        return text.replace("+00:00", rng.choice(["Z", "+00:00"]))

    leases = [(f"lease_eq_{n}", epoch) for n in range(40) for epoch in (1, 2)]
    clock = 0.0
    for _ in range(4_000):
        lease_id, epoch = rng.choice(leases)
        state_name = rng.choice(["running", "running", "exited", "unknown"])
        confirmed = int(state_name == "exited" and rng.random() < 0.7)
        if rng.random() < 0.02:  # rows the API refuses, still stored shapes the CHECK allows
            confirmed = 1
        clock += rng.choice([0.5, 1.0, 30.0, -5.0])
        connection.execute(
            """INSERT INTO execution_observations (id, grant_id, lease_id, worker_principal_id,
            epoch, operation_id, desired_state, observed_state, observed_at, provider_id,
            termination_confirmed, recorded_at)
            VALUES (?, 'grant_eq', ?, 'principal_eq', ?, NULL, 'admitted', ?, ?, 'c', ?, ?)""",
            (
                "observation_" + uuid.uuid4().hex,
                lease_id,
                epoch,
                state_name,
                stamp(clock),
                confirmed,
                stamp(clock),
            ),
        )
    compared = agreed_true = 0
    try:
        for lease_id, epoch in leases + [("lease_missing", 1)]:
            for _ in range(25):
                lease = {"id": lease_id, "epoch": epoch, "expires_at": stamp(rng.uniform(0, 9e3))}
                grant = {"expires_at": stamp(rng.uniform(0, 9e3))}
                now = base + timedelta(seconds=rng.uniform(0, 9e3))
                registered = rng.choice(
                    [None, now, stamp(rng.uniform(0, 9e3)), base + timedelta(seconds=10)]
                )
                old = _old_exited_inside_grant(connection, grant, lease, now, registered)
                new = ArtifactStore._exited_inside_grant(connection, grant, lease, now, registered)
                assert new == old, (lease_id, epoch, registered)
                compared += 1
                agreed_true += int(old)
    finally:
        connection.close()
    assert compared > 1_000 and 0 < agreed_true < compared


def test_schema_92_migration_adds_the_indexes_idempotently(tmp_path):
    from tessera_control.migrations import LATEST_SCHEMA_VERSION, migrate_database
    from tessera_control.output_lookup_indexes import (
        OUTPUT_LOOKUP_INDEXES,
        create_output_lookup_indexes,
    )
    from tessera_control.store import initialize_state

    names = [re.search(r"EXISTS (\w+)", sql).group(1) for sql in OUTPUT_LOOKUP_INDEXES]
    state = tmp_path / "state"
    initialize_state(state)
    path = state / "tessera.sqlite3"

    def indexes(connection):
        return {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index' AND name IN ({})".format(
                    ",".join("?" * len(names))
                ),
                names,
            )
        }

    with closing_db(state, _connect) as connection:
        fresh = indexes(connection)
        assert set(fresh) == set(names)
        events_before = connection.execute("SELECT count(*) FROM events").fetchone()[0]
        for name in names:
            connection.execute(f"DROP INDEX {name}")
        connection.execute("UPDATE schema_meta SET schema_version=91 WHERE singleton=1")
    assert LATEST_SCHEMA_VERSION == 92
    assert migrate_database(path, _connect) == 92
    with closing_db(state, _connect) as connection:
        assert indexes(connection) == fresh
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == events_before
        create_output_lookup_indexes(connection)  # idempotent
        assert indexes(connection) == fresh
