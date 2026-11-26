"""A Salad completion shutdown cancel never blocks a verified execution success (ADR 0075).

The live-check driver always stops its route when it ends. After a clean run it records that
stop at control through its own reduce-only worker cancel with ``cancel_cause.class ==
"execution_complete"``, which also fences the grant and its operation (``cancel_requested``).
That stop is the run's normal end, not a request to abandon it: a verified pass still succeeds
and indexes its outputs. Every other cancel still refuses an execution success, and attempts the
old refusal failed are recovered by ``reconcile_completion_cancels`` (idempotent, audited, no
charge). Real stores and runner; the executor and the human are test doubles; no provider.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from types import SimpleNamespace

import pytest
from salad_campaign_fixture import FINISHED, SaladWorld, salad_provider_reset  # noqa: F401

from tessera_control import campaign_completion_cancel as completion
from tessera_control.campaign_io import resolve_artifact
from tessera_control.store import Conflict, Forbidden, _connect, canonical_digest

CONTENT = b'{"score": 1}'
SHA = hashlib.sha256(CONTENT).hexdigest()
REFUSED = "Execution success cannot override a requested cancellation."


# -- scenario helpers --------------------------------------------------------------------------


def driver_cancel(world: SaladWorld) -> None:
    """The live-check driver's own reduce-only route cancel (``SaladLiveSession.cancel``)."""
    from tessera_control.remote_execution import RemoteExecutionStore

    state = world.route_state()
    RemoteExecutionStore(world.env.state).worker_cancel(
        world.worker,
        state["id"],
        "salad-live-cancel-" + state["id"],
        expected_revision=state["revision"],
        proposal_digest=state["proposal_digest"],
    )


def human_grant_cancel(world: SaladWorld) -> None:
    """A person cancels the attempt's grant directly (an operator cancel, not the driver)."""
    from tessera_control.execution import ExecutionStore

    grant = world.row(
        "SELECT id, revision FROM execution_grants WHERE id=?", world.attempt()["grant_id"]
    )
    body = {"expected_revision": grant["revision"]}
    ExecutionStore(world.env.state).revoke_execution_grant(
        world.human,
        grant["id"],
        world.key("grant-cancel"),
        canonical_digest(body),
        grant["revision"],
    )


def run_with_cancel(world: SaladWorld, cause, *, before=None) -> None:
    """Route started, then the executor double runs it to a verified pass whose receipt names
    ``cause``; ``before`` runs just before the driver's own cancel (another actor's cancel)."""
    world.to_route_started()
    original = world.terminate_route

    def terminate(**kwargs):
        if before is not None:
            before(world)
        driver_cancel(world)
        original(**kwargs)

    world.terminate_route = terminate
    extra = {"cancel_recorded": True}
    if cause is not None:
        extra["cancel_cause"] = (
            cause
            if isinstance(cause, dict)
            else {"class": cause, "exception": None, "guardian_refusal": None}
        )
    world.run_to_outcome(receipt_extra=extra)


def settle(world: SaladWorld, cents: int = 1) -> None:
    world.tick_until(lambda: world.phase() == "awaiting_charge" or world.status() in FINISHED)
    if world.status() not in FINISHED:
        world.record_charge(cents)
    world.tick_until(lambda: world.status() in FINISHED)


def output_rows(world: SaladWorld) -> list:
    return world.env.query(
        "SELECT * FROM campaign_stage_io WHERE campaign_id=? AND direction='output'",
        world.campaign_id,
    )


def resolve_pin(world: SaladWorld, name: str, sha: str):
    """What a later campaign's ``kind=artifact`` input of this exact sha256 resolves to."""
    campaign = world.row("SELECT org_id, project_id FROM campaigns WHERE id=?", world.campaign_id)
    source = SimpleNamespace(
        campaign_id=world.campaign_id,
        stage="g1",
        output=name,
        sha256=sha,
        name="pre",
        data_class=None,
    )
    connection = _connect(world.env.state / "tessera.sqlite3")
    try:
        return resolve_artifact(connection, dict(campaign), source)
    finally:
        connection.close()


def historical(world: SaladWorld, monkeypatch, cause="execution_complete", *, before=None):
    """A campaign the old guard failed: every cancel fence refused the success."""
    with monkeypatch.context() as patch:
        patch.setattr(completion, "completion_evidence", lambda *_: (None, "old_guard"))
        run_with_cancel(world, cause, before=before)
        world.tick_until(lambda: world.attempt()["status"] == "failed_permanent")
        settle(world)
    assert world.status() == "failed", world.dump()
    attempt = world.attempt()
    assert (attempt["status"], attempt["failure_class"]) == ("failed_permanent", "step_refused")
    assert attempt["failure_detail"] == REFUSED
    assert output_rows(world) == []


def recover(world: SaladWorld, key: str = "recover-1", **over):
    body = {"reason": "completion shutdown recorded as success", "dry_run": False, **over}
    return world.env.store.reconcile_completion_cancels(
        world.human, world.campaign_id, key, canonical_digest(body), body
    )


def recovery_events(world: SaladWorld) -> list[dict]:
    return world.events(completion.RECOVERED_EVENT)


# -- (a) the forward path ----------------------------------------------------------------------


def test_completion_cancel_succeeds_and_writes_stage_io(tmp_path):
    world = SaladWorld(tmp_path)
    run_with_cancel(world, "execution_complete")
    operation = world.row(
        "SELECT desired_state FROM external_operations WHERE operation_id=?",
        world.salad()["operation_id"],
    )
    assert operation["desired_state"] == "cancel_requested"  # the driver's own fence
    settle(world, 1)
    assert world.status() == "succeeded", world.dump()
    attempt = world.attempt()
    assert attempt["status"] == "succeeded" and attempt["failure_class"] is None
    admitted = world.row(
        "SELECT outcome FROM research_experiment_admitted_attempts WHERE id=?",
        attempt["admitted_attempt_id"],
    )
    assert admitted["outcome"] == "succeeded"
    rows = output_rows(world)
    assert [(r["sha256"], r["size_bytes"], r["attempt_id"]) for r in rows] == [
        (SHA, len(CONTENT), attempt["id"])
    ]
    assert attempt["settled_cents"] == 1
    receipt = json.loads(
        world.row(
            "SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", world.campaign_id
        )["receipt_json"]
    )
    assert receipt["status"] == "succeeded"
    resolved = resolve_pin(world, rows[0]["name"], SHA)
    assert resolved.sha256 == SHA and resolved.source_attempt_id == attempt["id"]
    assert recovery_events(world) == []  # the forward path needs no recovery addendum


def test_completion_evidence_names_the_driver_cancel(tmp_path):
    world = SaladWorld(tmp_path)
    run_with_cancel(world, "execution_complete")
    # Custody is released (and retained) when the runner collects, before it resolves.
    world.tick_until(lambda: world.attempt()["status"] not in {"exited", "collected"})
    connection = _connect(world.env.state / "tessera.sqlite3")
    try:
        evidence, refusal = completion.completion_evidence(
            connection, world.salad()["operation_id"]
        )
    finally:
        connection.close()
    assert refusal == "" and evidence is not None
    assert evidence["cancel_cause_class"] == "execution_complete"
    assert evidence["cancel_authority"] == "executor_worker"
    assert [o["sha256"] for o in evidence["outputs"]] == [SHA]


# -- (b) a real cancel still refuses ------------------------------------------------------------


@pytest.mark.parametrize(
    "cause",
    [
        "deadline",
        "budget",
        "guardian_stop_cause",
        "control_refused",
        "prestart",
        "error",
        "execution_complete_but_not",
        "EXECUTION_COMPLETE",
        None,
        {"class": ["execution_complete"]},
        {"kind": "execution_complete"},
    ],
)
def test_a_real_cancel_still_refuses_execution_success(tmp_path, cause):
    world = SaladWorld(tmp_path)
    run_with_cancel(world, cause)
    world.tick_until(lambda: world.attempt()["status"] == "failed_permanent")
    attempt = world.attempt()
    assert (attempt["status"], attempt["failure_class"]) == ("failed_permanent", "step_refused")
    assert attempt["failure_detail"] == REFUSED
    admitted = world.row(
        "SELECT outcome FROM research_experiment_admitted_attempts WHERE id=?",
        attempt["admitted_attempt_id"],
    )
    assert admitted["outcome"] is None
    assert output_rows(world) == []


def test_an_operator_grant_cancel_before_the_driver_still_refuses(tmp_path):
    """The receipt says execution_complete, but a person fenced the grant first: the run was
    cancelled by someone else, so its success cannot override that cancel."""
    world = SaladWorld(tmp_path)
    run_with_cancel(world, "execution_complete", before=human_grant_cancel)
    world.tick_until(lambda: world.attempt()["status"] in {"failed_permanent", "cancelled"})
    attempt = world.attempt()
    assert attempt["status"] != "succeeded"
    assert output_rows(world) == []
    connection = _connect(world.env.state / "tessera.sqlite3")
    try:
        evidence, refusal = completion.completion_evidence(
            connection, world.salad()["operation_id"]
        )
    finally:
        connection.close()
    assert evidence is None and refusal == "cancel_not_driver_completion"


def test_a_passed_receipt_without_a_verified_journey_is_refused(tmp_path):
    world = SaladWorld(tmp_path)
    run_with_cancel(world, "execution_complete")
    world.tick_until(lambda: world.attempt()["status"] not in {"exited", "collected"})
    connection = _connect(world.env.state / "tessera.sqlite3")
    row = world.salad()
    receipt = json.loads(row["live_receipt_json"])
    receipt["reason"] = "journey_incomplete"
    world.env.execute(
        "UPDATE campaign_salad_attempts SET live_receipt_json=? WHERE attempt_id=?",
        json.dumps(receipt),
        row["attempt_id"],
    )
    try:
        evidence, refusal = completion.completion_evidence(connection, row["operation_id"])
    finally:
        connection.close()
    assert evidence is None and refusal == "receipt_not_verified_pass"


# -- (c) the recovery command -------------------------------------------------------------------


def test_recovery_restores_a_historical_completion_cancel_and_is_idempotent(tmp_path, monkeypatch):
    world = SaladWorld(tmp_path)
    historical(world, monkeypatch)
    before = world.row("SELECT * FROM campaigns WHERE id=?", world.campaign_id)
    receipt_before = world.row(
        "SELECT receipt_digest FROM campaign_receipts WHERE campaign_id=?", world.campaign_id
    )["receipt_digest"]
    settled_before = world.attempt()["settled_cents"]
    budget_before = world.env.query("SELECT * FROM experiment_budget_entries")
    with pytest.raises(Conflict):
        resolve_pin(world, "output.json", SHA)

    status, dry = world.env.store.reconcile_completion_cancels(
        world.human,
        world.campaign_id,
        "dry-1",
        canonical_digest({"reason": "look", "dry_run": True}),
        {"reason": "look", "dry_run": True},
    )
    assert status == 200 and dry["dry_run"] is True
    assert [a["verdict"] for a in dry["attempts"]] == ["recoverable"]
    assert world.status() == "failed" and output_rows(world) == []

    status, result = recover(world)
    assert status == 200, result
    assert result["campaign_status"] == "succeeded"
    assert result["source_receipt_digest"] == receipt_before
    attempt = world.attempt()
    assert attempt["status"] == "succeeded" and attempt["failure_class"] is None
    assert attempt["settled_cents"] == settled_before  # the recorded charge stays as it was
    assert world.env.query("SELECT * FROM experiment_budget_entries") == budget_before
    campaign = world.row("SELECT * FROM campaigns WHERE id=?", world.campaign_id)
    assert campaign["status"] == "succeeded" and campaign["stop_reason"] is None
    assert campaign["settled_cents"] == before["settled_cents"]
    assert campaign["held_cents"] == before["held_cents"]
    stage = world.row(
        "SELECT status FROM campaign_stages WHERE campaign_id=? AND stage_key='g1'",
        world.campaign_id,
    )
    assert stage["status"] == "succeeded"
    admitted = world.row(
        "SELECT outcome FROM research_experiment_admitted_attempts WHERE id=?",
        attempt["admitted_attempt_id"],
    )
    assert admitted["outcome"] == "succeeded"
    rows = output_rows(world)
    assert [(r["sha256"], r["attempt_id"]) for r in rows] == [(SHA, attempt["id"])]
    # The immutable terminal receipt is untouched; the sealed addendum lists the output.
    assert (
        world.row(
            "SELECT receipt_digest FROM campaign_receipts WHERE campaign_id=?", world.campaign_id
        )["receipt_digest"]
        == receipt_before
    )
    events = recovery_events(world)
    assert len(events) == 1
    audit = events[0]
    assert audit["campaign_status_before"] == "failed"
    assert audit["campaign_status_after"] == "succeeded"
    recovered = audit["attempts"][0]
    assert recovered["status_before"] == "failed_permanent"
    assert recovered["evidence"]["cancel_cause_class"] == "execution_complete"
    assert [(o["name"], o["sha256"]) for o in recovered["io_outputs"]] == [(rows[0]["name"], SHA)]
    resolved = resolve_pin(world, rows[0]["name"], SHA)
    assert resolved.sha256 == SHA and resolved.source_attempt_id == attempt["id"]

    # A replay of the same key, and a fresh run, are no-op successes.
    assert recover(world)[0] == 200
    status, again = recover(world, key="recover-2")
    assert status == 200 and again["recovered"] == []
    assert [a["verdict"] for a in again["attempts"]] == ["already_recovered"]
    assert len(output_rows(world)) == 1 and len(recovery_events(world)) == 1
    assert (
        world.row("SELECT revision FROM campaigns WHERE id=?", world.campaign_id)["revision"]
        == campaign["revision"]
    )


def test_recovery_refuses_a_historical_real_cancel(tmp_path, monkeypatch):
    world = SaladWorld(tmp_path)
    historical(world, monkeypatch, cause="deadline")
    with pytest.raises(Conflict, match="cancel_not_completion"):
        recover(world)
    assert world.status() == "failed" and output_rows(world) == []
    assert world.attempt()["status"] == "failed_permanent"
    assert recovery_events(world) == []


def test_recovery_refuses_an_operator_cancel_with_a_completion_receipt(tmp_path, monkeypatch):
    world = SaladWorld(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(completion, "completion_evidence", lambda *_: (None, "old_guard"))
        run_with_cancel(world, "execution_complete", before=human_grant_cancel)
        world.tick_until(lambda: world.status() in FINISHED or world.phase() == "awaiting_charge")
        if world.status() not in FINISHED:
            world.record_charge(1)
        world.tick_until(lambda: world.status() in FINISHED)
    with pytest.raises(Conflict):
        recover(world)
    assert output_rows(world) == [] and recovery_events(world) == []


def test_recovery_refuses_custody_bytes_that_no_longer_match(tmp_path, monkeypatch):
    world = SaladWorld(tmp_path)
    historical(world, monkeypatch)
    artifact = json.loads(
        world.row(
            "SELECT artifacts_json FROM artifact_manifests WHERE id=?",
            world.attempt()["manifest_id"],
        )["artifacts_json"]
    )[0]
    world.env.execute("UPDATE artifacts SET content=? WHERE id=?", b'{"score": 2}', artifact["id"])
    with pytest.raises(Conflict, match="custody_digest_mismatch"):
        recover(world)
    assert world.status() == "failed" and output_rows(world) == []


def test_recovery_needs_the_owner_credential(tmp_path, monkeypatch):
    world = SaladWorld(tmp_path)
    historical(world, monkeypatch)
    body = {"reason": "x", "dry_run": False}
    with pytest.raises(Forbidden):
        world.env.store.reconcile_completion_cancels(
            world.worker, world.campaign_id, "k-worker", canonical_digest(body), body
        )
    assert world.status() == "failed" and output_rows(world) == []


def test_cli_posts_one_keyed_recovery_per_campaign(capsys):
    from tessera_control.cli import _reconcile_completion_cancels

    calls = []

    def transport(method, path, body, key):
        calls.append((method, path, body, key))
        return 200, {"ok": True}

    arguments = argparse.Namespace(
        campaign_id=["campaign_a", "campaign_b", "campaign_a"],
        reason="r",
        dry_run=False,
    )
    assert _reconcile_completion_cancels(arguments, transport, "k") == 0
    assert calls == [
        (
            "POST",
            "/campaigns/campaign_a/reconcile-completion-cancels",
            {"reason": "r", "dry_run": False},
            "k-campaign_a",
        ),
        (
            "POST",
            "/campaigns/campaign_b/reconcile-completion-cancels",
            {"reason": "r", "dry_run": False},
            "k-campaign_b",
        ),
    ]
    calls.clear()
    arguments.dry_run = True
    assert _reconcile_completion_cancels(arguments, transport, None) == 0
    assert [c[:2] for c in calls] == [
        ("GET", "/campaigns/campaign_a/completion-cancels"),
        ("GET", "/campaigns/campaign_b/completion-cancels"),
    ]
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert [r["campaign_id"] for r in printed["results"]] == ["campaign_a", "campaign_b"]


def test_cli_parser_accepts_the_recovery_verb():
    from tessera_control.cli import parser

    parsed = parser().parse_args(
        [
            "campaign",
            "reconcile-completion-cancels",
            "--owner-header",
            "owner.json",
            "--campaign-id",
            "campaign_a",
            "--campaign-id",
            "campaign_b",
            "--dry-run",
        ]
    )
    assert parsed.campaign_action == "reconcile-completion-cancels"
    assert parsed.campaign_id == ["campaign_a", "campaign_b"] and parsed.dry_run is True
