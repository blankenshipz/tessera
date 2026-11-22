"""Synthetic Salad campaign setup: real stores and runner, no provider, no network.

The executor's provider-facing effects are test doubles. Where a test needs a claimed or
terminated route it appends the same rows the real flow writes (a ``salad_proxy.claim``
event, a provider-provenance route state, an uploaded manifest, a worker observation);
none of this proves a provider behavior, only how the control side reacts to the rows.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from campaign_fixture import Env

from tessera_control.artifact_manifests import ArtifactManifestStore
from tessera_control.artifacts import ArtifactStore
from tessera_control.campaign_models import SALAD_RUNTIME
from tessera_control.campaign_provider_salad import register_provider_instance
from tessera_control.campaign_runner import CampaignRunner
from tessera_control.interaction import InteractionStore
from tessera_control.research import ResearchStore
from tessera_control.runtime_packages import RuntimePackageStore
from tessera_control.salad_campaign_profiles import SaladCampaignProfiles
from tessera_control.store import canonical_digest

IMAGE = "ghcr.io/example/tessera-gpu@sha256:" + "1" * 64
GPU = "550e8400-e29b-41d4-a716-446655440000"
HOLD = 20
FINISHED = {"succeeded", "stopped", "failed", "cancelled"}


def profile_data(env: Env, **over: Any) -> dict[str, Any]:
    value = dict(
        name="gpu-main",
        org_id=env.owner["org_id"],
        project_id=env.project["id"],
        worker_principal_id=env.worker["id"],
        salad_organization="example-org",
        salad_project="tessera-research",
        country="US",
        gpu_class=GPU,
        cpu_millicores=1000,
        memory_mb=1024,
        disk_mb=1024,
        route_max_seconds=900,
        capability_environment_id="salad-gpu-campaign",
        artifact_destination="artifact-ref:quarantine",
        checkpoint_destination="checkpoint-ref:external",
        monitor_profile="a" * 64,
        watchdog_profile="b" * 64,
        custody_host="custody.example-tailnet.ts.net",
        credential_reference="credential-ref:salad-management",
        credential_version=1,
        allowed_image_digests=["sha256:" + "1" * 64],
        max_run_cents=100,
        max_task_cents=100,
        # Legacy scenarios exercise the human review stops; pre-approval tests opt in.
        preapprove_reviews=False,
    )
    value.update(over)
    return value


def profiles(env: Env, **over: Any) -> SaladCampaignProfiles:
    return SaladCampaignProfiles.model_validate(
        dict(schema="tessera.salad.campaign-profiles.v1", profiles=[profile_data(env, **over)])
    )


def contract_data(**over: Any) -> dict[str, Any]:
    value = dict(
        contract="tessera.salad.workload.v1",
        argv_template=["train", "--op", "{operation}", "--epoch", "{epoch}", "{custody_host}"],
        env={"OMP_NUM_THREADS": "4"},
        workload_seconds=20,
        outputs=[dict(name="output.json", max_bytes=4096)],
        egress=dict(
            kind="provider_broad",
            fetches=[dict(url="https://models.example.com/w.bin", sha256="c" * 64, max_bytes=1000)],
        ),
        custody_host="custody.example-tailnet.ts.net",
    )
    value.update(over)
    return value


def program_template() -> dict[str, Any]:
    text = "Operational text for one bounded stage run."
    return dict(
        outcome="gpu_stage_run",
        brief={
            name: text
            for name in (
                "objective",
                "hypothesis",
                "success_gate",
                "failure_gate",
                "stop_gate",
                "target_domain",
                "provenance_policy",
                "dataset_recommendation",
                "representation_recommendation",
                "split_recommendation",
                "metric_recommendation",
                "uncertainty",
            )
        },
        source=dict(
            uri="urn:tessera:workload:contract-v1",
            quoted_passage="Reviewed generic workload contract.",
            content_digest="d" * 64,
            locator="workload-contract.json",
        ),
        gate=dict(
            category="experimental_ground_truth",
            metric="reference_match",
            unit="fraction",
            procedure="Platform-derived fraction of outputs matching the reference.",
            comparison="eq",
            threshold=1.0,
            statistic="mean",
            minimum_replicates=2,
            minimum_samples=1,
        ),
    )


def salad_stage(env: Env, key: str = "g1", /, *, hold: int = HOLD, attempts: int = 1, **over: Any):
    contract = contract_data()
    value = {
        "key": key,
        "backend": "salad-gpu-v1",
        "image": IMAGE,
        "argv": ["/tessera/bin/run", *contract["argv_template"]],
        "output_paths": ["output.json"],
        "max_output_bytes": 4096,
        "max_duration_seconds": 3000,
        "max_exposure_cents": hold,
        "retry": {"max_attempts": attempts},
        "inputs": [
            {
                "kind": "snapshot",
                "name": "code",
                "snapshot_id": env.snapshot_id,
                "digest": env.snapshot_digest,
                "data_class": "public",
            }
        ],
        "salad": dict(
            profile="gpu-main",
            worker_principal_id=env.worker["id"],
            workload=contract,
            program=program_template(),
            grant_ttl_seconds=21600,
        ),
    }
    value.update(over)
    return value


def cpu_stage(env: Env, key: str, /, *, hold: int = 6, **over: Any) -> dict[str, Any]:
    value = {
        "key": key,
        "backend": "docker-local-cpu-v1",
        "image": "python@sha256:" + "a" * 64,
        "argv": ["python", "-c", "print(1)"],
        "output_paths": ["data.json"],
        "max_output_bytes": 1024,
        "max_duration_seconds": 60,
        "max_exposure_cents": hold,
        "inputs": [
            {
                "kind": "snapshot",
                "name": "code",
                "snapshot_id": env.snapshot_id,
                "digest": env.snapshot_digest,
                "data_class": "public",
            }
        ],
    }
    value.update(over)
    return value


def salad_spec(env: Env, stages: list[dict[str, Any]], **over: Any) -> dict[str, Any]:
    spec = {
        "version": 1,
        "name": "salad campaign",
        "budget_cap_cents": 50,
        "deadline_seconds": 86400,
        "worker_principal_id": env.worker["id"],
        "stages": stages,
    }
    spec.update(over)
    return spec


def salad_stage_v2(env: Env, key: str = "g1", /, **over: Any) -> dict[str, Any]:
    """The same stage as ``salad_stage`` in the v2 shape: the worker lives only in
    ``provider_config`` (neither the stage nor the provider descriptor names it)."""
    v1 = salad_stage(env, key)
    value = {
        "key": key,
        "provider": "salad",
        "runtime": SALAD_RUNTIME,
        "provider_config": v1["salad"],
        "image": v1["image"],
        "argv": v1["argv"],
        "max_duration_seconds": v1["max_duration_seconds"],
        "max_exposure_cents": v1["max_exposure_cents"],
        "retry": v1["retry"],
        "inputs": v1["inputs"],
        "outputs": [{"name": "output", "path": "output.json", "max_bytes": 4096}],
    }
    value.update(over)
    return value


def salad_spec_v2(env: Env, stages: list[dict[str, Any]], **over: Any) -> dict[str, Any]:
    spec = {"version": 2, "name": "salad v2", "budget_cap_cents": 50, "deadline_seconds": 86400}
    spec |= {"stages": stages}
    spec.update(over)
    return spec


@pytest.fixture(autouse=True)
def salad_provider_reset():
    """The ``salad`` provider is process-wide: restore the profile-less default after a test.

    Import this fixture into any test module that builds a ``SaladWorld``."""
    yield
    register_provider_instance(None)


class SaladWorld:
    """An approved Salad campaign, a runner, and a test-driven executor and human."""

    def __init__(
        self, tmp_path: Path, *, stages=None, spec_over: dict | None = None, **profile_over: Any
    ) -> None:
        from tessera_control.campaign_store import CampaignStore

        self.env = Env(tmp_path)
        self.env.execute("UPDATE snapshot_releases SET status='revoked'")
        self.profiles = profiles(self.env, **profile_over)
        self.env.store = CampaignStore(self.env.state)
        # The provider owns the operator profiles (``--campaign-providers``); submit and
        # approve reach them through it.
        register_provider_instance(self.profiles)
        self.runner = CampaignRunner(self.env.state)
        self.human = self.env.human()
        self.worker = self.env.worker
        built = stages(self.env) if callable(stages) else stages
        self.spec = salad_spec(self.env, built or [salad_stage(self.env)], **(spec_over or {}))
        self.counter = 0
        self.campaign: dict[str, Any] | None = None

    # -- plumbing ---------------------------------------------------------------------

    @property
    def driver(self):
        """The Salad stage driver the registered provider holds for this runner."""
        from tessera_control.campaign_providers import get_provider

        return get_provider("salad").driver(self.runner)

    def start(self) -> dict[str, Any]:
        submitted = self.env.submit(spec=self.spec)
        self.campaign = self.env.approve(submitted)
        return self.campaign

    @property
    def campaign_id(self) -> str:
        assert self.campaign is not None
        return self.campaign["id"]

    def key(self, name: str) -> str:
        self.counter += 1
        return f"{name}-{self.counter}"

    def row(self, sql: str, *args: Any):
        rows = self.env.query(sql, *args)
        return rows[0] if rows else None

    def status(self) -> str:
        return self.row("SELECT status FROM campaigns WHERE id=?", self.campaign_id)["status"]

    def salad(self, stage_key: str = "g1", attempt: int | None = None):
        sql = (
            "SELECT sa.*, a.stage_key, a.attempt, a.grant_id, a.admitted_attempt_id "
            "FROM campaign_salad_attempts sa JOIN campaign_stage_attempts a ON a.id=sa.attempt_id "
            "WHERE a.campaign_id=? AND a.stage_key=?"
        )
        rows = self.env.query(
            sql + (" AND a.attempt=?" if attempt else "") + " ORDER BY a.attempt DESC",
            *([self.campaign_id, stage_key] + ([attempt] if attempt else [])),
        )
        return rows[0] if rows else None

    def attempt(self, stage_key: str = "g1"):
        return self.row(
            "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND stage_key=? "
            "ORDER BY attempt DESC",
            self.campaign_id,
            stage_key,
        )

    def phase(self, stage_key: str = "g1") -> str | None:
        found = self.salad(stage_key)
        return None if found is None else found["phase"]

    def tick(self, count: int = 1) -> None:
        for _ in range(count):
            self.runner.tick()

    def tick_until(self, predicate, limit: int = 60) -> int:
        for count in range(1, limit + 1):
            self.runner.tick()
            if predicate():
                return count
        raise AssertionError("condition not reached\n" + self.dump())

    def tick_until_phase(self, phase: str, stage_key: str = "g1", limit: int = 60) -> None:
        self.tick_until(lambda: self.phase(stage_key) == phase, limit)

    def dump(self) -> str:
        lines = [f"campaign={self.status()}"]
        for row in self.env.query(
            "SELECT stage_key, status, prep_phase FROM campaign_stages WHERE campaign_id=?",
            self.campaign_id,
        ):
            lines.append(f"stage {row['stage_key']} {row['status']} {row['prep_phase']}")
        for row in self.env.query(
            "SELECT stage_key, attempt, status, failure_class, failure_detail "
            "FROM campaign_stage_attempts WHERE campaign_id=?",
            self.campaign_id,
        ):
            lines.append(f"attempt {tuple(row)}")
        for row in self.env.query(
            "SELECT stage_key, step, status, error_class FROM campaign_step_intents "
            "WHERE campaign_id=? ORDER BY created_at",
            self.campaign_id,
        ):
            lines.append(f"intent {tuple(row)}")
        return "\n".join(lines)

    def events(self, event_type: str) -> list[dict[str, Any]]:
        return [
            json.loads(row["payload_json"])
            for row in self.env.query(
                "SELECT payload_json FROM events WHERE event_type=? ORDER BY cursor", event_type
            )
        ]

    # -- the human ----------------------------------------------------------------------

    def assess_source(self, stage_key: str = "g1", verdict: str = "supported") -> None:
        source_id = self.row(
            "SELECT source_id FROM campaign_salad_stages WHERE campaign_id=? AND stage_key=?",
            self.campaign_id,
            stage_key,
        )["source_id"]
        body = {"expected_revision": 1, "verdict": verdict, "reason": "reviewed by the owner"}
        ResearchStore(self.env.state).assess_source(
            self.human, source_id, self.key("assess"), canonical_digest(body), body
        )

    def resolve_decisions(self, decision: str = "approve") -> None:
        store = InteractionStore(self.env.plain)
        row = self.salad()
        for decision_id in (row["plan_decision_id"], row["source_decision_id"]):
            current = self.row("SELECT revision FROM decision_requests WHERE id=?", decision_id)
            store.resolve_decision(
                self.human,
                self.key("resolve"),
                decision_id,
                current["revision"],
                decision,
                "Owner review of the bounded stage run; no scientific claim",
            )

    def record_charge(self, cents: int, stage_key: str = "g1", delivery: str | None = None) -> None:
        stage = self.row(
            "SELECT experiment_id FROM campaign_stages WHERE campaign_id=? AND stage_key=?",
            self.campaign_id,
            stage_key,
        )
        budget = self.row(
            "SELECT revision FROM experiment_budgets WHERE experiment_id=?",
            stage["experiment_id"],
        )
        admitted = self.salad(stage_key)["admitted_attempt_id"]
        body = {
            "expected_revision": budget["revision"],
            "delivery_id": delivery or self.key("delivery"),
            "evidence_digest": "e" * 64,
            "source_reference": "provider invoice, entered by the owner",
            "category": "compute",
            "admitted_attempt_id": admitted,
            "amount_cents": cents,
        }
        from tessera_control.experiments import ExperimentStore

        ExperimentStore(self.env.state).budget_command(
            self.human, stage["experiment_id"], self.key("charge"), body, charge=True
        )

    # -- the executor (test double) ------------------------------------------------------

    def lease_and_package(self, stage_key: str = "g1") -> dict[str, Any]:
        """Lease and package exactly as the executor does, then record the package."""
        from tessera_control.execution import ExecutionStore

        work = [
            w for w in self.env.store.list_salad_work(self.worker) if w["phase"] == "awaiting_lease"
        ]
        assert work, "no Salad grant is awaiting a lease\n" + self.dump()
        item = work[0]
        execution = ExecutionStore(self.env.state)
        _, lease = execution.acquire_execution_lease(
            self.worker, item["grant_id"], self.key("lease"), "lease"
        )
        body = {
            "expected_epoch": lease["epoch"],
            "admitted_attempt_id": item["admitted_attempt_id"],
            "execution_adapter": "kubernetes-job",
        }
        _, response = RuntimePackageStore(self.env.state).execution_package(
            self.worker,
            lease["id"],
            self.key("package"),
            canonical_digest(body),
            lease["epoch"],
            admitted_attempt_id=item["admitted_attempt_id"],
            execution_adapter="kubernetes-job",
        )
        package = response["package"]
        self.env.store.record_salad_package(
            self.worker,
            item["attempt_id"],
            self.key("record-package"),
            dict(
                operation_id=package["operation_id"],
                lease_id=lease["id"],
                epoch=lease["epoch"],
                package_digest=response["package_digest"],
            ),
        )
        self.lease = lease
        return response

    def route_state(self) -> dict[str, Any]:
        row = self.salad()
        found = self.row(
            "SELECT payload_json FROM events WHERE event_type='remote_execution.state' "
            "AND json_extract(payload_json,'$.id')=? ORDER BY cursor DESC",
            row["route_id"],
        )
        return json.loads(found["payload_json"])

    def _append(self, event_type: str, payload: dict[str, Any], revision: int = 1) -> None:
        state = self.row("SELECT project_id, org_id FROM campaigns WHERE id=?", self.campaign_id)
        with self.env.store.runner_connection() as connection:
            self.env.store._event(
                connection,
                {"id": self.human["id"], "org_id": state["org_id"]},
                state["project_id"],
                None,
                event_type,
                revision,
                payload,
            )

    def claim(self, action: str = "create") -> None:
        row = self.salad()
        state = self.route_state()
        self._append(
            "salad_proxy.claim",
            dict(
                nonce=hashlib.sha256(f"{state['id']}{action}".encode()).hexdigest(),
                route_id=state["id"],
                route_digest=state["proposal_digest"],
                operation_id=row["operation_id"],
                attempt_id=state["proposal"]["attempt_id"],
                lease_id=row["lease_id"],
                epoch=row["epoch"],
                package_digest=row["package_digest"],
                action=action,
                preflight_digest="f" * 64,
            ),
        )

    def terminate_route(self, *, reallocated: bool = False, provenance: str = "provider") -> None:
        """Write the route state a confirmed provider stop leaves (test double)."""
        state = self.route_state()
        state.update(
            termination_confirmed=True,
            reallocated=reallocated,
            provider_status="stopped",
            state="stopped",
            receipts=[{"provenance": provenance, "observed_at": datetime.now(UTC).isoformat()}],
        )
        state["revision"] += 1
        self._append("remote_execution.state", state, state["revision"])

    def upload_manifest(self, content: bytes = b'{"score": 1}') -> dict[str, Any]:
        row = self.salad()
        payload = {
            "expected_epoch": row["epoch"],
            "operation_id": row["operation_id"],
            "relative_path": "output.json",
            "sha256": hashlib.sha256(content).hexdigest(),
            "content_base64": base64.b64encode(content).decode(),
        }
        _, artifact = ArtifactStore(self.env.state).upload_artifact(
            self.worker, row["lease_id"], self.key("artifact"), canonical_digest(payload), payload
        )
        body = {
            "expected_epoch": row["epoch"],
            "operation_id": row["operation_id"],
            "artifacts": [
                {key: artifact[key] for key in ("id", "relative_path", "sha256", "size")}
            ],
        }
        _, manifest = ArtifactManifestStore(self.env.state).create_manifest(
            self.worker, row["lease_id"], self.key("manifest"), canonical_digest(body), body
        )
        return manifest

    def observe_exit(self) -> None:
        from tessera_control.execution import ExecutionStore

        row = self.salad()
        body = {
            "expected_epoch": row["epoch"],
            "operation_id": row["operation_id"],
            "observed_state": "exited",
            "observed_at": datetime.now(UTC).isoformat(),
            "provider_id": "provider-group-1",
            "termination_confirmed": True,
        }
        ExecutionStore(self.env.state).report_execution_observation(
            self.worker, row["lease_id"], self.key("observe"), canonical_digest(body), body
        )

    def run_to_outcome(
        self,
        *,
        passed: bool = True,
        outputs: bool = True,
        guardian_final: dict[str, Any] | None = None,
    ) -> None:
        """Executor double for a route that ran: claim, stop, import, observe, record."""
        row = self.salad()
        self.env.store.mark_salad_executing(self.worker, row["attempt_id"], self.key("executing"))
        self.claim("create")
        self.claim("start")
        manifest = self.upload_manifest() if outputs else None
        self.terminate_route()
        self.observe_exit()
        content = b'{"score": 1}'
        receipt: dict[str, Any] = {
            "schema": "tessera.salad.live-check.v1",
            "status": "passed" if passed else "inconclusive",
            "reason": "verified_journey" if passed else "journey_incomplete",
            "stop_confirmed": True,
            "outputs": [
                {
                    "name": "output.json",
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "bytes": len(content),
                }
            ],
        }
        self.env.store.record_salad_outcome(
            self.worker,
            row["attempt_id"],
            self.key("outcome"),
            dict(
                route_id=row["route_id"],
                live_receipt=receipt,
                manifest_id=None if manifest is None else manifest["id"],
                provider_id="provider-group-1",
                estimated_exposure={"estimated_exposure_cents": 7},
                **({} if guardian_final is None else {"guardian_final": guardian_final}),
            ),
        )

    # -- scenario drivers ------------------------------------------------------------------

    def to_awaiting_review(self) -> None:
        self.start()
        self.tick_until(lambda: self.row("SELECT 1 FROM campaign_salad_stages") is not None)
        self.tick_until(
            lambda: (
                self.row("SELECT source_id FROM campaign_salad_stages WHERE source_id IS NOT NULL")
                is not None
            )
        )
        self.assess_source()
        self.tick_until_phase("awaiting_lease")
        self.lease_and_package()
        self.tick_until_phase("awaiting_review")

    def to_route_started(self) -> None:
        self.to_awaiting_review()
        self.resolve_decisions()
        self.tick_until_phase("route_started")

    def to_awaiting_charge(self) -> None:
        self.to_route_started()
        self.run_to_outcome()
        self.tick_until_phase("awaiting_charge")

    def cancel(self) -> None:
        from tessera_control.store import canonical_digest as digest

        current = self.row("SELECT revision FROM campaigns WHERE id=?", self.campaign_id)
        body = {"expected_revision": current["revision"], "reason": "operator cancel"}
        status, _ = self.env.store.cancel_campaign(
            self.human, self.campaign_id, self.key("cancel"), digest(body), body
        )
        assert status == 200

    def grant_expiry_in_past(self) -> None:
        past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        self.env.execute(
            "UPDATE execution_grants SET expires_at=? WHERE id=?",
            past,
            self.attempt()["grant_id"],
        )
