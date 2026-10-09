"""Default offline checks inject only external effects; never a live pilot."""
# ruff: noqa: E501

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_kubernetes_execution import _FakeKubernetes
from test_local_api_workflow import Journey, complete

from tessera_worker.comparison_pilot import (
    ComparisonPilot,
    PilotError,
    PilotJournal,
    PilotReadinessJournal,
)
from tessera_worker.control_channel import ControlChannelError
from tessera_worker.kubernetes_contracts import cluster_identity_digest
from tessera_worker.kubernetes_dispatch_service import (
    KubernetesDispatchWorkerConfig,
    KubernetesDispatchWorkerService,
)
from tessera_worker.kubernetes_execution import KubernetesOutputBinding


class Channel:
    def __init__(self, journey, worker=False):
        self.journey, self.worker = journey, worker

    def get(self, path):
        return self.journey.call("GET", path, worker=self.worker)

    def post(self, path, body, key):
        response = self.journey.client.post(
            path,
            json=body,
            headers={
                "Authorization": self.journey.worker if self.worker else self.journey.owner,
                "Idempotency-Key": key,
            },
        )
        if response.status_code >= 400:
            raise ControlChannelError("synthetic control rejection", response.status_code)
        return response.json()


def setup(tmp_path, monkeypatch, capsys, *, effect="succeeded"):
    original_call = Journey.call

    def bounded_plan(self, method, path, body=None, **kwargs):
        if method == "PUT" and path.endswith("/plan"):
            body = body | {"max_duration_seconds": 120}
        return original_call(self, method, path, body, **kwargs)

    monkeypatch.setattr(Journey, "call", bounded_plan)
    journey = Journey(tmp_path, monkeypatch, capsys)
    grants = {}
    for kind, task in journey.tasks.items():
        approved = journey.call(
            "POST",
            f"/tasks/{task['id']}/approve",
            {
                "expected_revision": task["revision"],
                "expected_plan_digest": task["current_plan_digest"],
            },
        )
        grants[kind] = journey.call(
            "POST",
            f"/projects/{journey.project['id']}/execution-grants",
            {
                "task_id": task["id"],
                "expected_task_revision": approved["revision"],
                "expected_plan_digest": task["current_plan_digest"],
                "worker_principal_id": journey.credential["principal_id"],
                "snapshot_id": journey.snapshot["id"],
                "pinned_image": "python@sha256:" + "b" * 64,
                "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                "max_exposure_cents": 5,
                "currency": "USD",
                "acknowledges_unmeasured_infrastructure": True,
            },
        )
    root = tmp_path / "pilot"
    root.mkdir(mode=0o700)
    journal = PilotJournal(root)
    owner, worker = Channel(journey), Channel(journey, True)
    output = KubernetesOutputBinding("outputs", "/outputs", "synthetic-pvc", "c" * 64)
    fakes = []

    def execute(dispatch, package):
        fake = _FakeKubernetes(package, journey.cluster)
        fakes.append(fake)
        original_create = fake.create

        def create(manifest):
            result = original_create(manifest)
            if effect in {"succeeded", "failed", "lost_reply"}:
                complete(fake, "failed" if effect == "failed" else "succeeded")
            if effect == "lost_reply":
                raise TimeoutError("synthetic lost create reply")
            return result

        fake.create = create
        if effect == "unknown":

            def delete(_name, _uid):
                raise TimeoutError("synthetic lost cancellation")

            fake.delete = delete
        adapter = fake.adapter(journey.worker_root / "kubernetes.sqlite3")
        service = KubernetesDispatchWorkerService(
            KubernetesDispatchWorkerConfig(
                "http://127.0.0.1:8765",
                credential_file=journey.credential_file,
                workspace_root=journey.worker_root,
                dispatch_id=dispatch["id"],
            ),
            journey.cluster,
            output,
            adapter,
            channel=worker,
        )
        if effect in {"cancelled", "unknown"}:
            service.run(max_cycles=1)
            grant = owner.get(f"/execution-grants/{package.package['grant_id']}")
            owner.post(
                f"/execution-grants/{grant['id']}/revoke",
                {"expected_revision": grant["revision"]},
                "pilot-stop",
            )
        return service.run(max_cycles=2)

    def collect(package, limit):
        assert limit == 1024
        content = b"1\n" if len(fakes) == 1 else b"2\n"
        return content, "synthetic-kubernetes-provider"

    pilot = ComparisonPilot(owner, worker, journey.cluster, output, journal, execute, collect)
    config = {
        "project_id": journey.project["id"],
        "experiment_id": journey.experiment["id"],
        "design_revision": 1,
        "target_name": "synthetic-remote",
        "grants": {kind: value["id"] for kind, value in grants.items()},
        "max_seconds_per_variant": 120,
        "max_output_bytes": 1024,
        "max_internal_reservation_cents_per_variant": 5,
        "cost_enforcement_digest": "d" * 64,
    }
    return journey, pilot, config, fakes, journal


def test_default_script_has_no_external_effects(capsys, monkeypatch):
    path = Path(__file__).parents[1] / "scripts/check_comparison_pilot.py"
    spec = importlib.util.spec_from_file_location("comparison_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "run", lambda _: pytest.fail("live entry point invoked"))
    assert module.main([]) == 0
    assert json.loads(capsys.readouterr().out)["live_ran"] is False
    assert module.main(["--run"]) == 1
    assert json.loads(capsys.readouterr().out)["blocker"] == "--config required"


def test_operator_attested_unknown_billing_never_becomes_a_zero_cost_cap(
    tmp_path, monkeypatch, capsys
):
    path = Path(__file__).parents[1] / "scripts/check_comparison_pilot.py"
    spec = importlib.util.spec_from_file_location("comparison_runner_cost", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    journey, _pilot, config, _fakes, journal = setup(tmp_path, monkeypatch, capsys)
    try:
        cost = {
            "version": 1,
            "target_digest": cluster_identity_digest(journey.cluster),
            "project_id": config["project_id"],
            "currency": "USD",
            "billing_state": "operator_attested_unknown",
            "max_provider_incremental_cost_cents": 0,
            "autoscaling_disabled": True,
            "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        }
        with pytest.raises(PilotError, match="live execution is blocked"):
            module.validate_cost_enforcement(cost, config, journey.cluster)
    finally:
        journal.close()


def test_readiness_gate_retains_blocked_reason_without_claiming_an_execution(tmp_path):
    root = tmp_path / "readiness"
    root.mkdir(mode=0o700)
    config = {"pilot": "synthetic"}
    journal = PilotReadinessJournal(root)
    try:
        journal.record(
            config, "isolation_and_credential_recheck", "blocked", "target attestation stale"
        )
    finally:
        journal.close()

    reopened = PilotReadinessJournal(root)
    try:
        assert reopened.db.execute(
            "SELECT stage,outcome,reason FROM readiness_events"
        ).fetchall() == [
            ("isolation_and_credential_recheck", "blocked", "target attestation stale")
        ]
        assert not (root / "pilot.sqlite3").exists()
    finally:
        reopened.close()


@pytest.mark.parametrize("effect", ["succeeded", "failed", "lost_reply", "cancelled", "unknown"])
def test_two_variants_use_ordinary_authority_and_retain_uncertainty(
    tmp_path,
    monkeypatch,
    capsys,
    effect,
):
    journey, pilot, config, fakes, journal = setup(tmp_path, monkeypatch, capsys, effect=effect)
    try:
        try:
            result = pilot.run(config)
        except PilotError as error:
            # Offline-only diagnostic; the live CLI never emits upstream bodies.
            raise error.__context__ or error from None
        assert result["evidence_kind"] == "synthetic"
        assert result["human_review"] == "pending"
        assert result["cost"] == result["usage"] == "unknown"
        assert result["model_provider_enabled"] is False
        assert len(fakes) == (1 if effect in {"cancelled", "unknown"} else 2)
        assert all(fake.creates == 1 for fake in fakes)
        exported = journey.export()
        assert len(exported["operations"]) == len(fakes)
        assert all(attempt["human_outcome"] is None for attempt in exported["admitted_attempts"])
        if effect in {"succeeded", "lost_reply"}:
            assert result["state"] == "completed"
            assert [item["artifact_status"] for item in result["variants"]] == ["quarantined"] * 2
            assert len({item["package_digest"] for item in result["variants"]}) == 2
        with pytest.raises(PilotError, match="no replay"):
            pilot.run(config)
        assert all(fake.creates == 1 for fake in fakes)
    finally:
        journal.close()


def test_output_limit_never_uploads_or_submits_intervention(tmp_path, monkeypatch, capsys):
    journey, pilot, config, fakes, journal = setup(tmp_path, monkeypatch, capsys)
    pilot.collect = lambda *_: (b"x" * 1025, "synthetic")
    try:
        with pytest.raises(PilotError, match="stopped"):
            pilot.run(config)
        assert len(fakes) == 1 and fakes[0].creates == 1
        events = [
            json.loads(row[0])
            for row in journal.db.execute(
                "SELECT payload_json FROM events WHERE kind='request_intent'"
            )
        ]
        assert not any(e["path"].endswith("/artifacts") for e in events)
        assert len(journey.export()["operations"]) == 1
    finally:
        journal.close()


@pytest.mark.parametrize("fence", ["revoked", "expired", "epoch", "package", "worker_scope"])
def test_authority_fences_before_any_external_effect(tmp_path, monkeypatch, capsys, fence):
    journey, pilot, config, fakes, journal = setup(tmp_path, monkeypatch, capsys)
    if fence == "revoked":
        grant = pilot.owner.get(f"/execution-grants/{config['grants']['baseline']}")
        pilot.owner.post(
            f"/execution-grants/{grant['id']}/revoke",
            {"expected_revision": grant["revision"]},
            "revoke-before-pilot",
        )
    elif fence == "expired":
        from tessera_control.execution import ExecutionStore

        monkeypatch.setattr(
            ExecutionStore, "_now", staticmethod(lambda: datetime.now(UTC) + timedelta(minutes=20))
        )
    elif fence == "worker_scope":
        # A real owner token cannot impersonate a scoped worker on worker routes.
        pilot.worker = Channel(journey, False)
    else:
        original = pilot.worker.post

        def tamper(path, body, key):
            if path.endswith("/package") and fence == "epoch":
                body = body | {"expected_epoch": body["expected_epoch"] + 1}
            result = original(path, body, key)
            if path.endswith("/package") and fence == "package":
                result["package"]["provider_enabled"] = True
            return result

        pilot.worker.post = tamper
    try:
        with pytest.raises(PilotError, match="stopped"):
            pilot.run(config)
        assert fakes == []
        assert not journey.export()["kubernetes_dispatches"]
    finally:
        journal.close()


def test_lost_control_admission_reply_is_not_retried(tmp_path, monkeypatch, capsys):
    journey, pilot, config, fakes, journal = setup(tmp_path, monkeypatch, capsys)
    original = pilot.owner.post

    def lose_reply(path, body, key):
        original(path, body, key)
        raise TimeoutError("synthetic reply lost after commit")

    pilot.owner.post = lose_reply
    try:
        with pytest.raises(PilotError, match="stopped"):
            pilot.run(config)
        assert len(journey.export()["admitted_attempts"]) == 1
        assert fakes == []
        assert journal.db.execute(
            "SELECT count(*) FROM events WHERE kind='reply_unknown'"
        ).fetchone() == (1,)
    finally:
        journal.close()


def test_grant_is_rechecked_before_admission_and_blocked_reason_is_durable(
    tmp_path, monkeypatch, capsys
):
    journey, pilot, config, fakes, journal = setup(tmp_path, monkeypatch, capsys)
    original_get = pilot.owner.get
    baseline_path = f"/execution-grants/{config['grants']['baseline']}"
    reads = 0

    def expired_after_preflight(path):
        nonlocal reads
        value = original_get(path)
        if path == baseline_path:
            reads += 1
            if reads == 2:
                return value | {
                    "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
                }
        return value

    pilot.owner.get = expired_after_preflight
    try:
        with pytest.raises(PilotError, match="stopped"):
            pilot.run(config)
        assert fakes == []
        blocked = [
            json.loads(row[0])
            for row in journal.db.execute("SELECT payload_json FROM events WHERE kind='blocked'")
        ]
        assert blocked == [
            {
                "reconciliation_required": True,
                "reason": "grant is no longer active for the registered variant",
                "stage": "orchestration",
            }
        ]
    finally:
        journal.close()
