"""Resolving attempts whose worker confirmed the exit (ADR 0053). Fakes only: in-process control."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from campaign_runner_harness import World, dump, stage_spec

from tessera_control import artifact_limits as limits_module
from tessera_control import campaign_exited_resolve as cer
from tessera_control.campaign_models import stage_provider
from tessera_control.campaign_receipts import build_receipt
from tessera_control.campaign_runner import CampaignRunner

METRIC = [{"name": "score", "path": "data.json", "json_pointer": "/score"}]
PAST = "2000-01-01T00:00:00Z"


def _world(tmp_path, **stage_extra):
    world = World(tmp_path)
    spec = world.env.spec()
    spec["stages"] = [stage_spec(world.env, "s1", metrics=METRIC, **stage_extra)]
    return world, world.campaign(spec)


def _admit(world, cid):
    world.tick_until(
        lambda: any(a["status"] == "admitted" for a in world.attempts(cid)), worker=False
    )


def _drop(world, *needles):
    """Lose the worker's reports whose path mentions any needle (observations still arrive)."""
    original = world.channel.post

    def cut(path, body, key):
        if any(n in path for n in needles):
            raise ConnectionError("synthetic report loss")
        return original(path, body, key)

    world.channel.post = cut
    return original


def _later(world, minutes):
    world.runner.now = lambda: datetime.now(UTC) + timedelta(minutes=minutes)


def _exit_without_receipt(world, cid):
    """The container exited and its exit was reported; the run receipt and manifest were lost."""
    _admit(world, cid)
    _drop(world, "run-receipt", "manifest", "artifact")
    world.poller.run(max_cycles=1)
    return world.attempts(cid)[-1]


def _ledger(world, cid):
    return world.row("SELECT held_cents, settled_cents FROM campaigns WHERE id=?", cid)


def _confirmed(world, grant_id):
    operation = world.row("SELECT operation_id FROM external_operations WHERE grant_id=?", grant_id)
    with_conn = world.env.query(
        "SELECT observed_state, termination_confirmed FROM execution_observations "
        "WHERE operation_id=?",
        operation["operation_id"],
    )
    return [(r["observed_state"], r["termination_confirmed"]) for r in with_conn]


def test_a_confirmed_exit_without_a_receipt_is_resolved_after_the_grace_and_releases_the_grant(
    tmp_path,
):
    world, campaign = _world(tmp_path, retry={"max_attempts": 2})
    cid = campaign["id"]
    attempt = _exit_without_receipt(world, cid)
    assert ("exited", 1) in _confirmed(world, attempt["grant_id"])
    world.runner.tick()  # inside the grace the normal flow may still deliver the receipt
    held = world.row("SELECT * FROM campaign_stage_attempts WHERE id=?", attempt["id"])
    assert (
        held["status"] in ("admitted", "running")
        and world.row("SELECT status FROM execution_grants WHERE id=?", attempt["grant_id"])[
            "status"
        ]
        == "active"
    )
    _later(world, 10)
    world.runner.tick()
    done = world.row("SELECT * FROM campaign_stage_attempts WHERE id=?", attempt["id"])
    assert (done["status"], done["failure_class"]) == (
        "failed_transient",
        cer.EXIT_CONFIRMED_UNRESOLVED,
    )
    assert (done["settled_cents"], done["settlement_basis"]) == (0, "local_zero")
    grant = world.row("SELECT * FROM execution_grants WHERE id=?", attempt["grant_id"])
    assert (
        grant["status"] == "reconciled" and grant["settlement_cents"] == 0
    )  # slot and grant released with the settlement
    assert _ledger(world, cid)["held_cents"] == 0
    (event,) = world.events_of("campaign.attempt_exit_resolved")
    assert event["payload"]["automatic"] is True and event["payload"]["is_evidence"] is False
    assert (
        event["payload"]["was_status"] in ("admitted", "running")
        and event["payload"]["evidence_digest"]
    )
    (granted,) = world.events_of("execution.exited_grant_reconciled")
    assert (
        granted["payload"]["provider_termination_inferred"] is False
        and granted["payload"]["settlement_cents"] == 0
    )
    assert world.attempts(cid)[0]["failure_class"] == cer.EXIT_CONFIRMED_UNRESOLVED


def test_the_outcome_is_on_the_campaign_receipt_and_never_a_pass(tmp_path):
    world, campaign = _world(tmp_path)  # one attempt only: the stage fails instead of hanging
    cid = campaign["id"]
    _exit_without_receipt(world, cid)
    _later(world, 10)
    for _ in range(6):
        world.runner.tick()
        if world.status(cid) in {"failed", "stopped", "succeeded"}:
            break
    assert world.status(cid) != "succeeded", dump(world, cid)
    stored = world.row("SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", cid)
    entries = [
        a for stage in json.loads(stored["receipt_json"])["stages"] for a in stage["attempts"]
    ]
    assert entries[0]["failure_class"] == cer.EXIT_CONFIRMED_UNRESOLVED
    assert entries[0]["exit_resolved"]["is_evidence"] is False and entries[0]["exit_code"] is None
    assert entries[0]["settled_cents"] == 0 or entries[0].get("settlement_cents", 0) == 0


def test_the_resolved_attempt_frees_the_slot_so_the_retry_launches(tmp_path):
    world, campaign = _world(tmp_path, retry={"max_attempts": 2})
    cid = campaign["id"]
    first = _exit_without_receipt(world, cid)
    _later(world, 10)
    world.runner.tick()
    assert (
        world.row("SELECT status FROM execution_grants WHERE id=?", first["grant_id"])["status"]
        == "reconciled"
    )
    attempts = world.attempts(cid)
    assert [a["attempt"] for a in attempts][0] == 1 and attempts[0]["status"] == "failed_transient"


def test_resolution_is_idempotent_and_a_second_sweep_changes_nothing(tmp_path):
    world, campaign = _world(tmp_path, retry={"max_attempts": 2})
    cid = campaign["id"]
    attempt = _exit_without_receipt(world, cid)
    _later(world, 10)
    world.runner.tick()
    before = (
        world.row("SELECT * FROM execution_grants WHERE id=?", attempt["grant_id"]),
        world.row("SELECT * FROM execution_project_limits"),
        len(world.events_of("campaign.attempt_exit_resolved")),
    )
    ctx = world.runner._load(cid)
    stale = dict(attempt)  # a sweep holding the stale pre-resolution row must not act twice
    assert world.runner._resolve_exited(ctx, stale) is False
    world.runner._resolve_exited_attempts(ctx)
    after = (
        world.row("SELECT * FROM execution_grants WHERE id=?", attempt["grant_id"]),
        world.row("SELECT * FROM execution_project_limits"),
        len(world.events_of("campaign.attempt_exit_resolved")),
    )
    assert (
        dict(before[0]) == dict(after[0])
        and dict(before[1]) == dict(after[1])
        and before[2] == after[2] == 1
    )


def test_a_second_runner_that_lost_the_lease_cannot_resolve(tmp_path):
    world, campaign = _world(tmp_path, retry={"max_attempts": 2})
    attempt = _exit_without_receipt(world, campaign["id"])
    _later(world, 10)
    other = CampaignRunner(world.env.state)
    other.now = world.runner.now
    ctx = other._load(campaign["id"])
    try:
        resolved = other._resolve_exited(ctx, dict(attempt))
    except Exception as error:  # noqa: BLE001 - the lease check refuses; either way nothing changes
        assert "lease" in type(error).__name__.lower() or "lease" in str(error).lower()
        resolved = False
    assert resolved is False
    assert (
        world.row("SELECT status FROM execution_grants WHERE id=?", attempt["grant_id"])["status"]
        == "active"
    )


def test_an_unknown_attempt_with_no_evidence_stays_unknown_and_nothing_is_invented(tmp_path):
    world, campaign = _world(tmp_path)
    cid = campaign["id"]
    _admit(world, cid)
    _drop(world, "observations", "run-receipt", "manifest", "artifact")
    world.poller.run(max_cycles=1)
    attempt = world.attempts(cid)[-1]
    world.env.execute(
        "UPDATE execution_grants SET expires_at=? WHERE id=?", PAST, attempt["grant_id"]
    )
    world.env.execute(
        "UPDATE execution_leases SET expires_at=?, hard_expires_at=? WHERE grant_id=?",
        PAST,
        PAST,
        attempt["grant_id"],
    )
    for _ in range(6):
        world.runner.tick()
    latest = world.attempts(cid)[-1]
    assert latest["status"] == "unknown" and latest["settled_cents"] is None
    assert (
        world.row("SELECT status FROM execution_grants WHERE id=?", attempt["grant_id"])["status"]
        == "active"
    )
    assert (
        world.row("SELECT count(*) AS n FROM execution_observations")["n"] == 0
    )  # no observation was invented
    assert world.events_of("campaign.attempt_exit_resolved") == []


def test_an_unknown_attempt_is_healed_once_a_confirmed_exit_follows_an_unconfirmed_one(tmp_path):
    world, campaign = _world(tmp_path, retry={"max_attempts": 2})
    cid = campaign["id"]
    attempt = _exit_without_receipt(world, cid)
    world.env.execute(
        "UPDATE campaign_stage_attempts SET status='unknown', failure_class='observed_unknown', "
        "failure_detail='observed exited, termination confirmed=False' WHERE id=?",
        attempt["id"],
    )
    world.env.execute(
        "UPDATE campaign_stages SET status='blocked_unknown' WHERE campaign_id=?", cid
    )
    world.env.execute("UPDATE campaigns SET status='blocked' WHERE id=?", cid)
    world.runner.tick()  # the confirmed exit is on record and uncontradicted: resolved at once
    done = world.row("SELECT * FROM campaign_stage_attempts WHERE id=?", attempt["id"])
    assert (
        done["status"] == "failed_transient"
        and done["failure_class"] == cer.EXIT_CONFIRMED_UNRESOLVED
    )
    assert (
        world.row("SELECT status FROM execution_grants WHERE id=?", attempt["grant_id"])["status"]
        == "reconciled"
    )
    assert world.status(cid) in {"running", "succeeded"}  # the campaign is unblocked
    assert world.stages(cid)["s1"] != "blocked_unknown"


def _assess(world, attempt_id, minutes=10, codes=(), free=True):
    with world.runner.store.runner_connection() as connection:
        row = connection.execute(
            "SELECT * FROM campaign_stage_attempts WHERE id=?", (attempt_id,)
        ).fetchone()
        return cer.assess(
            connection,
            row,
            now=datetime.now(UTC) + timedelta(minutes=minutes),
            transient_codes=codes,
            free_worker_grant=free,
        )


def _end_lease(world, grant_id):
    world.env.execute("UPDATE execution_leases SET expires_at=? WHERE grant_id=?", PAST, grant_id)


def _start_upload(world, grant_id, status="uploading"):
    lease = world.row("SELECT * FROM execution_leases WHERE grant_id=?", grant_id)
    operation = world.row("SELECT operation_id FROM external_operations WHERE grant_id=?", grant_id)
    world.env.execute(
        """INSERT INTO artifact_uploads (id, lease_id, worker_principal_id, epoch, operation_id,
        relative_path, sha256, size, received_bytes, status, created_at)
        VALUES ('upload-1', ?, ?, ?, ?, 'big.bin', ?, 1000, 10, ?, ?)""",
        lease["id"],
        lease["worker_principal_id"],
        lease["epoch"],
        operation["operation_id"],
        "a" * 64,
        status,
        PAST,
    )


def test_a_live_lease_or_an_upload_in_progress_is_never_overtaken(tmp_path):
    world, campaign = _world(tmp_path, retry={"max_attempts": 2})
    cid = campaign["id"]
    attempt = _exit_without_receipt(world, cid)
    aid, gid = attempt["id"], attempt["grant_id"]
    far = datetime.now(UTC) + timedelta(days=3650)
    world.env.execute(
        "UPDATE execution_leases SET expires_at=? WHERE grant_id=?",
        far.isoformat().replace("+00:00", "Z"),
        gid,
    )
    assert _assess(world, aid) is None  # the lease is live: the worker may still be collecting
    _end_lease(world, gid)
    assert _assess(world, aid) is not None
    _start_upload(world, gid)
    _later(world, 10)  # well past the 120 s grace, the grant still alive
    world.runner.tick()
    assert _assess(world, aid) is None
    held = world.row("SELECT status FROM campaign_stage_attempts WHERE id=?", aid)["status"]
    assert held in ("admitted", "running")
    assert world.row("SELECT status FROM execution_grants WHERE id=?", gid)["status"] == "active"
    assert not world.events_of("campaign.attempt_exit_resolved")
    # The upload ending (or the grant lapsing) hands it back to the resolution.
    world.env.execute("UPDATE artifact_uploads SET status='completed'")
    assert _assess(world, aid) is not None
    world.env.execute("UPDATE artifact_uploads SET status='uploading'")
    world.env.execute("UPDATE execution_grants SET expires_at=? WHERE id=?", PAST, gid)
    assert _assess(world, aid) is not None


def test_the_grace_counts_from_when_control_recorded_the_exit_not_the_workers_clock(tmp_path):
    world, campaign = _world(tmp_path, retry={"max_attempts": 2})
    attempt = _exit_without_receipt(world, campaign["id"])
    gid = attempt["grant_id"]
    _end_lease(world, gid)
    # A worker clock far ahead does not stretch the grace; control's own time governs it.
    future = (datetime.now(UTC) + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    world.env.execute(
        "UPDATE execution_observations SET observed_at=? WHERE grant_id=? "
        "AND observed_state='exited'",
        future,
        gid,
    )
    assert _assess(world, attempt["id"], minutes=0) is None
    assert _assess(world, attempt["id"], minutes=10) is not None


def test_only_a_non_billable_worker_grant_provider_is_settled_at_zero(tmp_path, monkeypatch):
    world, campaign = _world(tmp_path, retry={"max_attempts": 2})
    attempt = _exit_without_receipt(world, campaign["id"])
    _end_lease(world, attempt["grant_id"])
    assert _assess(world, attempt["id"]) is not None
    assert _assess(world, attempt["id"], free=False) is None  # billable or route-based: untouched
    real = stage_provider(world.runner.stage_for(attempt).provider)

    class Billable:
        descriptor = replace(real.descriptor, billable=True)

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr(CampaignRunner, "_provider", staticmethod(lambda stage: Billable()))
    _later(world, 10)
    world.runner.tick()
    row = world.row(
        "SELECT status, settled_cents FROM campaign_stage_attempts WHERE id=?", attempt["id"]
    )
    assert row["settled_cents"] is None and row["status"] in ("admitted", "running")
    assert (
        world.row("SELECT status FROM execution_grants WHERE id=?", attempt["grant_id"])["status"]
        == "active"
    )
    assert not world.events_of("campaign.attempt_exit_resolved")


def test_each_guard_blocks_resolution_contradiction_receipt_settled_or_reconciled(
    tmp_path,
):
    world, campaign = _world(tmp_path, retry={"max_attempts": 2})
    attempt = _exit_without_receipt(world, campaign["id"])
    aid, gid = attempt["id"], attempt["grant_id"]
    assert _assess(world, aid) is not None
    assert _assess(world, aid, minutes=0) is None  # inside the grace the receipt may still arrive
    operation = world.row("SELECT operation_id FROM external_operations WHERE grant_id=?", gid)[
        "operation_id"
    ]
    obs = world.row(
        "SELECT * FROM execution_observations WHERE operation_id=? ORDER BY rowid DESC", operation
    )
    # a later adverse observation (running, unconfirmed) contradicts the confirmed exit
    later = (
        datetime.fromisoformat(obs["observed_at"].replace("Z", "+00:00")) + timedelta(seconds=1)
    ).isoformat()
    cols = [c for c in obs.keys() if c != "id"]
    values = [
        later
        if c == "observed_at"
        else "running"
        if c == "observed_state"
        else 0
        if c == "termination_confirmed"
        else obs[c]
        for c in cols
    ]
    world.env.execute(
        f"INSERT INTO execution_observations (id, {','.join(cols)}) "
        f"VALUES (?, {','.join('?' for _ in cols)})",
        "obs-late",
        *values,
    )
    assert _assess(world, aid) is None
    world.env.execute("DELETE FROM execution_observations WHERE id='obs-late'")
    assert _assess(world, aid) is not None
    world.env.execute("UPDATE campaign_stage_attempts SET settled_cents=0 WHERE id=?", aid)
    assert _assess(world, aid) is None
    world.env.execute("UPDATE campaign_stage_attempts SET settled_cents=NULL WHERE id=?", aid)
    world.env.execute("UPDATE execution_grants SET status='reconciled' WHERE id=?", gid)
    assert _assess(world, aid) is None


def test_cost_is_never_above_the_hold_and_a_declared_transient_exit_code_is_classified(tmp_path):
    world, campaign = _world(tmp_path)
    attempt = _exit_without_receipt(world, campaign["id"])
    verdict = _assess(world, attempt["id"])
    assert verdict.cents <= verdict.evidence["hold_cents"] and verdict.basis == "local_zero"
    with pytest.raises(ValueError, match="never exceed"):
        with world.runner.store.runner_connection() as connection:
            grant = connection.execute(
                "SELECT * FROM execution_grants WHERE id=?", (attempt["grant_id"],)
            ).fetchone()
            cer.release_grant(
                connection, grant, cents=grant["max_exposure_cents"] + 1, principal_id="p", now=PAST
            )


def test_a_nonzero_hold_is_released_with_the_cost_clamped_and_the_ledger_balanced(tmp_path):
    world, campaign = _world(tmp_path, retry={"max_attempts": 2})
    cid = campaign["id"]
    attempt = _exit_without_receipt(world, cid)
    world.env.execute(
        "UPDATE execution_grants SET max_exposure_cents=7 WHERE id=?", attempt["grant_id"]
    )
    world.env.execute("UPDATE execution_project_limits SET held_cents=held_cents+7")
    before = dict(world.row("SELECT * FROM execution_project_limits"))
    _later(world, 10)
    world.runner.tick()
    grant = world.row("SELECT * FROM execution_grants WHERE id=?", attempt["grant_id"])
    after = dict(world.row("SELECT * FROM execution_project_limits"))
    assert grant["status"] == "reconciled" and grant["settlement_cents"] == 0
    assert (
        after["held_cents"] == before["held_cents"] - 7
        and after["settled_cents"] == before["settled_cents"]
    )


def _insert_run_receipt(world, attempt, exit_code):
    """A stored run receipt for the attempt's operation (the fake provider receipt would exclude it,
    so that trigger is dropped)."""
    operation = world.row("SELECT * FROM external_operations WHERE grant_id=?", attempt["grant_id"])
    world.env.execute("DROP TRIGGER IF EXISTS execution_run_receipts_exclude_fake")
    row = {
        "operation_id": operation["operation_id"],
        "org_id": operation["org_id"],
        "project_id": operation["project_id"],
        "task_id": operation["task_id"],
        "grant_id": operation["grant_id"],
        "lease_id": operation["lease_id"],
        "epoch": operation["epoch"],
        "worker_principal_id": operation["worker_principal_id"],
        "provider_kind": "local_docker_cpu",
        "receipt_evidence": "observed_provider_execution",
        "adapter_version": "t1",
        "provider_id": "container-test",
        "command_digest": "c" * 64,
        "exit_code": exit_code,
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:00:01Z",
        "duration_ms": 1000,
        "output_digest": "a" * 64,
        "receipt_payload_json": "{}",
        "receipt_digest": "d" * 64,
        "provider_success": "not_established",
        "scientific_validity": "not_assessed",
        "created_at": "2026-01-01T00:00:01Z",
    }
    world.env.execute(
        f"INSERT INTO execution_run_receipts ({','.join(row)}) "
        f"VALUES ({','.join('?' for _ in row)})",
        *row.values(),
    )


@pytest.mark.parametrize(
    ("code", "codes", "status", "klass"),
    [
        (3, (3,), "failed_transient", "t2_declared_exit_code"),
        (9, (3,), "failed_permanent", "undeclared_exit_code"),
        (0, (), "failed_transient", cer.EXIT_CONFIRMED_UNRESOLVED),
    ],
)
def test_a_recorded_exit_code_is_classified_like_the_normal_observation(
    tmp_path, code, codes, status, klass
):
    world, campaign = _world(tmp_path)
    attempt = _exit_without_receipt(world, campaign["id"])
    world.env.execute(
        "UPDATE campaign_stage_attempts SET status='unknown' WHERE id=?", attempt["id"]
    )  # a receipt exists: only an unknown attempt is resolved
    _insert_run_receipt(world, attempt, code)
    verdict = _assess(world, attempt["id"], codes=codes)
    assert (verdict.status, verdict.failure_class) == (status, klass)
    assert (
        verdict.evidence["exit_code"] == code and verdict.evidence["run_receipt_digest"] == "d" * 64
    )
    running = world.row("SELECT id FROM campaign_stage_attempts WHERE id=?", attempt["id"])
    world.env.execute(
        "UPDATE campaign_stage_attempts SET status='running' WHERE id=?", running["id"]
    )
    assert (
        _assess(world, attempt["id"], codes=codes) is None
    )  # a receipt exists and the attempt is not unknown: the normal flow's


def test_the_receipt_shows_exit_resolved_for_every_resolution_class(tmp_path):
    world, campaign = _world(tmp_path, retry={"max_attempts": 2, "transient_exit_codes": [3]})
    cid = campaign["id"]
    attempt = _exit_without_receipt(world, cid)
    world.env.execute(
        "UPDATE campaign_stage_attempts SET status='unknown' WHERE id=?", attempt["id"]
    )
    _insert_run_receipt(world, attempt, 9)  # undeclared: permanent, not exit_confirmed_unresolved
    world.runner.tick()
    resolved = world.attempts(cid)[0]
    assert (resolved["status"], resolved["failure_class"]) == (
        "failed_permanent",
        "undeclared_exit_code",
    )
    assert resolved["exit_code"] == 9 and resolved["run_receipt_digest"]
    with world.runner.store.runner_connection() as connection:
        campaign_row = connection.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        receipt, _ = build_receipt(connection, campaign_row, "failed")
    (entry,) = receipt["stages"][0]["attempts"]
    assert entry["exit_resolved"]["outcome"] == "failed_permanent"
    assert (
        entry["exit_resolved"]["is_evidence"] is False and entry["exit_resolved"]["evidence_digest"]
    )


def test_a_zero_hold_attempt_still_resolves_without_a_ledger_row(tmp_path):
    world, campaign = _world(tmp_path, retry={"max_attempts": 2})
    attempt = _exit_without_receipt(world, campaign["id"])
    gid = attempt["grant_id"]
    world.env.execute("UPDATE execution_project_limits SET held_cents = held_cents - 0")
    grant = world.row("SELECT * FROM execution_grants WHERE id=?", gid)
    assert grant["max_exposure_cents"] == 0
    world.env.execute("DELETE FROM execution_project_limits")
    _later(world, 10)
    world.runner.tick()
    assert (
        world.row("SELECT status FROM execution_grants WHERE id=?", gid)["status"] == "reconciled"
    )
    assert world.attempts(campaign["id"])[0]["failure_class"] == cer.EXIT_CONFIRMED_UNRESOLVED


def test_a_large_artifact_upload_begun_through_the_real_route_is_never_overtaken(
    tmp_path, monkeypatch
):
    """The worker reports ``exited`` first, then streams large outputs in chunks; control must
    not resolve the attempt while that upload (a ``snapshot_chunk_uploads`` row) is going."""
    limits_module.operator_limits.cache_clear()
    monkeypatch.setattr(
        limits_module, "LEGACY_INLINE_BYTES", 100
    )  # a tiny file takes the large path
    world, campaign = _world(tmp_path, retry={"max_attempts": 2})
    cid = campaign["id"]
    reach_control = (
        world.channel.post
    )  # the worker's reports are lost; this call is the route itself
    attempt = _exit_without_receipt(world, cid)
    gid, aid = attempt["grant_id"], attempt["id"]
    lease = world.row("SELECT * FROM execution_leases WHERE grant_id=?", gid)
    operation = world.row("SELECT operation_id FROM external_operations WHERE grant_id=?", gid)
    data = bytes(range(256)) * 2
    started = reach_control(
        f"/worker/leases/{lease['id']}/large-artifact-uploads",
        {
            "expected_epoch": lease["epoch"],
            "operation_id": operation["operation_id"],
            "relative_path": "data.json",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        },
        "begin-large",
    )
    assert started["status"] == "uploading"
    row = world.row("SELECT status FROM snapshot_chunk_uploads WHERE id=?", started["id"])
    assert row["status"] == "uploading"  # the real route wrote it, not artifact_uploads
    assert not world.row("SELECT count(*) AS n FROM artifact_uploads")["n"]
    _end_lease(world, gid)  # the lease lapsed while the worker was still uploading
    _later(world, 10)  # far past the grace, the grant still alive
    world.runner.tick()
    assert _assess(world, aid) is None
    assert world.row("SELECT status FROM execution_grants WHERE id=?", gid)["status"] == "active"
    assert not world.events_of("campaign.attempt_exit_resolved")
    # An expired upload no longer holds it back, and neither does a finished one.
    world.env.execute("UPDATE snapshot_chunk_uploads SET expires_at=?", PAST)
    assert _assess(world, aid) is not None
    world.env.execute(
        "UPDATE snapshot_chunk_uploads SET expires_at='2999-01-01T00:00:00Z', status='completed'"
    )
    assert _assess(world, aid) is not None
    limits_module.operator_limits.cache_clear()
