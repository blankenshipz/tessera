"""A running route is not cancelled for a control refusal that is not a verified revocation.

While the last post-start observe read the group running, a Forbidden (or HTTP 401/403) cancels
only when control is ready and the detail is exactly one of control's authority-gone details;
anything else is a transient control stall. Every refusal names the refused call, the HTTP status
when there was one and the bounded detail on the receipt. Control never turns a lock, a timed-out
call or a concurrent credential renewal into Forbidden.

Isolated orchestration (fake sessions) and a temporary control database only; no provider.
"""

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from test_salad_live_check_running_cancel import (
    SECRET,
    RunningSession,
    cancelled_before_output,
    concrete,
    drive,
)

from tessera_control.salad_live_check import (
    CANCEL_CAUSE_CLASSES,
    CONTROL_CALLS,
    RUNNING_CANCEL_PREFIX,
    UNRECOGNIZED_DETAIL,
)
from tessera_control.store import Conflict, Forbidden
from tessera_worker.control_channel import AUTHORITY_GONE_DETAILS, ControlChannelError

RETIRED, UNASSIGNED = "Worker is retired.", "Worker is not assigned to this execution grant."
assert {RETIRED, UNASSIGNED} == AUTHORITY_GONE_DETAILS


class ProbedSession(RunningSession):
    """A RunningSession whose control readiness probe answers from a script (last repeats)."""

    def __init__(self, *, readiness=("ready",), **kwargs):
        super().__init__(**kwargs)
        self.readiness = list(readiness)
        self.probes = 0

    def control_readiness(self):
        self.probes += 1
        answer = self.readiness.pop(0) if len(self.readiness) > 1 else self.readiness[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


def tagged(error, call="Store.get"):
    error.live_check_call = call
    return error


def unverified():
    return [
        lambda: Forbidden("Unauthorized"),
        lambda: Forbidden(),
        lambda: Forbidden(""),
        lambda: Forbidden("Authenticated exact Salad route required."),
        # A substring of a revocation detail is not one.
        lambda: Forbidden(RETIRED + " Until further notice."),
        lambda: Forbidden(RETIRED.lower()),
        lambda: ControlChannelError("HTTP error", 403),
        lambda: ControlChannelError("HTTP error", 401),
    ]


# --- not a verified revocation: skipped as a stall while running ------------------------------


@pytest.mark.parametrize("make", unverified())
def test_unverified_refusal_on_a_running_observe_is_a_stall(make):
    session = ProbedSession(retain_after=2, observe_errors=[None] + [make() for _ in range(5)])
    receipt = drive(session)
    assert receipt["status"] == "passed", receipt
    assert receipt["cancel_cause"]["class"] == "execution_complete"
    assert receipt["control_refusals_skipped"] == 5
    assert receipt["control_refusal"]["skipped"] is True
    assert receipt["control_refusal"]["verified_authority_gone"] is False
    assert receipt["control_refusal"]["step"] == "observe"
    assert not receipt["reason"].startswith(RUNNING_CANCEL_PREFIX)


@pytest.mark.parametrize("make", unverified())
def test_unverified_refusal_of_the_guardian_status_read_is_a_stall(make):
    session = ProbedSession(retain_after=3)
    calls = {"n": 0}
    original = session.evidence

    def evidence(route):
        calls["n"] += 1
        if 2 <= calls["n"] <= 6:
            raise make()
        return original(route)

    session.evidence = evidence
    receipt = drive(session)
    assert receipt["status"] == "passed", receipt
    assert receipt["control_refusals_skipped"] == 5
    assert receipt["control_refusal"]["step"] == "evidence"


@pytest.mark.parametrize("make", unverified())
def test_unverified_keepalive_refusal_inside_the_silence_bound_is_a_stall(make):
    # One refused beat, then the guardian is reached again: nothing is silent for long.
    session = ProbedSession(retain_after=4, beat_errors=[None, None, tagged(make())])
    receipt = drive(session)
    assert receipt["status"] == "passed", receipt
    assert receipt["control_refusals_skipped"] == 1
    assert receipt["control_refusal"] | {"detail": None} == {
        "kind": "forbidden",
        "step": "keepalive",
        "call": "Store.get",
        "http_status": receipt["control_refusal"]["http_status"],
        "detail": None,
        "verified_authority_gone": False,
        "control_readiness": "ready",
        "skipped": True,
    }
    assert receipt["control_stalls"] == 0


def test_a_persistent_keepalive_refusal_still_ends_at_the_silence_bound():
    # A refused beat never reaches the guardian: tolerated only inside the silence bound.
    refusals = [None] + [tagged(Forbidden("Unauthorized")) for _ in range(10)]
    session = ProbedSession(beat_errors=refusals)
    receipt = drive(session)
    assert cancelled_before_output(session)
    cause = receipt["cancel_cause"]
    assert cause["class"] == "silence_bound"
    assert cause["exception"] == "Forbidden"
    assert cause["refusal"]["call"] == "Store.get"
    assert cause["refusal"]["detail"] == "Unauthorized"
    assert cause["refusal"]["skipped"] is False
    assert receipt["control_refusals_skipped"] >= 1
    assert receipt["reason"] == RUNNING_CANCEL_PREFIX + "silence_bound"


# --- control unready: even an exact revocation detail is a stall -----------------------------


@pytest.mark.parametrize("unready", ["state_unavailable", "recovery_hold", OSError("locked")])
@pytest.mark.parametrize("detail", ["Unauthorized", RETIRED, UNASSIGNED])
def test_forbidden_while_control_is_unready_is_a_stall(unready, detail):
    session = ProbedSession(
        retain_after=2,
        readiness=[unready, unready, unready, "ready"],
        observe_errors=[None] + [Forbidden(detail) for _ in range(3)],
    )
    receipt = drive(session)
    assert receipt["status"] == "passed", receipt
    assert receipt["control_refusals_skipped"] == 3
    assert receipt["control_refusal"]["control_readiness"] == "state_unavailable" or (
        receipt["control_refusal"]["control_readiness"] == unready
    )
    assert receipt["cancel_cause"]["class"] == "execution_complete"


# --- a verified revocation on a ready control still cancels, and says which call --------------


@pytest.mark.parametrize("detail", [RETIRED, UNASSIGNED])
def test_exact_revocation_on_a_ready_control_cancels_and_names_the_call(detail):
    error = tagged(Forbidden(detail), "WorkerRegistryStore.heartbeat")
    session = ProbedSession(beat_errors=[None, None, error])
    receipt = drive(session)
    assert cancelled_before_output(session)
    cause = receipt["cancel_cause"]
    assert cause["class"] == "control_refused"
    assert cause["exception"] == "Forbidden"
    assert cause["last_provider_status"] == "running"
    assert cause["refusal"] == {
        "kind": "forbidden",
        "step": "keepalive",
        "call": "WorkerRegistryStore.heartbeat",
        "http_status": None,
        "detail": detail,
        "verified_authority_gone": True,
        "control_readiness": "ready",
        "skipped": False,
    }
    assert receipt["control_refusal"] == cause["refusal"]
    assert receipt["control_refusals_skipped"] == 0
    assert receipt["reason"] == RUNNING_CANCEL_PREFIX + "control_refused"


def test_a_verified_http_403_cancels_and_records_the_status():
    error = ControlChannelError("HTTP error", 403, authority_gone=True)
    session = ProbedSession(observe_errors=[None, error])
    receipt = drive(session)
    assert cancelled_before_output(session)
    cause = receipt["cancel_cause"]
    assert cause["class"] == "control_refused"
    assert cause["refusal"]["http_status"] == 403
    assert cause["refusal"]["step"] == "observe"
    assert cause["refusal"]["verified_authority_gone"] is True


def test_a_session_without_a_readiness_probe_decides_on_the_detail_alone():
    session = RunningSession(observe_errors=[None, Forbidden(RETIRED)])
    receipt = drive(session)
    assert receipt["cancel_cause"]["class"] == "control_refused"
    assert receipt["cancel_cause"]["refusal"]["control_readiness"] is None
    session = RunningSession(retain_after=2, observe_errors=[None, Forbidden("Unauthorized")])
    assert drive(session)["status"] == "passed"


def test_a_conflict_while_running_still_cancels_with_its_evidence():
    session = ProbedSession(
        observe_errors=[None, Conflict("Remote execution lease/epoch is fenced.")]
    )
    receipt = drive(session)
    cause = receipt["cancel_cause"]
    assert cause["class"] == "control_refused"
    assert cause["refusal"]["kind"] == "conflict"
    assert cause["refusal"]["detail"] == "Remote execution lease/epoch is fenced."
    assert cause["refusal"]["control_readiness"] is None
    assert session.probes == 0


# --- deadline, guardian stop cause and the pre-running bound are unchanged --------------------


def test_the_deadline_still_cancels_through_skipped_refusals():
    session = ProbedSession(observe_errors=[None] + [Forbidden("Unauthorized")] * 60)
    receipt = drive(session, max_polls=40, max_seconds=50)
    assert receipt["cancel_cause"]["class"] == "deadline"
    assert receipt["control_refusals_skipped"] >= 1
    assert not receipt["reason"].startswith(RUNNING_CANCEL_PREFIX)


def test_a_guardian_stop_cause_still_cancels_after_skipped_refusals():
    session = ProbedSession(
        observe_errors=[None, Forbidden("Unauthorized"), Forbidden("Unauthorized")],
        stop_cause="missed_poll",
    )
    session.retain_after = None
    receipt = drive(session, max_polls=6)
    assert receipt["control_refusals_skipped"] == 2
    assert receipt["cancel_cause"]["class"] == "guardian_stop_cause"
    assert not receipt["reason"].startswith(RUNNING_CANCEL_PREFIX)


def test_an_unverified_refusal_before_the_route_runs_still_cancels():
    # No post-start observe read running yet: the earlier behaviour is unchanged.
    session = ProbedSession(observe_errors=[Forbidden("Unauthorized")])
    receipt = drive(session)
    assert cancelled_before_output(session)
    assert receipt["last_provider_status"] is None
    assert receipt["cancel_cause"]["class"] == "control_refused"
    assert receipt["cancel_cause"]["refusal"]["skipped"] is False
    assert "control_refusals_skipped" in receipt and receipt["control_refusals_skipped"] == 0


def test_a_write_lock_still_stalls_and_is_not_a_refusal():
    locked = [None] + [sqlite3.OperationalError("database is locked") for _ in range(5)]
    session = ProbedSession(retain_after=2, observe_errors=locked)
    receipt = drive(session)
    assert receipt["status"] == "passed", receipt
    assert receipt["observe_transient_failures"] == 5
    assert "control_refusal" not in receipt and "control_refusals_skipped" not in receipt
    assert receipt["cancel_cause"]["refusal"] is None
    assert session.probes == 0


# --- evidence is bounded and never carries secrets --------------------------------------------


@pytest.mark.parametrize(
    "detail", [SECRET, "x" * 400, "Bearer " + "b" * 40, "see https://example.invalid/a?b=c"]
)
def test_refusal_detail_never_carries_secret_or_unbounded_text(detail):
    session = ProbedSession(observe_errors=[None, Forbidden(detail)])
    receipt = drive(session, max_polls=3)
    dumped = json.dumps(receipt)
    assert SECRET not in dumped and "example.invalid" not in dumped and "b" * 40 not in dumped
    assert UNRECOGNIZED_DETAIL in dumped
    assert receipt["cancel_cause"]["class"] in CANCEL_CAUSE_CLASSES


def test_a_non_refusal_error_carries_no_text():
    session = ProbedSession(observe_errors=[None, RuntimeError(SECRET)])
    receipt = drive(session)
    assert receipt["cancel_cause"]["class"] == "error"
    assert receipt["cancel_cause"]["refusal"] is None
    assert SECRET not in json.dumps(receipt)


# --- the concrete session names the refused control call -------------------------------------


@pytest.fixture
def unbounded_wall(monkeypatch):
    """The SIGALRM wall bound needs a free real-time timer; the test runner may hold it."""
    from contextlib import nullcontext

    monkeypatch.setattr("tessera_control.salad_live_check._wall_bound", lambda s: nullcontext())


@pytest.mark.usefixtures("unbounded_wall")
def test_concrete_keepalive_tags_the_refused_call():
    session = concrete(renew_error=Forbidden("Unauthorized"))
    with pytest.raises(Forbidden) as raised:
        session.keepalive("route")
    assert raised.value.live_check_call == "Store.renew_execution_lease"
    session = concrete(heartbeat_error=Forbidden("Unauthorized"))
    with pytest.raises(Forbidden) as raised:
        session.keepalive("route")
    assert raised.value.live_check_call == "TrustedSaladServer.heartbeat"
    assert {"Store.renew_execution_lease", "TrustedSaladServer.heartbeat"} <= CONTROL_CALLS


# --- control: a lock, a timeout or a concurrent renewal never becomes Forbidden ---------------


@pytest.fixture
def worker_world(tmp_path):
    from fastapi.testclient import TestClient
    from test_credentials import create_project, issue, token

    from tessera_control.api import create_app
    from tessera_control.store import Store, initialize_state

    state = tmp_path / "state"
    initialize_state(state)
    client = TestClient(create_app(state))
    project = create_project(client, state, "Refusal project", "refusal-project")
    store = Store(state)
    workers = []
    for label in ("worker-a", "worker-b"):
        value = token(label)
        credential, _ = issue(
            client,
            state,
            value=value,
            kind="worker",
            audience="worker",
            project_id=str(project["id"]),
            key=label,
        )
        workers.append((store.authenticate_worker(value), credential["credential_id"]))
    return store, workers


def _renew_concurrently(database_path, credential_id):
    """A renewal (expiry update + renewal row) committed by another connection, or False."""
    other = sqlite3.connect(database_path, timeout=0, isolation_level=None)
    try:
        other.execute("BEGIN IMMEDIATE")
        now = datetime.now(UTC)
        new = (now + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        owner = other.execute("SELECT id FROM principals WHERE role='human' LIMIT 1").fetchone()
        other.execute("UPDATE credentials SET expires_at=? WHERE id=?", (new, credential_id))
        other.execute(
            "INSERT INTO worker_credential_renewals VALUES (?,?,?,?,?)",
            (
                "renewal_" + uuid.uuid4().hex,
                credential_id,
                now.isoformat().replace("+00:00", "Z"),
                new,
                owner[0],
            ),
        )
        other.execute("COMMIT")
        return True
    except sqlite3.OperationalError:
        if other.in_transaction:
            other.execute("ROLLBACK")
        return False
    finally:
        other.close()


def _read_during_renewal(store, worker, credential_id):
    from tessera_control.store import _connect

    connection = _connect(store.database_path)
    fired = {}

    def trace(statement):
        if "worker_credential_renewals" in statement and "committed" not in fired:
            fired["committed"] = _renew_concurrently(store.database_path, credential_id)

    connection.set_trace_callback(trace)
    try:
        return store._live_principal(connection, worker, audience="worker"), fired
    finally:
        connection.close()


def test_a_concurrent_renewal_is_never_read_half_applied(worker_world, monkeypatch):
    store, [(unguarded, unguarded_id), (worker, credential_id)] = worker_world
    # The defect this guards: without one read snapshot the renewal lands between the
    # credential and renewal reads, and a live credential is refused as "Unauthorized".
    from contextlib import nullcontext

    import tessera_control.store as store_module

    with monkeypatch.context() as patched:
        patched.setattr(store_module, "_read_snapshot", lambda connection: nullcontext())
        with pytest.raises(Forbidden, match="Unauthorized"):
            _read_during_renewal(store, unguarded, unguarded_id)
    actor, fired = _read_during_renewal(store, worker, credential_id)
    assert actor["credential_id"] == credential_id
    # The writer waited for the read (a stall at the writer), never a refusal at the reader.
    assert fired == {"committed": False}
    assert _renew_concurrently(store.database_path, credential_id) is True
    with store_module._connect(store.database_path) as db:
        assert store._live_principal(db, worker, audience="worker")["credential_id"] == (
            credential_id
        )


def test_a_locked_database_is_a_stall_not_forbidden(worker_world, monkeypatch):
    store, [(worker, _), _] = worker_world
    import tessera_control.store as store_module

    real = store_module._connect

    def quick(path):
        connection = real(path)
        connection.execute("PRAGMA busy_timeout = 0")
        return connection

    monkeypatch.setattr(store_module, "_connect", quick)
    holder = sqlite3.connect(store.database_path, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            with store_module._connect(store.database_path) as db:
                store._live_principal(db, worker, audience="worker")
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_a_timed_out_licence_label_read_is_not_forbidden(monkeypatch):
    from tessera_control import license_labels, provider_inputs, provider_trust

    class Policy:
        def allowed_license_labels(self, provider_id):
            return frozenset({"open"})

    monkeypatch.setattr(provider_trust, "current_policy", lambda connection: Policy())

    def expired(connection, snapshot_id):
        raise TimeoutError("bounded live operation expired")

    monkeypatch.setattr(license_labels, "snapshot_labels", expired)
    with pytest.raises(TimeoutError):
        provider_inputs._require_license_labels(None, "provider", "snapshot")

    def unreadable(connection, snapshot_id):
        raise KeyError("labels")

    monkeypatch.setattr(license_labels, "snapshot_labels", unreadable)
    with pytest.raises(Forbidden):  # an unreadable label set still refuses (fail closed)
        provider_inputs._require_license_labels(None, "provider", "snapshot")
