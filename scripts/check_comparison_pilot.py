"""Opt-in, retained two-variant remote Kubernetes compute pilot (no model calls)."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from tessera_worker.comparison_pilot import (
    ComparisonPilot,
    PilotControlChannel,
    PilotError,
    PilotJournal,
    PilotReadinessJournal,
    validate_config,
)
from tessera_worker.comparison_pilot_remote import (
    PilotRemoteExecutionAdapter,
    RemoteKubernetes,
    bounded_file,
    load_target,
    validate_output_storage_evidence,
)
from tessera_worker.kubernetes_contracts import cluster_identity_digest
from tessera_worker.kubernetes_dispatch_service import (
    KubernetesDispatchWorkerConfig,
    KubernetesDispatchWorkerService,
)
from tessera_worker.service import read_private_credential


def validate_cost_enforcement(cost, pilot, cluster):
    """Accept only a provider-enforced cap; grants never stand in for billing."""
    expected_cost = {
        "version",
        "target_digest",
        "project_id",
        "currency",
        "billing_state",
        "max_provider_incremental_cost_cents",
        "autoscaling_disabled",
        "expires_at",
    }
    if (
        not isinstance(cost, dict)
        or set(cost) != expected_cost
        or (
            cost.get("target_digest") != cluster_identity_digest(cluster)
            or cost.get("project_id") != pilot["project_id"]
            or cost.get("currency") != "USD"
            or cost.get("autoscaling_disabled") is not True
        )
    ):
        raise PilotError("target-bound provider billing evidence is malformed")
    if cost["billing_state"] == "operator_attested_unknown":
        raise PilotError("provider billing is operator-attested unknown; live execution is blocked")
    try:
        expires_at = datetime.fromisoformat(cost["expires_at"])
    except (TypeError, ValueError) as error:
        raise PilotError("provider billing evidence expiry is malformed") from error
    if (
        cost["billing_state"] != "provider_enforced_incremental_cap_v1"
        or type(cost["max_provider_incremental_cost_cents"]) is not int
        or cost["max_provider_incremental_cost_cents"] != 0
        or expires_at.tzinfo is None
        or expires_at <= datetime.now(UTC)
    ):
        raise PilotError("current provider-enforced no-incremental-charge evidence required")
    return cost


def run(config_path: Path):
    config = json.loads(bounded_file(config_path, 64 * 1024))
    if not isinstance(config, dict) or not isinstance(config.get("runtime"), dict):
        raise PilotError("configuration requires an explicit runtime section")
    root_value = config["runtime"].get("journal_root")
    if not isinstance(root_value, str):
        raise PilotError("configuration requires an explicit readiness journal root")
    readiness = PilotReadinessJournal(Path(root_value))
    stage = "configuration"
    try:
        if set(config) != {"pilot", "target", "runtime"}:
            raise PilotError("configuration requires pilot, target, runtime sections")
        pilot, runtime = config["pilot"], config["runtime"]
        validate_config(pilot)
        cluster, output = load_target(config["target"])
        expected = {
            "control_url",
            "owner_credential_file",
            "worker_credential_file",
            "compute_credential_file",
            "ca_file",
            "journal_root",
            "worker_root",
            "cost_enforcement_file",
            "output_storage_enforcement_file",
        }
        if set(runtime) != expected:
            raise PilotError("runtime reference fields do not match the pilot contract")
        root, worker_root = Path(runtime["journal_root"]), Path(runtime["worker_root"])
        # Do not create directories, enroll credentials, approve plans, or provision
        # a target on the operator's behalf. Both roots must already be private.
        if (
            not worker_root.is_absolute()
            or worker_root.is_symlink()
            or not worker_root.is_dir()
            or worker_root.stat().st_mode & 0o077
        ):
            raise PilotError("worker root must be an existing absolute 0700 directory")
        readiness.record(config, stage, "passed", "static_configuration_valid")

        stage = "provider_cost_evidence"
        cost_bytes = bounded_file(Path(runtime["cost_enforcement_file"]), 16384)
        if hashlib.sha256(cost_bytes).hexdigest() != pilot["cost_enforcement_digest"]:
            raise PilotError("cost enforcement evidence digest mismatch")
        cost = json.loads(cost_bytes)
        validate_cost_enforcement(cost, pilot, cluster)
        readiness.record(config, stage, "passed", "current_target_bound_cost_evidence")

        stage = "output_storage_attestation"
        output_storage_bytes = bounded_file(Path(runtime["output_storage_enforcement_file"]), 16384)
        output_storage = validate_output_storage_evidence(
            json.loads(output_storage_bytes),
            cluster,
            output,
            pilot["project_id"],
            pilot["max_output_bytes"],
        )
        readiness.record(config, stage, "passed", "current_target_bound_storage_attestation")

        journal = PilotJournal(root)
        owner = PilotControlChannel(
            runtime["control_url"], read_private_credential(Path(runtime["owner_credential_file"]))
        )
        worker_file = Path(runtime["worker_credential_file"])
        worker = PilotControlChannel(runtime["control_url"], read_private_credential(worker_file))
        remote = RemoteKubernetes(
            cluster,
            output,
            Path(runtime["ca_file"]),
            Path(runtime["compute_credential_file"]),
            journal,
            config["target"]["credential_scope"],
            datetime.fromisoformat(cost["expires_at"]),
            output_storage,
            isolation=config["target"]["isolation"],
        )
        stage = "isolation_and_credential_recheck"
        remote.verify_target(submission=True)
        readiness.record(config, stage, "passed", "fresh_target_bound_isolation_and_credential")
        adapter = PilotRemoteExecutionAdapter(worker_root / "kubernetes.sqlite3", remote)

        def execute(dispatch, _package):
            return KubernetesDispatchWorkerService(
                KubernetesDispatchWorkerConfig(
                    runtime["control_url"],
                    credential_file=worker_file,
                    workspace_root=worker_root,
                    dispatch_id=dispatch["id"],
                ),
                cluster,
                output,
                adapter,
                channel=worker,
                control_outage_seconds=10,
            ).run()

        result = ComparisonPilot(
            owner,
            worker,
            cluster,
            output,
            journal,
            execute,
            lambda package, limit: remote.collect(adapter, package, limit),
        ).run(pilot, live=True)
        readiness.record(config, "orchestration", "passed", "two_variants_completed")
        return result
    except PilotError as error:
        readiness.record(config, stage, "blocked", str(error))
        raise
    except Exception:
        readiness.record(config, stage, "blocked", "malformed_or_unavailable_readiness_evidence")
        raise
    finally:
        if "journal" in locals():
            journal.close()
        readiness.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    if not args.run:
        print(
            json.dumps({"state": "not_started", "live_ran": False, "model_provider_enabled": False})
        )
        return 0
    if args.config is None:
        print(json.dumps({"state": "blocked", "live_ran": False, "blocker": "--config required"}))
        return 1
    try:
        result = run(args.config)
    except Exception:
        # Never emit upstream exception bodies, credential paths, argv or config.
        result = {
            "state": "blocked_or_unknown",
            "reconciliation_required": True,
            "message": "Inspect private pilot and worker journals; do not retry submission.",
        }
    print(json.dumps(result, sort_keys=True))
    return 0 if result["state"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
