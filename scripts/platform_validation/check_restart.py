"""Item 7: custody, holds, idempotent replay and events survive SIGKILL + restart."""

from __future__ import annotations

import time

from .common import Context, Journey, Result, db_counts, db_rows
from .real_run import RealRun, _docker, local_python_image

CAPABILITY = "restart-recovery"

COUNTS = {
    "events": "SELECT count(*) FROM events",
    "attempts": "SELECT count(*) FROM research_experiment_admitted_attempts",
    "held_cents": "SELECT coalesce(sum(held_cents),0) FROM execution_project_limits",
}
EVENTS = "SELECT rowid, event_type FROM events ORDER BY rowid"
LEASES = "SELECT count(*) FROM execution_leases"


def run(ctx: Context) -> list[Result]:
    out: list[Result] = []

    def add(case: str, ok: bool | str, evidence: str) -> None:
        # `ok` is a bool (PASS/FAIL) or an explicit status string such as "GAP".
        status = ok if isinstance(ok, str) else ("PASS" if ok else "FAIL")
        out.append(Result(CAPABILITY, case, status, evidence))

    ctx.start_service()
    # Committed state plus idempotent create.
    key = ctx.new_key()
    status, project = ctx.request("POST", "/projects", {"name": "restart"}, idempotency_key=key)
    journey = Journey(ctx)
    grant, status_a, attempt, admit_key = journey.admit("baseline")
    # Worker lease: the narrowest public flow that takes custody of the grant.
    ctx.call("POST", "/worker/heartbeat", {}, headers=journey.worker_headers())
    lease_key = ctx.new_key()
    lease = ctx.call(
        "POST",
        "/worker/leases",
        {"grant_id": grant["id"]},
        key=lease_key,
        headers=journey.worker_headers(),
    )
    before = db_counts(ctx.state_dir, COUNTS)
    events_before = db_rows(ctx.state_dir, EVENTS)
    add(
        "pre-kill-custody-held",
        status_a == 201
        and bool(lease.get("id"))
        and before["attempts"] == 1
        and before["held_cents"] > 0,
        f"admitted={status_a} lease_id={'set' if lease.get('id') else 'none'} "
        f"held_cents={before['held_cents']} events={before['events']}",
    )

    ctx.stop_service(kill=True)  # SIGKILL mid-flight, no shutdown hooks
    ctx.start_service()

    after = db_counts(ctx.state_dir, COUNTS)
    add("sigkill-state-intact", after == before, f"before={before} after={after}")
    add(
        "hold-survives-restart",
        after["held_cents"] == before["held_cents"] > 0,
        f"held_cents {before['held_cents']} -> {after['held_cents']}",
    )

    status, listed = ctx.request("GET", "/projects")
    names = [p["id"] for p in listed["projects"]] if status == 200 else []
    add(
        "committed-state-survives",
        project["id"] in names and journey.project["id"] in names,
        f"projects listed={len(names)}",
    )

    status, replay = ctx.request("POST", "/projects", {"name": "restart"}, idempotency_key=key)
    add(
        "project-idempotent-replay",
        status in (200, 201) and replay == project,
        f"replay status={status} identical={replay == project}",
    )

    path, request = journey.last_admit
    status, replay = ctx.request("POST", path, request, idempotency_key=admit_key)
    same = status in (200, 201) and replay == attempt
    add(
        "admission-idempotent-replay", same, f"replay status={status} identical={replay == attempt}"
    )
    after_replay = db_counts(ctx.state_dir, COUNTS)
    add(
        "no-second-start",
        after_replay["attempts"] == 1
        and after_replay["held_cents"] == before["held_cents"]
        and after_replay["events"] == before["events"],
        f"attempts={after_replay['attempts']} held={after_replay['held_cents']} "
        f"events {before['events']} -> {after_replay['events']}",
    )

    events_after = db_rows(ctx.state_dir, EVENTS)
    add(
        "event-history-survives",
        bool(events_before) and events_after[: len(events_before)] == events_before,
        f"event rows {len(events_before)} -> {len(events_after)}, prefix identical",
    )

    status, lease_replay = ctx.request(
        "POST",
        "/worker/leases",
        {"grant_id": grant["id"]},
        idempotency_key=lease_key,
        headers=journey.worker_headers(),
        auth="none",
    )
    add(
        "lease-idempotent-replay",
        status in (200, 201) and lease_replay == lease,
        f"replay status={status} identical={lease_replay == lease}",
    )
    status, second = ctx.request(
        "POST",
        "/worker/leases",
        {"grant_id": grant["id"]},
        idempotency_key=ctx.new_key(),
        headers=journey.worker_headers(),
        auth="none",
    )
    leases = db_counts(ctx.state_dir, {"n": LEASES})["n"]
    add(
        "lease-custody-not-duplicated",
        status == 409 and leases == 1,
        f"second lease request status={status} lease_rows={leases}",
    )

    try:
        _in_flight(ctx, add)
    except _NoImage:
        out.append(
            Result(
                CAPABILITY,
                "in-flight-provider-operation",
                "GAP",
                "no local python@sha256 image on this host (the harness never pulls)",
            )
        )
    except Exception as error:  # noqa: BLE001 - a crash in the live section is a FAIL
        add("in-flight-section", False, f"{type(error).__name__}: {error}")
    return out


class _NoImage(Exception):
    pass


# -- real Docker: control SIGKILL while a workload container is running -----------------
INFLIGHT_CODE = (
    "import time; time.sleep(40); open('/outputs/result.txt', 'w').write('generic output\\n')"
)
OP_ROWS = "SELECT * FROM external_operations WHERE grant_id='{g}' ORDER BY operation_id"
# The columns that bind an operation to its custody; progress columns (updated_at, observed
# state, provider id) legitimately change while the worker is still reporting.
OP_IDENTITY = (
    "SELECT operation_id, org_id, project_id, task_id, grant_id, lease_id, epoch, "
    "worker_principal_id, adapter_version FROM external_operations "
    "WHERE grant_id='{g}' ORDER BY operation_id"
)
LEASE_ROWS = (
    "SELECT id, epoch, grant_id, worker_principal_id FROM execution_leases WHERE grant_id='{g}'"
)
RECEIPTS = "SELECT count(*) FROM execution_run_receipts WHERE grant_id='{g}'"
EVENT_ROWS = "SELECT rowid, event_type FROM events ORDER BY rowid"
FORMAT = "{{.Id}}|{{.State.Status}}|{{.State.StartedAt}}|{{.State.ExitCode}}|{{.RestartCount}}"


def _container(name: str) -> dict | None:
    """Docker's own record of one exact-named container, or None when absent."""
    done = _docker("inspect", "--format", FORMAT, name)
    if done.returncode != 0:
        return None
    cid, status, started, exit_code, restarts = done.stdout.strip().split("|")
    return {
        "id": cid,
        "status": status,
        "started": started,
        "exit": int(exit_code),
        "restarts": int(restarts),
    }


def _named(name: str) -> int:
    done = _docker("ps", "-a", "--filter", f"name=^/{name}$", "--format", "{{.ID}}")
    return len(done.stdout.split()) if done.returncode == 0 else -1


def _facts(ctx: Context, grant_id: str, operation: str) -> dict:
    status, grant = ctx.request("GET", f"/execution-grants/{grant_id}")
    return {
        "operations": db_rows(ctx.state_dir, OP_ROWS.format(g=grant_id)),
        "identity": db_rows(ctx.state_dir, OP_IDENTITY.format(g=grant_id)),
        "leases": db_rows(ctx.state_dir, LEASE_ROWS.format(g=grant_id)),
        "grant": (status, grant),
        "events": db_counts(ctx.state_dir, {"n": "SELECT count(*) FROM events"})["n"],
        "held": db_counts(ctx.state_dir, {"n": COUNTS["held_cents"]})["n"],
        "receipts": db_counts(ctx.state_dir, {"n": RECEIPTS.format(g=grant_id)})["n"],
        "container": _container(f"tessera-op-{operation}"),
    }


def _worker_summary(record) -> str:
    w = record.worker
    return (
        f"rc={record.worker_returncode} state={w.get('state')} quarantined={w.get('quarantined')} "
        f"reason={w.get('reason')} run_receipt={(w.get('run_receipt') or {}).get('status')}"
    )


def _in_flight(ctx: Context, add) -> None:
    image = local_python_image()
    if image is None:
        raise _NoImage
    driver = RealRun(ctx, image)
    try:
        driver.setup()
        _in_flight_run(ctx, driver, add)
    finally:
        left = driver.cleanup()
        status, evidence = driver.residue_report(left)
        add("docker-residue", status == "PASS", f"{status}: {evidence}")


def _in_flight_run(ctx: Context, driver: RealRun, add) -> None:
    record = driver.prepare_task(
        "in-flight restart", INFLIGHT_CODE, output_paths=["result.txt"], max_duration_seconds=120
    )
    grant_id = record.grant_id
    key = ctx.new_key()
    status, replayed = ctx.request(
        "POST", "/projects", {"name": "in-flight replay"}, idempotency_key=key
    )
    first_project = (status, replayed)
    handle = driver.start_worker(record)

    operation, container = "", None
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and handle.process.poll() is None:
        ops = driver._control_operations(grant_id)
        if ops:
            operation = ops[0]
            container = _container(f"tessera-op-{operation}")
            if container and container["status"] == "running":
                break
        time.sleep(0.5)
    running = bool(container and container["status"] == "running")
    if not running:
        driver.finish_worker(handle, timeout=30)
        add(
            "inflight-running-at-kill",
            False,
            f"op={operation or 'none'} container={container} worker {_worker_summary(record)}",
        )
        return
    before = _facts(ctx, grant_id, operation)
    events_before = db_rows(ctx.state_dir, EVENT_ROWS)
    lease = before["leases"][0] if before["leases"] else ()
    add(
        "inflight-running-at-kill",
        len(before["operations"]) == 1 and len(before["leases"]) == 1,
        f"op={operation[:8]} lease={str(lease[0])[-8:]} epoch={lease[1] if lease else None} "
        f"grant={before['grant'][1].get('status')} events={before['events']} "
        f"held={before['held']} container={container['id'][:12]} started={container['started']}",
    )

    ctx.stop_service(kill=True)  # SIGKILL control while the workload container is running
    # With control dead nothing can write the state, so this full row is the exact state
    # the restart must preserve (the pre-kill snapshot may predate a final worker report).
    quiesced = db_rows(ctx.state_dir, OP_ROWS.format(g=grant_id))
    driver.finish_worker(handle, timeout=75)
    outage_container = _container(f"tessera-op-{operation}")
    worker_out = record.worker
    claimed_exit = worker_out.get("state") == "exited"
    hung = record.worker_returncode == -1  # did not exit within the bound and was killed
    add(
        "outage-worker-behaviour",
        # The worker must react to the outage (exit with a parseable result) and must not
        # claim an exit it could not confirm with control; a worker that never returns is a
        # FAIL, not an acceptable outcome.
        (not claimed_exit) and bool(worker_out) and not hung,
        f"observed: {_worker_summary(record)} detail={worker_out.get('detail')}; "
        f"container after outage={outage_container and outage_container['status']} "
        f"exit={outage_container and outage_container['exit']}; no exited/quarantined claim "
        "without control confirmation",
    )

    ctx.restart_service(reuse_port=True)
    after = _facts(ctx, grant_id, operation)
    add(
        "restart-same-port",
        after["grant"][0] == 200 and ctx.base_url.endswith(f":{ctx.port}"),
        f"control ready again at the same URL {ctx.base_url}",
    )
    identity_same = after["identity"] == before["identity"]
    row_same = after["operations"] == quiesced
    same = (
        identity_same
        and row_same
        and after["leases"] == before["leases"]
        and after["grant"] == before["grant"]
        and after["held"] == before["held"]
    )
    add(
        "custody-survives-sigkill",
        same and len(after["operations"]) == 1 and len(after["leases"]) == 1,
        f"operation identity (operation, lease, epoch, grant, worker, adapter) identical to "
        f"before the kill={identity_same}; full operation row identical to the state at "
        f"SIGKILL={row_same}; lease/epoch identical={after['leases'] == before['leases']}; "
        f"grant identical={after['grant'] == before['grant']}; held "
        f"{before['held']}->{after['held']}; operations={len(after['operations'])}",
    )
    events_after = db_rows(ctx.state_dir, EVENT_ROWS)
    add(
        "inflight-event-history-prefix",
        events_after[: len(events_before)] == events_before,
        f"events {len(events_before)} -> {len(events_after)}, prefix identical",
    )
    status, again = ctx.request(
        "POST", "/projects", {"name": "in-flight replay"}, idempotency_key=key
    )
    add(
        "inflight-idempotent-replay",
        (status, again) == first_project and status in (200, 201),
        f"earlier request replayed status={status} identical={(status, again) == first_project}",
    )

    # Recovery: the same worker CLI against the same grant and workspace, same control URL.
    handle = driver.start_worker(record)
    driver.finish_worker(handle, timeout=150)
    final = _facts(ctx, grant_id, operation)
    add(
        "recovery-worker-returned",
        record.worker_returncode != -1 and bool(record.worker),
        "the recovery run returned a parseable result within its bound (this does not claim "
        f"recovery succeeded): {_worker_summary(record)} detail={record.worker.get('detail')} "
        f"stderr={record.worker_stderr[:80]}",
    )
    box = final["container"]
    add(
        "container-created-once",
        _named(f"tessera-op-{operation}") == 1
        and box is not None
        and box["id"] == container["id"]
        and box["started"] == container["started"]
        and box["restarts"] == 0,
        f"containers named tessera-op-{operation[:8]}={_named(f'tessera-op-{operation}')} "
        f"id same={box and box['id'] == container['id']} StartedAt same="
        f"{box and box['started'] == container['started']} final status="
        f"{box and box['status']} exit={box and box['exit']} restarts={box and box['restarts']}",
    )
    honest_ok, honest_note = _honest(ctx, driver, record, operation, final)
    add("final-evidence-honest", honest_ok, honest_note)
    recovered = record.worker.get("state") == "exited" and record.worker.get("quarantined") is True
    add(
        "provider-effect-reconcile-after-crash",
        "PASS" if (recovered and honest_ok) else ("GAP" if honest_ok else "FAIL"),
        (
            "recovery reached a confirmed exit and control holds a matching receipt: "
            if recovered and honest_ok
            else "NOT PROVEN: after the control crash the workload was cancelled by the worker's "
            "outage policy, so recovery did not reach a confirmed exit with a receipt "
            f"(final worker state={record.worker.get('state')} reason={record.worker.get('reason')}"
            f"; container exit={box and box['exit']}); reconciling a provider effect that "
            "completes while control is down is not demonstrated. "
        )
        + honest_note,
    )
    add(
        "no-duplicate-receipt-or-lease",
        final["receipts"] <= 1
        and final["leases"] == before["leases"]
        and len(final["operations"]) == 1
        and final["operations"][0][0] == operation,
        f"receipts={final['receipts']} leases={len(final['leases'])} (same lease/epoch="
        f"{final['leases'] == before['leases']}) operations={len(final['operations'])}",
    )


def _honest(ctx: Context, driver: RealRun, record, operation: str, final: dict):
    """(ok, evidence): the worker's final claim must be backed by Docker and control."""
    w, box = record.worker, final["container"]
    state, quarantined = w.get("state"), w.get("quarantined")
    if state == "exited" and quarantined is True:
        status, receipt = ctx.request("GET", f"/external-operations/{operation}/run-receipt")
        lease = final["leases"][0] if final["leases"] else ("", 0)
        ok = (
            box is not None
            and box["status"] == "exited"
            and status == 200
            and final["receipts"] == 1
            and receipt.get("operation_id") == operation
            and receipt.get("lease_id") == lease[0]
            and receipt.get("epoch") == lease[1]
            and receipt.get("exit_code") == box["exit"]
        )
        return ok, (
            f"exited+quarantined; container status={box and box['status']} exit="
            f"{box and box['exit']}; control receipt GET {status} exit_code="
            f"{receipt.get('exit_code') if isinstance(receipt, dict) else None} lease/epoch/op "
            f"match={ok}; worker receipt={w.get('run_receipt')}"
        )
    retained = list(driver.root.glob("worker-*/outputs/development-only-cpu-outputs/workspaces/*"))
    no_claim = "run_receipt" not in w and final["receipts"] == 0
    ok = state == "unknown" and quarantined is False and bool(retained) and no_claim
    return ok, (
        f"state={state} quarantined={quarantined} reason={w.get('reason')} workspace retained="
        f"{bool(retained)} no run receipt claimed at worker or control={no_claim}; container="
        f"{box and box['status']}"
    )
