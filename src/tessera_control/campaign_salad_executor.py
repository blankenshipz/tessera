"""Operator-run executor for Salad GPU campaign stages (ADR 0038 amendment).

Runs on the trusted Linux server with the trusted-server worker credential. It leases and
packages admitted Salad grants, keeps leases alive, runs each started route through the
unchanged ``salad_live_check --run`` subprocess, imports sealed custody, uploads the verified
files through the worker artifact/manifest stores, reports the provider-confirmed exit and
records its outcome. It never approves, starts, settles, resolves a decision or records a
charge, and it never calls create/start outside the live driver.

    python -m tessera_control.campaign_salad_executor --config FILE [--once] [--health-file PATH]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .salad_campaign_profiles import (
    DATA_POLICIES,
    DATA_POLICY_PUBLIC,
    DATA_POLICY_TRUST,
    SaladCampaignProfile,
    derive_selection,
    load_profiles,
    proxy_policy,
)
from .salad_workload_contract import WorkloadContractV1
from .store import canonical_digest

CONFIG_SCHEMA = "tessera.salad.campaign-executor.v1"
RECEIPT_NAME = "salad-live-receipt.json"
LIVE_RECEIPT_SCHEMA = "tessera.salad.live-check.v1"
SUBPROCESS_GRACE_SECONDS = 300
KEEPALIVE_PHASES = ("leased", "route_started", "executing")
_IDENT = re.compile(r"[a-zA-Z0-9_-]{1,200}\Z")
_Path = Annotated[str, StringConstraints(min_length=1, max_length=4096)]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class CustodyConfig(_Closed):
    host: Annotated[str, StringConstraints(pattern=r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")]
    port: Annotated[int, Field(ge=443, le=443)]
    token_file: _Path
    ca_file: _Path
    certificate_sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class ExecutorConfig(_Closed):
    schema_id: Annotated[str, Field(alias="schema")]
    state_dir: _Path
    profiles_file: _Path
    profile: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
    worker_credential_file: _Path
    credential_directory: _Path
    guardian_socket: _Path
    guardian_uid: Annotated[int, Field(ge=0, le=2**31 - 1)]
    custody: CustodyConfig
    work_directory: _Path
    source_revision: Annotated[
        str, StringConstraints(pattern=r"^[a-f0-9]{40}(?:\+sha256:[a-f0-9]{64})?$")
    ]
    poll_seconds: Annotated[int, Field(ge=2, le=30)]
    # The guardian may not have reached ``stopped`` right after the exit: re-read a few
    # times, a short fixed delay apart, before the runner falls back to a human charge.
    guardian_retries: Annotated[int, Field(ge=0, le=10)] = 3
    guardian_retry_seconds: Annotated[int, Field(ge=0, le=30)] = 2

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)


def load_config(path: Path | str) -> ExecutorConfig:
    """Closed, owner-only (0600) absolute config file; every referenced path is checked."""
    from .salad_live_check import _private_bytes, _private_path
    from .salad_workload_jwt import strict_json

    try:
        config = ExecutorConfig.model_validate(strict_json(_private_bytes(path, 65536)))
        if config.schema_id != CONFIG_SCHEMA:
            raise ValueError("schema")
        for directory in (config.state_dir, config.credential_directory, config.work_directory):
            os.close(_private_path(directory, directory=True))
        for file in (
            config.worker_credential_file,
            config.custody.token_file,
            config.custody.ca_file,
            config.profiles_file,
        ):
            os.close(_private_path(file))
        socket_path = Path(config.guardian_socket)
        if not socket_path.is_absolute() or ".." in socket_path.parts:
            raise ValueError("guardian socket")
    except (OSError, ValueError, ValidationError):
        raise ValueError(
            f"Executor config must be an owner-only absolute {CONFIG_SCHEMA} JSON file "
            "whose referenced files and directories are private."
        ) from None
    return config


class ControlPort(Protocol):
    """Everything the executor does to control, as the worker. Tests inject a fake."""

    def work(self) -> list[dict[str, Any]]: ...
    def pending_grants(self) -> set[str]: ...
    def plan(self, grant_id: str) -> dict[str, int]: ...
    def acquire_lease(self, grant_id: str, key: str) -> dict[str, Any]: ...
    def package(
        self, lease_id: str, epoch: int, admitted_attempt_id: str, key: str
    ) -> dict[str, Any]: ...
    def record_package(self, attempt_id: str, payload: dict[str, Any]) -> None: ...
    def operation(self, operation_id: str) -> dict[str, Any] | None: ...
    def heartbeat(self) -> None: ...
    def renew(self, lease_id: str, epoch: int) -> None: ...
    def route(self, route_id: str) -> dict[str, Any]: ...
    def mark_executing(self, attempt_id: str) -> None: ...
    def upload_outputs(
        self, lease_id: str, epoch: int, operation_id: str, directory: Path, files: list[dict]
    ) -> dict[str, Any]: ...
    def report_exited(
        self,
        lease_id: str,
        epoch: int,
        operation_id: str,
        provider_id: str,
        observed_at: str,
    ) -> None: ...
    def record_outcome(self, attempt_id: str, payload: dict[str, Any]) -> None: ...
    def guardian_final(self, route_id: str) -> dict[str, Any] | None: ...


RouteRunner = Callable[[Path, Path, int], "dict[str, Any] | None"]
Importer = Callable[[str, Path], "list[dict[str, Any]]"]


class RouteTimeout(Exception):
    """The live-check subprocess outlived its bound; no outcome is recorded."""


def subprocess_route_runner(config_path: Path, receipt_dir: Path, timeout: int):
    """Run the unchanged live driver in a fresh interpreter; return its receipt, if any."""
    try:
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "tessera_control.salad_live_check",
                "--run",
                "--server-config",
                str(config_path),
                "--receipt-directory",
                str(receipt_dir),
            ],
            timeout=timeout,
            capture_output=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise RouteTimeout from None
    return read_receipt(receipt_dir)


def read_receipt(receipt_dir: Path) -> dict[str, Any] | None:
    from .salad_live_check import _private_bytes

    try:
        value = json.loads(_private_bytes(Path(receipt_dir) / RECEIPT_NAME, 65536))
    except (OSError, ValueError):
        return None
    if type(value) is not dict or value.get("schema") != LIVE_RECEIPT_SCHEMA:
        return None
    return value


def _key(step: str, attempt_id: str) -> str:
    return f"salad-exec-{step}-{hashlib.sha256(attempt_id.encode()).hexdigest()[:40]}"


class SaladCampaignExecutor:
    def __init__(
        self,
        config: ExecutorConfig,
        profile: SaladCampaignProfile,
        control: ControlPort,
        *,
        worker_principal_id: str,
        route_runner: RouteRunner = subprocess_route_runner,
        importer: Importer,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.sleep = sleep
        if profile.worker_principal_id != worker_principal_id:
            raise ValueError("The profile belongs to a different worker principal.")
        self.config, self.profile, self.control = config, profile, control
        self.route_runner, self.importer = route_runner, importer
        self.work_directory = Path(config.work_directory)
        self._leases: dict[str, tuple[str, int]] = {}
        # Attempts whose lease renewal was refused: never retried past the fence.
        self.fenced: dict[str, str] = {}
        self.log: list[dict[str, Any]] = []

    # -- one pass ---------------------------------------------------------------------------

    def run_once(self) -> list[dict[str, Any]]:
        start = len(self.log)
        try:
            items = self.control.work()
        except Exception as error:
            self._note("work_unavailable", None, type(error).__name__)
            return self.log[start:]
        pending: set[str] | None = None
        for item in items:
            attempt = item["attempt_id"]
            try:
                if item["phase"] == "awaiting_lease":
                    if pending is None:
                        pending = self.control.pending_grants()
                    self._lease_and_package(item, pending)
                elif item["phase"] == "route_started":
                    self._keepalive(item)
                    self._execute(item)
                elif item["phase"] == "executing":
                    self._resume(item)
                elif item["phase"] == "leased":
                    self._keepalive(item)
            except Exception as error:
                # Class name only: messages can carry identifiers from provider paths.
                self._note("error", attempt, type(error).__name__)
        return self.log[start:]

    def _note(self, action: str, attempt_id: str | None, detail: str = "") -> None:
        self.log.append({"action": action, "attempt_id": attempt_id, "detail": detail})

    # -- steps 2 and 3: lease, package, keepalive -------------------------------------------

    def _lease_and_package(self, item: dict[str, Any], pending: set[str]) -> None:
        attempt, grant = item["attempt_id"], item["grant_id"]
        if grant not in pending and attempt not in self._leases:
            self._note("not_pending", attempt)
            return
        if item["profile"] != self.profile.name:
            self._note("refused_profile", attempt, "different_profile")
            return
        plan = self.control.plan(grant)
        if not 1 <= plan["budget_cents"] <= self.profile.max_task_cents:
            self._note("refused_budget", attempt, "plan_budget_above_profile")
            return
        if attempt not in self._leases:
            lease = self.control.acquire_lease(grant, _key("lease", attempt))
            self._leases[attempt] = (lease["id"], lease["epoch"])
        lease_id, epoch = self._leases[attempt]
        response = self.control.package(
            lease_id, epoch, item["admitted_attempt_id"], _key("package", attempt)
        )
        package = response["package"]
        self.control.record_package(
            attempt,
            dict(
                operation_id=package["operation_id"],
                lease_id=lease_id,
                epoch=epoch,
                package_digest=response["package_digest"],
            ),
        )
        self._note("packaged", attempt, package["operation_id"])

    def _lease_for(self, item: dict[str, Any]) -> tuple[str, int] | None:
        attempt = item["attempt_id"]
        if attempt not in self._leases and item.get("operation_id"):
            operation = self.control.operation(item["operation_id"])
            if operation is not None:
                self._leases[attempt] = (operation["lease_id"], operation["epoch"])
        return self._leases.get(attempt)

    def _keepalive(self, item: dict[str, Any]) -> None:
        """Registry heartbeat and lease renewal; a refusal is recorded and never retried."""
        attempt = item["attempt_id"]
        if attempt in self.fenced:
            return
        lease = self._lease_for(item)
        if lease is None:
            return
        try:
            self.control.heartbeat()
            self.control.renew(*lease)
        except Exception as error:
            self.fenced[attempt] = type(error).__name__
            self._note("lease_renewal_refused", attempt, type(error).__name__)

    # -- steps 4 to 6: run a started route --------------------------------------------------

    def _route_dir(self, route_id: str) -> Path:
        if not _IDENT.fullmatch(route_id):
            raise ValueError("exact route id required")
        return self.work_directory / route_id

    def _verified_selection(self, item: dict[str, Any], state: dict[str, Any]):
        """Re-derive the exact selection from the operator profile; refuse on any mismatch."""
        proposal = state["proposal"]
        selection = proposal["selection"]
        execution = selection["salad"]["execution"]
        if (
            execution.get("argv_binding") != "workload_v1"
            or (state["org_id"], state["project_id"])
            != (self.profile.org_id, self.profile.project_id)
            or proposal["operation_id"] != item["operation_id"]
            or not state.get("approved_by")
            or state["desired"] == "stop"
        ):
            return None
        data_policy = execution.get("data_policy", DATA_POLICY_PUBLIC)
        if data_policy not in DATA_POLICIES:
            return None
        # Derived from the admission evidence the control plane recorded, not from the
        # route's own literal: input that was not admitted as public-only never runs under
        # the public literal (ADR 0040 section 4).
        admission = state.get("input_admission")
        if (
            isinstance(admission, dict)
            and admission.get("admission_basis") != "public_only"
            and data_policy != DATA_POLICY_TRUST
        ):
            return None
        plan = self.control.plan(item["grant_id"])
        contract = WorkloadContractV1.model_validate(execution["workload"])
        derived = derive_selection(
            self.profile,
            contract,
            operation_id=proposal["operation_id"],
            epoch=proposal["epoch"],
            snapshot_digest=selection["manifests"]["data"],
            image=selection["manifests"]["image"],
            max_cost_cents=execution["max_cost_cents"],
            plan_max_seconds=plan["max_duration_seconds"],
            # The runner derived the same value from the stage's inputs; the control plane
            # admitted the route under the provider trust setting (ADR 0040 §4).
            data_policy=data_policy,
        )
        if derived.model_dump(mode="json") != selection:
            return None
        if not 1 <= plan["budget_cents"] <= self.profile.max_task_cents:
            return None
        return derived, plan

    def _write_live_config(self, route_id, state, derived, plan, directory: Path) -> Path:
        from .salad_live_check import MAX_POLLS

        policy = proxy_policy(self.profile, derived)
        policy_json = policy.model_dump(mode="json")
        policy_json["management"] = policy.management.model_dump(mode="json")
        max_seconds = derived.resources.max_seconds
        config = dict(
            schema="tessera.salad.live-config.v1",
            state_dir=self.config.state_dir,
            route_id=route_id,
            task_id=state["task_id"],
            org_id=state["org_id"],
            project_id=state["project_id"],
            proposal_digest=state["proposal_digest"],
            policy=policy_json,
            worker_credential_file=self.config.worker_credential_file,
            credential_directory=self.config.credential_directory,
            guardian_socket=self.config.guardian_socket,
            guardian_uid=self.config.guardian_uid,
            custody=self.config.custody.model_dump(mode="json"),
            approved_max_cost_cents=plan["budget_cents"],
            max_seconds=max_seconds,
            max_polls=min(MAX_POLLS, max(30, -(-max_seconds // 5))),
            source_revision=self.config.source_revision,
        )
        path = directory / "live-config.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(json.dumps(config, sort_keys=True).encode())
        return path

    def _execute(self, item: dict[str, Any]) -> None:
        attempt, route_id = item["attempt_id"], item["route_id"]
        if attempt in self.fenced:
            self._note("fenced_no_run", attempt, self.fenced[attempt])
            return
        state = self.control.route(route_id)
        if any(effect["action"] in {"create", "start"} for effect in state["effects"]):
            # A route with any effect is never re-run; the runner's silent-executor rule applies.
            self._note("refused_route_has_effects", attempt)
            return
        verified = self._verified_selection(item, state)
        if verified is None:
            self._note("refused_selection_mismatch", attempt)
            return
        derived, plan = verified
        directory = self._route_dir(route_id)
        if directory.exists():
            # A directory means a prior pass already launched (or tried to): never re-run.
            self._note("refused_route_already_attempted", attempt)
            return
        try:
            self.control.mark_executing(attempt)
        except Exception as error:
            self._note("mark_executing_refused", attempt, type(error).__name__)
            return
        directory.mkdir(mode=0o700)
        receipts = directory / "receipts"
        receipts.mkdir(mode=0o700)
        (directory / "outputs").mkdir(mode=0o700)
        config_path = self._write_live_config(route_id, state, derived, plan, directory)
        try:
            receipt = self.route_runner(
                config_path,
                receipts,
                derived.resources.max_seconds + SUBPROCESS_GRACE_SECONDS,
            )
        except RouteTimeout:
            self._note("route_timeout", attempt)
            return
        finally:
            config_path.unlink(missing_ok=True)
        if receipt is None:
            self._note("no_receipt", attempt)
            return
        self._finish(item, receipt)

    def _resume(self, item: dict[str, Any]) -> None:
        """An `executing` attempt after a restart: finish only from a receipt on disk."""
        self._keepalive(item)
        receipts = self._route_dir(item["route_id"]) / "receipts"
        receipt = read_receipt(receipts) if receipts.is_dir() else None
        if receipt is not None:
            self._finish(item, receipt)

    # -- step 5: custody import, upload, observation, outcome -------------------------------

    def _finish(self, item: dict[str, Any], receipt: dict[str, Any]) -> None:
        attempt, route_id = item["attempt_id"], item["route_id"]
        manifest_id = None
        provider_id = None
        state = self.control.route(route_id)
        provider_id = state.get("provider_id")
        lease = self._lease_for(item)
        if receipt.get("stop_confirmed") is True and lease is not None:
            try:
                manifest_id = self._import_and_upload(item, state, lease)
            except Exception as error:
                self._note("import_failed", attempt, type(error).__name__)
        if (
            lease is not None
            and provider_id
            and state.get("termination_confirmed") is True
            and not state.get("reallocated")
        ):
            observed_at = _last_provider_observation(state)
            if observed_at is not None:
                try:
                    self.control.report_exited(
                        lease[0], lease[1], item["operation_id"], provider_id, observed_at
                    )
                except Exception as error:
                    self._note("observation_failed", attempt, type(error).__name__)
        financial = receipt.get("financial")
        guardian_final = self._read_guardian_final(attempt, route_id)
        self.control.record_outcome(
            attempt,
            dict(
                route_id=route_id,
                live_receipt=receipt,
                manifest_id=manifest_id,
                provider_id=provider_id,
                estimated_exposure=financial if isinstance(financial, dict) else None,
                guardian_final=guardian_final,
            ),
        )
        self._note("outcome_recorded", attempt, "manifest" if manifest_id else "no_manifest")

    def _read_guardian_final(self, attempt: str, route_id: str) -> dict[str, Any] | None:
        """The guardian's final status, re-read while it is missing or not yet ``stopped``.

        None (the runner then waits for a human charge) is logged with the error class, so
        a miswired status client is visible rather than silently disabling the bound."""
        final: dict[str, Any] | None = None
        reason = "unprojectable"
        for index in range(1 + self.config.guardian_retries):
            failed = False
            try:
                final = self.control.guardian_final(route_id)
            except Exception as error:
                final, reason, failed = None, type(error).__name__, True
            if final is not None and final.get("status") == "stopped":
                return final
            if final is None and not failed:
                # No guardian is configured (or nothing projectable): re-reading cannot help.
                break
            if index < self.config.guardian_retries:
                self.sleep(self.config.guardian_retry_seconds)
        if final is None:
            self._note("guardian_final_unavailable", attempt, reason)
        else:
            self._note("guardian_not_stopped", attempt, str(final.get("status"))[:40])
        return final

    def _import_and_upload(self, item, state, lease) -> str:
        directory = self._route_dir(item["route_id"]) / "outputs"
        files = self.importer(item["route_id"], directory)
        for entry in files:
            data = (directory / entry["name"]).read_bytes()
            if hashlib.sha256(data).hexdigest() != entry["sha256"] or len(data) != entry["bytes"]:
                raise ValueError("imported file differs from its manifest entry")
        manifest = self.control.upload_outputs(
            lease[0], lease[1], item["operation_id"], directory, files
        )
        return manifest["id"]


def _last_provider_observation(state: dict[str, Any]) -> str | None:
    provider = [r for r in state.get("receipts", []) if r.get("provenance") == "provider"]
    return provider[-1].get("observed_at") if provider else None


# -- real composition (trusted server only; not exercised by default tests) ------------------


class StoreControl:
    """ControlPort over the control stores, acting as the worker. No HTTP, no new authority."""

    def __init__(
        self,
        state_dir: Path | str,
        worker: dict[str, Any],
        guardian_status: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        from .artifact_manifests import ArtifactManifestStore
        from .campaign_store import CampaignStore
        from .execution import ExecutionStore
        from .remote_execution import RemoteExecutionStore
        from .runtime_packages import RuntimePackageStore
        from .worker_grants import WorkerGrantStore
        from .workers import WorkerRegistryStore

        self.state_dir, self.worker = Path(state_dir), worker
        self.guardian_status = guardian_status
        self.campaigns = CampaignStore(self.state_dir)
        self.grants = WorkerGrantStore(self.state_dir)
        self.executions = ExecutionStore(self.state_dir)
        self.packages = RuntimePackageStore(self.state_dir)
        self.registry = WorkerRegistryStore(self.state_dir)
        self.routes = RemoteExecutionStore(self.state_dir)
        self.artifacts = ArtifactManifestStore(self.state_dir)

    def _read(self, sql: str, args: tuple) -> list[Any]:
        from .store import _connect

        connection = _connect(self.executions.database_path)
        try:
            return connection.execute(sql, args).fetchall()
        finally:
            connection.close()

    def work(self):
        return self.campaigns.list_salad_work(self.worker)

    def pending_grants(self):
        return {g["id"] for g in self.grants.list_pending(self.worker)["grants"]}

    def plan(self, grant_id):
        rows = self._read(
            """SELECT r.budget_cents, r.max_duration_seconds FROM execution_grants g
            JOIN projects p ON p.id = g.project_id
            JOIN task_revisions r ON r.task_id = g.task_id AND r.plan_digest = g.plan_digest
            AND r.revision < g.task_revision
            WHERE g.id = ? AND g.worker_principal_id = ? AND p.org_id = ?
            ORDER BY r.revision DESC LIMIT 1""",
            (grant_id, self.worker["id"], self.worker["org_id"]),
        )
        if not rows:
            raise LookupError("plan unavailable")
        return {k: rows[0][k] for k in ("budget_cents", "max_duration_seconds")}

    def acquire_lease(self, grant_id, key):
        return self.executions.acquire_execution_lease(
            self.worker, grant_id, key, canonical_digest({"grant_id": grant_id})
        )[1]

    def package(self, lease_id, epoch, admitted_attempt_id, key):
        payload = dict(
            expected_epoch=epoch,
            admitted_attempt_id=admitted_attempt_id,
            execution_adapter="kubernetes-job",
        )
        return self.packages.execution_package(
            self.worker,
            lease_id,
            key,
            canonical_digest(payload),
            epoch,
            False,
            None,
            admitted_attempt_id,
            False,
            "kubernetes-job",
        )[1]

    def record_package(self, attempt_id, payload):
        self.campaigns.record_salad_package(
            self.worker, attempt_id, _key("record-package", attempt_id), payload
        )

    def operation(self, operation_id):
        rows = self._read(
            "SELECT lease_id, epoch, grant_id FROM external_operations "
            "WHERE operation_id = ? AND org_id = ? AND worker_principal_id = ?",
            (operation_id, self.worker["org_id"], self.worker["id"]),
        )
        return dict(rows[0]) if rows else None

    def heartbeat(self):
        import uuid

        self.registry.heartbeat(
            self.worker, "salad-exec-worker-" + uuid.uuid4().hex, canonical_digest({}), {}
        )

    def renew(self, lease_id, epoch):
        import uuid

        payload = {"lease_id": lease_id, "expected_epoch": epoch}
        self.executions.renew_execution_lease(
            self.worker,
            lease_id,
            "salad-exec-renew-" + uuid.uuid4().hex,
            canonical_digest(payload),
            epoch,
        )

    def route(self, route_id):
        return self.routes.get(self.worker, route_id)

    def mark_executing(self, attempt_id):
        self.campaigns.mark_salad_executing(
            self.worker, attempt_id, _key("mark-executing", attempt_id)
        )

    def upload_outputs(self, lease_id, epoch, operation_id, directory, files):
        entries = []
        for entry in files:
            data = (Path(directory) / entry["name"]).read_bytes()
            payload = dict(
                expected_epoch=epoch,
                operation_id=operation_id,
                relative_path=entry["name"],
                sha256=entry["sha256"],
                content_base64=base64.b64encode(data).decode("ascii"),
            )
            key = _key("upload-" + entry["name"], operation_id)
            _, uploaded = self.artifacts.upload_artifact(
                self.worker, lease_id, key, canonical_digest(payload), payload
            )
            entries.append(
                dict(
                    id=uploaded["id"],
                    relative_path=entry["name"],
                    sha256=entry["sha256"],
                    size=len(data),
                )
            )
        payload = dict(expected_epoch=epoch, operation_id=operation_id, artifacts=entries)
        return self.artifacts.create_manifest(
            self.worker,
            lease_id,
            _key("manifest", operation_id),
            canonical_digest(payload),
            payload,
        )[1]

    def report_exited(self, lease_id, epoch, operation_id, provider_id, observed_at):
        payload = dict(
            expected_epoch=epoch,
            operation_id=operation_id,
            observed_state="exited",
            observed_at=observed_at,
            provider_id=provider_id,
            termination_confirmed=True,
        )
        self.executions.report_execution_observation(
            self.worker,
            lease_id,
            _key("observe-exited", operation_id),
            canonical_digest(payload),
            payload,
        )

    def record_outcome(self, attempt_id, payload):
        self.campaigns.record_salad_outcome(
            self.worker, attempt_id, _key("outcome", attempt_id), payload
        )

    def guardian_final(self, route_id):
        """The final guardian status projection, or None when it cannot be recorded.

        A failed read raises; the executor logs the error class and may retry."""
        if self.guardian_status is None:
            return None
        return project_guardian_final(self.guardian_status(self.routes.get(self.worker, route_id)))


_GUARDIAN_FLAGS = (
    "orphan",
    "allocation_uncertain",
    "escalation_pending",
    "budget_fenced",
    "rate_unknown",
)


def _text(value: Any) -> str | None:
    """A decimal-text field: strings, integers and finite JSON floats (``3.5`` -> ``"3.5"``)."""
    if type(value) in (str, int):
        return str(value)
    if type(value) is float and math.isfinite(value):
        return format(Decimal(repr(value)), "f")
    return None


def project_guardian_final(
    status: dict[str, Any], *, now: datetime | None = None
) -> dict[str, Any] | None:
    """Only the keys the control plane records; anything missing leaves the flag unclean."""
    if type(status) is not dict:
        return None
    from .campaign_store import _GuardianFinal

    billed = status.get("actual_billed_cost_cents")
    projected = dict(
        status=status.get("status"),
        reason=str(status.get("reason") or "")[:80],
        **{flag: status.get(flag) is not False for flag in _GUARDIAN_FLAGS},
        published_hourly_rate_usd=_text(status.get("published_hourly_rate_usd")),
        estimated_exposure_cents=_text(status.get("estimated_exposure_cents")),
        actual_billed_cost_cents=billed if type(billed) is int and billed >= 0 else None,
        observed_at=(now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    try:
        _GuardianFinal.model_validate(projected)
    except ValidationError:
        return None  # an unrecordable status must not lose the outcome
    return projected


def trusted_importer(config: ExecutorConfig, profile: SaladCampaignProfile, worker, state_dir):
    """Importer over a TrustedSaladServer using the independent custody HTTPS client."""

    def import_route(route_id: str, destination: Path) -> list[dict[str, Any]]:
        from .remote_execution import RemoteExecutionStore
        from .salad_live_check import _private_bytes
        from .salad_secret_files import PrivateFileSecretResolver
        from .salad_server import SaladSafetyHTTPSClient, TrustedSaladServer

        route = RemoteExecutionStore(state_dir).get(worker, route_id)
        selection = route["proposal"]["selection"]
        from .remote_execution_models import ExecutionSelection

        policy = proxy_policy(profile, ExecutionSelection.model_validate(selection))
        _private_bytes(config.worker_credential_file, 4096)
        resolver = PrivateFileSecretResolver(config.credential_directory)
        try:
            server = TrustedSaladServer(
                state_dir=state_dir,
                worker=worker,
                policy=policy,
                resolver=resolver,
                safety=SaladSafetyHTTPSClient(**config.custody.model_dump(mode="json")),
            )
            reply = server.import_retained(route_id, destination=str(destination))
        finally:
            resolver.close()
        return reply["files"]

    return import_route


def build_executor(config: ExecutorConfig) -> SaladCampaignExecutor:
    from .salad_live_check import _private_bytes
    from .store import Store

    profile = load_profiles(config.profiles_file).get(config.profile)
    if profile is None:
        raise ValueError("Unknown Salad campaign profile.")
    if config.custody.host != profile.custody_host:
        raise ValueError("Custody host differs from the profile.")
    state_dir = Path(config.state_dir)
    worker = Store(state_dir).authenticate_worker(
        _private_bytes(config.worker_credential_file, 4096).decode("ascii").strip()
    )

    def guardian_status(state):
        from .salad_server import SaladSafetyHTTPSClient

        return SaladSafetyHTTPSClient(**config.custody.model_dump(mode="json")).status(state)

    return SaladCampaignExecutor(
        config,
        profile,
        StoreControl(state_dir, worker, guardian_status),
        worker_principal_id=worker["id"],
        importer=trusted_importer(config, profile, worker, state_dir),
    )


def main(argv=None) -> int:
    from tessera_worker.service_loop import StopFlag, run_service

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--max-cycles", type=int, default=None)
    parser.add_argument("--health-file", type=Path, default=None)
    options = parser.parse_args(argv)
    try:
        config = load_config(options.config)
        executor = build_executor(config)
    except Exception:
        print(json.dumps({"status": "refused", "reason": "executor_setup_unavailable"}))
        return 2
    stop = StopFlag()
    stop.install()
    return run_service(
        step=executor.run_once,
        interval=config.poll_seconds,
        once=options.once,
        max_cycles=options.max_cycles,
        health_file=options.health_file,
        name="campaign-salad-executor",
        stop=stop,
    )


if __name__ == "__main__":
    raise SystemExit(main())
