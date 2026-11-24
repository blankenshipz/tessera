"""Connect control packages, released inputs and the durable CPU supervisor.

The transport and adapter are supplied by trusted supervisor configuration.
There is deliberately no unattended dispatch CLI until worker enrollment and
artifact/cost acceptance are integrated. Closing a desktop has no role here.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from tessera_control.image_build_models import MAX_RESULT_BYTES, repository_registered
from tessera_control.persistent_output_storage import validate_persistent_output_storage

from .chunked_inputs import materialize_chunked_snapshot, verify_chunked_authority
from .control_channel import classify_control_failure
from .cpu import EGRESS_PROFILE, CpuOperation
from .image_build import ImageBuildError, builder_argv, parse_build_result
from .inputs import materialize_snapshot
from .provider import Provider, ProviderDispatch
from .public_fetch import PublicFetchError, fetch_public_files
from .supervisor import CpuSupervisor, DriverResult, Lease, SupervisorError

MAX_DESCRIPTOR_BYTES = 2 * 1024 * 1024
EGRESS_REPORT_STATE = "egress_journal"
PUBLIC_FETCH_REPORT_STATE = "public_fetch"
IMAGE_BUILD_REPORT_STATE = "image_build"
BUILD_OUTPUT_DIRECTORY = "build-output"

# Observation delivery (ADR 0054): a report that cannot be delivered stays in the durable
# ``reports`` journal and is retried with bounded exponential backoff, keyed by its fixed
# idempotency key, until control accepts or refuses it or it is 24 hours old.
DELIVERY_BACKOFF_BASE_SECONDS = 2.0
DELIVERY_BACKOFF_CAP_SECONDS = 300.0
DELIVERY_MAX_AGE = timedelta(hours=24)
# One report may use at most the lease time left, less this reserve (one renewal interval), so a
# report that hangs cannot by itself lapse the 30 s lease (the loop is single-threaded and the
# renewal follows). It is never shorter than the minimum request time.
REPORT_LEASE_RESERVE_SECONDS = 10.0
MIN_REPORT_REQUEST_SECONDS = 1.0


class ObservationDeliveryPending(SupervisorError):
    """An observation is durably recorded but control did not take it; retry later."""

    def __init__(
        self,
        message: str,
        retry_after_seconds: float,
        *,
        auth: bool = False,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.auth = auth
        self.status = status  # the HTTP status of a rejected credential, when there was one


class ObservationRefused(SupervisorError):
    """Control permanently refused an observation, or it aged out undelivered."""


class ControlChannel(Protocol):
    def post(self, path: str, body: dict[str, Any], key: str) -> dict[str, Any]: ...


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class CpuRuntime:
    """One durable operation per private runtime directory; fresh checks on ticks."""

    def __init__(
        self,
        directory: Path,
        input_root: Path,
        channel: ControlChannel,
        adapter: Any,
        *,
        worker_id: str,
        persistent_output_collection: bool = False,
        persistent_output_storage: dict[str, Any] | None = None,
        provider: Provider | None = None,
        public_root: Path | None = None,
        public_cache_dir: Path | None = None,
        public_fetch_proxy: str | None = None,
        public_fetcher: Callable[..., list[dict[str, Any]]] = fetch_public_files,
        registry_auth_file: Path | None = None,
        build_repositories: tuple[str, ...] = (),
    ):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink() or stat.S_IMODE(self.directory.stat().st_mode) != 0o700:
            raise SupervisorError("Runtime requires a private 0700 directory.")
        self.input_root = Path(input_root)
        if self.input_root.is_symlink() or stat.S_IMODE(self.input_root.stat().st_mode) != 0o700:
            raise SupervisorError("Runtime inputs require private 0700 supervisor custody.")
        # ADR 0097: private custody for supervisor-fetched public files, and the optional
        # worker-configured cache (used only when a plan's cache flag is true).
        self.public_root = Path(public_root) if public_root is not None else None
        if self.public_root is not None and (
            self.public_root.is_symlink() or stat.S_IMODE(self.public_root.stat().st_mode) != 0o700
        ):
            raise SupervisorError("Runtime public inputs require private 0700 supervisor custody.")
        self.public_cache_dir = Path(public_cache_dir) if public_cache_dir is not None else None
        # Optional explicit operator proxy; None fetches directly (environment proxies ignored).
        self.public_fetch_proxy = public_fetch_proxy
        self._public_fetcher = public_fetcher
        # ADR 0123: the operator's registry auth file, mounted read-only into a pushing build.
        self.registry_auth_file = (
            Path(registry_auth_file) if registry_auth_file is not None else None
        )
        # ADR 0123: the worker operator's own registered push destinations (``host[/path]``).
        # Empty refuses every pushing build, whatever control approved.
        self.build_repositories = tuple(build_repositories)
        self.channel = channel
        self.adapter = adapter
        self.worker_id = worker_id
        self.persistent_output_collection = persistent_output_collection
        self.persistent_output_storage = persistent_output_storage
        self.descriptor: dict[str, Any] | None = None
        self.operation: CpuOperation | None = None
        self.lease: Lease | None = None
        self.provider_dispatch = (
            ProviderDispatch(self.directory, channel, provider) if provider is not None else None
        )
        # Injectable so retry timing is testable without sleeping.
        self._clock: Callable[[], datetime] = lambda: datetime.now(UTC)
        self._initialize_observation_journal()
        self._initialize_package_refresh_journal()
        self.supervisor = CpuSupervisor(
            self.directory / "journal",
            adapter,
            self._validate,
            before_start=self.provider_dispatch.before_start if self.provider_dispatch else None,
            before_reconcile=(
                self.provider_dispatch.reconcile_stable if self.provider_dispatch else None
            ),
            before_cancel=self.provider_dispatch.cancel_stable if self.provider_dispatch else None,
        )

    def close(self) -> None:
        self.supervisor.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _request_package(self, lease_id: str, epoch: int) -> dict[str, Any]:
        body = self._package_body(epoch)
        return self.channel.post(
            f"/worker/leases/{lease_id}/package",
            body,
            f"runtime-package-{lease_id}",
        )

    def _package_body(self, epoch: int) -> dict[str, Any]:
        body: dict[str, Any] = {"expected_epoch": epoch}
        if self.persistent_output_collection:
            body["persistent_output_collection"] = True
            body["persistent_output_storage"] = self.persistent_output_storage
        return body

    def _current_package(self, lease_id: str, epoch: int) -> dict[str, Any]:
        """Read current authority with a durable fresh key, not a cached package reply."""

        key = self._package_refresh_key(lease_id, epoch)
        try:
            response = self.channel.post(
                f"/worker/leases/{lease_id}/package", self._package_body(epoch), key
            )
            self._validate_package(response, lease_id, epoch)
        except Exception:
            # A lost response keeps this exact read intent pending, so a restart
            # replays it rather than silently treating a stale cached reply as live.
            raise
        self._complete_package_refresh(lease_id, epoch)
        return response

    def _validate_package(self, response: dict[str, Any], lease_id: str, epoch: int) -> None:
        try:
            package, lease, operation = (
                response["package"],
                response["lease"],
                response["operation"],
            )
            plan = package["plan"]
            spec = plan["execution_spec"]
            if (
                package["version"] != 1
                or package["profile_version"] != "local-cpu-v1"
                or package["adapter_version"] != "docker-local-cpu-v1"
                or _digest(package) != response["package_digest"]
                or _digest(plan) != package["plan_digest"]
                or package["lease_id"] != lease_id
                or lease["id"] != lease_id
                or type(package["epoch"]) is not int
                or package["epoch"] != epoch
                or lease["epoch"] != epoch
                or lease["grant_id"] != package["grant_id"]
                or package["worker_principal_id"] != self.worker_id
                or lease["worker_principal_id"] != self.worker_id
                or package["hard_expires_at"] != lease["hard_expires_at"]
                or spec["profile"] != "local-cpu"
                or spec["network_enabled"] is not False
                or spec["pinned_skill_digests"]
                or spec["input_snapshot_digest"] != package["snapshot_digest"]
                or package["operation_id"]
                != hashlib.sha256(f"tessera-cpu-v1:{lease_id}".encode()).hexdigest()[:32]
                or package["docker_lookup_key"] != f"tessera-op-{package['operation_id']}"
                or operation["operation_id"] != package["operation_id"]
                or operation["lease_id"] != lease_id
                or operation["grant_id"] != package["grant_id"]
                or operation["worker_principal_id"] != self.worker_id
                or operation["project_id"] != package["project_id"]
                or operation["task_id"] != package["task_id"]
                or operation["epoch"] != epoch
                or operation["package_digest"] != response["package_digest"]
                or operation["adapter_version"] != package["adapter_version"]
                or operation["profile_version"] != package["profile_version"]
                or operation["docker_lookup_key"] != package["docker_lookup_key"]
                or operation["retry_class"] != "manual_reconciliation"
                or operation["desired_state"] not in {"admitted", "cancel_requested"}
                or operation["observed_state"] not in {"unknown", "running", "exited"}
            ):
                raise SupervisorError("Control package does not match runtime authority.")
            experiment = package.get("experiment")
            operation_identity = {
                "experiment_attempt_id": operation.get("experiment_attempt_id"),
                "experiment_id": operation.get("experiment_id"),
                "design_revision": operation.get("experiment_design_revision"),
                "variant_id": operation.get("experiment_variant_id"),
            }
            if experiment is None:
                if any(value is not None for value in operation_identity.values()):
                    raise SupervisorError("Control operation has an unbound experiment identity.")
            elif (
                set(experiment)
                != {
                    "admitted_attempt_id",
                    "experiment_id",
                    "design_revision",
                    "variant_id",
                    "task_id",
                    "plan_digest",
                    "grant_id",
                }
                or experiment["task_id"] != package["task_id"]
                or experiment["plan_digest"] != package["plan_digest"]
                or experiment["grant_id"] != package["grant_id"]
                or experiment["admitted_attempt_id"] != operation_identity["experiment_attempt_id"]
                or experiment["experiment_id"] != operation_identity["experiment_id"]
                or experiment["design_revision"] != operation_identity["design_revision"]
                or experiment["variant_id"] != operation_identity["variant_id"]
            ):
                raise SupervisorError(
                    "Control package experiment identity does not match its operation."
                )
            self._validate_network_profile(package, spec)
            self._validate_public_fetch(package, spec)
            self._validate_build(package, spec)
            capability = package.get("persistent_output_storage")
            if self.persistent_output_collection:
                if capability is None:
                    raise SupervisorError("Control package lacks persistent output storage.")
                if validate_persistent_output_storage(capability) != self.persistent_output_storage:
                    raise SupervisorError("Control package persistent output storage changed.")
                if spec["max_output_bytes"] > capability["capacity_bytes"]:
                    raise SupervisorError(
                        "Control package output storage capacity is insufficient."
                    )
            elif capability is not None:
                raise SupervisorError(
                    "Control package unexpectedly enables persistent output storage."
                )
            expiry = datetime.fromisoformat(lease["expires_at"])
            hard_expiry = datetime.fromisoformat(lease["hard_expires_at"])
            if expiry.tzinfo is None or hard_expiry.tzinfo is None or expiry > hard_expiry:
                raise SupervisorError("Control package lease bounds are invalid.")
        except (KeyError, TypeError, ValueError) as error:
            raise SupervisorError("Control package is malformed.") from error

    @staticmethod
    def _validate_network_profile(package: dict[str, Any], spec: dict[str, Any]) -> None:
        policy = spec.get("egress")
        profile = package.get("network_profile")
        if profile is None:
            # Older packages predate network_profile: network none only.
            if policy is not None or package.get("egress") is not None:
                raise SupervisorError("Control package egress lacks a network profile.")
        elif profile == "none":
            if policy is not None or package.get("egress") is not None:
                raise SupervisorError("Control package network profile contradicts its egress.")
        elif profile == EGRESS_PROFILE:
            if not isinstance(policy, dict) or package.get("egress") != policy:
                raise SupervisorError("Control package egress does not match its plan policy.")
            try:
                CpuOperation(
                    "0" * 32,
                    "x@sha256:" + "0" * 64,
                    ("x",),
                    "0" * 64,
                    1,
                    network_profile=profile,
                    egress=policy,
                )
            except ValueError as error:
                raise SupervisorError("Control package egress policy is invalid.") from error
        else:
            raise SupervisorError("Control package has an unknown network profile.")

    @staticmethod
    def _validate_public_fetch(package: dict[str, Any], spec: dict[str, Any]) -> None:
        """The package carries the plan's public fetch exactly, only without any egress."""
        plan = package.get("public_fetch")
        if plan is None and spec.get("public_fetch") is None:
            return
        if (
            not isinstance(plan, dict)
            or plan != spec.get("public_fetch")
            or package.get("network_profile") != "none"
            or not isinstance(plan.get("files"), list)
            or not isinstance(plan.get("hosts"), list)
            or not plan["files"]
            or type(plan.get("cache")) is not bool
        ):
            raise SupervisorError("Control package public fetch does not match its plan.")

    @staticmethod
    def _validate_build(package: dict[str, Any], spec: dict[str, Any]) -> None:
        """A build package carries the plan's policy exactly, the argv derived from it, and the
        allowlist egress; nothing else may run in a build stage (ADR 0123)."""
        policy = package.get("build")
        if policy is None and spec.get("build") is None:
            return
        try:
            if (
                policy != spec.get("build")
                or package.get("network_profile") != EGRESS_PROFILE
                or package.get("public_fetch") is not None
                or spec["argv"] != builder_argv(policy)
            ):
                raise SupervisorError("Control package build does not match its plan.")
        except ImageBuildError as error:
            raise SupervisorError("Control package build policy is invalid.") from error

    def _build_custody(self, policy: dict[str, Any]) -> dict[str, str | None]:
        """The private /outputs directory and, when pushing, the operator's registry auth."""
        if policy["push"] and self.registry_auth_file is None:
            raise SupervisorError("no registry auth configured for a pushing image build.")
        if policy["push"] and not repository_registered(
            policy["repository"], self.build_repositories
        ):
            # Checked before the auth file is named for a mount: the operator's push rights
            # apply only to repositories this worker's own operator registered.
            raise SupervisorError("build repository is not registered on this worker.")
        output = self.directory / BUILD_OUTPUT_DIRECTORY
        output.mkdir(mode=0o700, exist_ok=True)
        if output.is_symlink() or stat.S_IMODE(output.stat().st_mode) != 0o700:
            raise SupervisorError("Image build output requires a private 0700 directory.")
        return {
            "build_directory": str(output),
            "registry_auth_file": str(self.registry_auth_file) if policy["push"] else None,
        }

    def _fresh_build_output(self) -> None:
        """Start a new build operation with an empty ``build-output`` directory.

        A result file an earlier operation left in the reused runtime directory must never be
        read as this build's (ADR 0123). Called only when a new operation is admitted; recovery
        of an existing one keeps what its own container wrote."""
        output = self.directory / BUILD_OUTPUT_DIRECTORY
        if output.is_symlink():
            raise SupervisorError("Image build output requires a private 0700 directory.")
        if output.exists():
            shutil.rmtree(output, ignore_errors=True)
            if output.exists():
                raise SupervisorError("Image build output could not be cleared.")

    def _fetch_public(self, operation_id: str, plan: dict[str, Any]) -> dict[str, Any]:
        """Download and verify the plan's public files into private custody (ADR 0097).

        Runs in the supervisor, before any container exists; the container stays network none
        and only ever sees the verified files. Any failure removes everything fetched."""
        if self.public_root is None:
            raise SupervisorError(
                "Public fetch refused: this worker has no public input root; the operator "
                "must set --public-root (or public_root in the poller config)."
            )
        directory = self.public_root / f"tessera-public-{operation_id}"
        self._discard_public(directory)
        directory.mkdir(mode=0o700)
        cache = self.public_cache_dir if plan["cache"] else None
        proxy = {"proxy": self.public_fetch_proxy} if self.public_fetch_proxy is not None else {}
        rows: list[dict[str, Any]] = []
        try:
            for name in sorted({item["input"] for item in plan["files"]}):
                pins = [
                    {key: item[key] for key in ("name", "url", "sha256", "max_bytes")}
                    for item in plan["files"]
                    if item["input"] == name
                ]
                fetched = self._public_fetcher(
                    pins,
                    dest=directory / name,
                    redirect_hosts=tuple(plan["hosts"]),
                    cache_dir=cache,
                    **proxy,
                )
                rows += [{"input": name} | dict(row) for row in fetched]
            expected = {(row["input"], row["name"]) for row in rows}
            if expected != {(item["input"], item["name"]) for item in plan["files"]}:
                raise SupervisorError("Public fetch failed: incomplete result.")
            for name in {row["input"] for row in rows}:
                found = {entry.name for entry in os.scandir(directory / name)}
                if found != {row["name"] for row in rows if row["input"] == name}:
                    raise SupervisorError("Public fetch failed: unexpected files.")
                (directory / name).chmod(0o555)
        except PublicFetchError as error:
            self._discard_public(directory)
            raise SupervisorError(f"Public fetch failed: {error.code}") from None
        except OSError:
            self._discard_public(directory)
            raise SupervisorError("Public fetch failed: io_error") from None
        except BaseException:
            self._discard_public(directory)
            raise
        return {"directory": str(directory), "rows": rows}

    @staticmethod
    def _discard_public(directory: Path) -> None:
        if not directory.exists():
            return
        for current, directories, _ in os.walk(directory):
            Path(current).chmod(0o700)
            for item in directories:
                (Path(current) / item).chmod(0o700)
        shutil.rmtree(directory, ignore_errors=True)

    def _operation(self, descriptor: dict[str, Any]) -> CpuOperation:
        response = descriptor["control"]
        package = response["package"]
        self._validate_package(response, package["lease_id"], package["epoch"])
        if (
            hashlib.sha256(descriptor["input_manifest_json"].encode()).hexdigest()
            != package["snapshot_digest"]
        ):
            raise SupervisorError("Staged manifest does not match the control package.")
        fetched = descriptor.get("public_fetch")
        plan = package.get("public_fetch")
        if (fetched is None) != (plan is None):
            raise SupervisorError("Runtime descriptor public fetch does not match its package.")
        public: dict[str, Any] = {}
        if fetched is not None:
            pins = {(f["input"], f["name"]): f["sha256"] for f in plan["files"]}
            rows = fetched["rows"]
            if {(r["input"], r["name"]): r["sha256"] for r in rows} != pins:
                raise SupervisorError("Runtime descriptor public fetch contradicts its plan.")
            public = {
                "public_directory": fetched["directory"],
                "public_files": tuple(
                    (r["input"], r["name"], r["sha256"], r["bytes"]) for r in rows
                ),
            }
        build = self._build_custody(package["build"]) if package.get("build") is not None else {}
        return CpuOperation(
            operation_id=package["operation_id"],
            image=package["pinned_image"],
            argv=tuple(package["plan"]["execution_spec"]["argv"]),
            plan_digest=package["plan_digest"],
            max_duration_seconds=package["plan"]["max_duration_seconds"],
            input_directory=descriptor["input_directory"],
            input_manifest_json=descriptor["input_manifest_json"],
            network_profile=package.get("network_profile") or "none",
            egress=package["plan"]["execution_spec"].get("egress"),
            # Operator container limits belong to the operation, so the supervisor's durable
            # fence (and recovery's digest check) covers the limits the container runs with.
            **public,
            **build,
            **self._operation_limits(package["plan"]["execution_spec"].get("max_scratch_bytes")),
        )

    def _operation_limits(self, requested_scratch: int | None = None) -> dict[str, int]:
        """The operator's limits, with a plan's scratch request granted or refused (ADR 0059).

        A request above the operator's ceiling, or that the container's memory cannot back,
        is refused here, at admission, before any container exists."""
        limits = getattr(self.adapter, "operation_limits", None)
        values = limits() if callable(limits) else {}
        if requested_scratch is None:
            return values
        grant = getattr(self.adapter, "scratch_for", None)
        if not callable(grant):
            raise SupervisorError("This worker does not grant a plan's scratch request.")
        try:
            values["scratch_bytes"] = grant(requested_scratch)
        except ValueError as error:
            raise SupervisorError(str(error)) from None
        return values

    @staticmethod
    def _lease(response: dict[str, Any]) -> Lease:
        package, lease = response["package"], response["lease"]
        return Lease(
            id=lease["id"],
            operation_id=package["operation_id"],
            plan_digest=package["plan_digest"],
            epoch=lease["epoch"],
            expires_at=datetime.fromisoformat(lease["expires_at"]).astimezone(UTC),
            task_revision=package["task_revision"],
            project_id=package["project_id"],
        )

    def _validate(self, lease: Lease) -> bool:
        if self.descriptor is None:
            return False
        try:
            response = self._current_package(lease.id, lease.epoch)
            return response["package_digest"] == self.descriptor["control"][
                "package_digest"
            ] and datetime.fromisoformat(response["lease"]["expires_at"]) > datetime.now(UTC)
        except Exception:
            return False

    def start(self, lease_id: str, epoch: int) -> DriverResult:
        """Recover an existing identity or stage exactly one newly admitted package."""
        path = self.directory / "runtime.json"
        if path.exists() or path.is_symlink():
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise SupervisorError("Runtime descriptor must be a regular file.")
                encoded = source.read(MAX_DESCRIPTOR_BYTES + 1)
            if len(encoded) > MAX_DESCRIPTOR_BYTES:
                raise SupervisorError("Runtime descriptor exceeds its bound.")
            descriptor = json.loads(encoded)
            response = descriptor["control"]
            self._validate_package(response, lease_id, epoch)
            if descriptor.get("experiment_identity") != response["package"].get("experiment"):
                raise SupervisorError("Runtime descriptor experiment identity changed.")
            self._bind_journal(lease_id, epoch)
        else:
            response = self._request_package(lease_id, epoch)
            self._validate_package(response, lease_id, epoch)
            self._bind_journal(lease_id, epoch)
            package = response["package"]
            # ADR 0097: the supervisor fetches pinned public files before inputs or a container.
            fetched = (
                self._fetch_public(package["operation_id"], package["public_fetch"])
                if package.get("public_fetch") is not None
                else None
            )
            bundle = self.channel.post(
                f"/worker/leases/{lease_id}/inputs",
                {"expected_epoch": epoch},
                f"runtime-inputs-{lease_id}",
            )
            if isinstance(bundle, dict) and (
                isinstance(bundle.get("snapshot"), dict)
                and bundle["snapshot"].get("delivery") == "chunked"
            ):
                verify_chunked_authority(
                    bundle, package["snapshot_digest"], lease_id, epoch, self.worker_id
                )
                staged = materialize_chunked_snapshot(
                    bundle["snapshot"],
                    lambda seq: self.channel.post(
                        f"/worker/leases/{lease_id}/inputs/chunks/{seq}",
                        {"expected_epoch": epoch},
                        f"runtime-input-chunk-{lease_id}-{seq}",
                    ),
                    self.input_root,
                    expected_snapshot_digest=package["snapshot_digest"],
                )
            else:
                staged = materialize_snapshot(
                    bundle,
                    self.input_root,
                    expected_snapshot_digest=package["snapshot_digest"],
                    expected_lease_id=lease_id,
                    expected_epoch=epoch,
                    expected_worker_id=self.worker_id,
                )
            descriptor = {
                "control": response,
                "experiment_identity": package.get("experiment"),
                "input_directory": str(staged.directory),
                "input_manifest_json": staged.manifest_json,
            }
            if fetched is not None:
                descriptor["public_fetch"] = fetched
            self._operation(descriptor)  # Validate before persisting local operation intent.
            if package.get("build") is not None:
                self._fresh_build_output()
            encoded = json.dumps(descriptor, sort_keys=True).encode()
            if len(encoded) > MAX_DESCRIPTOR_BYTES:
                raise SupervisorError("Runtime descriptor exceeds its bound.")
            fd, temporary = tempfile.mkstemp(prefix="runtime-", suffix=".tmp", dir=self.directory)
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(encoded)
                    output.flush()
                    os.fsync(output.fileno())
                os.link(temporary, path)  # Publish complete bytes without replacing another intent.
            finally:
                os.unlink(temporary)
            parent_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        self.descriptor = descriptor
        self.operation = self._operation(descriptor)
        self.lease = self._lease(response)
        self._refresh_lease()
        return self.supervisor.drive_once(self.operation, self.lease)

    def recover(self, lease_id: str, epoch: int) -> DriverResult:
        """Reconcile a durable runtime intent without issuing a start.

        A descriptor can survive a worker crash after package admission but before
        the supervisor recorded (or received) a start.  Recovery must observe
        that stable identity rather than treating the descriptor as permission to
        create or start it again.
        """

        path = self.directory / "runtime.json"
        if not path.exists() and not path.is_symlink():
            raise SupervisorError("Runtime has no durable descriptor to recover.")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise SupervisorError("Runtime descriptor must be a regular file.")
            encoded = source.read(MAX_DESCRIPTOR_BYTES + 1)
        if len(encoded) > MAX_DESCRIPTOR_BYTES:
            raise SupervisorError("Runtime descriptor exceeds its bound.")
        descriptor = json.loads(encoded)
        response = descriptor["control"]
        self._validate_package(response, lease_id, epoch)
        if descriptor.get("experiment_identity") != response["package"].get("experiment"):
            raise SupervisorError("Runtime descriptor experiment identity changed.")
        self._bind_journal(lease_id, epoch)
        self.descriptor = descriptor
        self.operation = self._operation(descriptor)
        self.lease = self._lease(response)
        self._refresh_lease()
        return self.supervisor.reconcile(self.operation, self.lease)

    def _bind_journal(self, lease_id: str, epoch: int) -> None:
        """Fence a second local journal for the same lease under this worker root."""
        if not isinstance(lease_id, str) or not 1 <= len(lease_id) <= 200:
            raise SupervisorError("Runtime lease identity is invalid.")
        path = self.input_root / f".runtime-{hashlib.sha256(lease_id.encode()).hexdigest()}.json"
        binding = {
            "lease_id": lease_id,
            "epoch": epoch,
            "worker_id": self.worker_id,
            "directory": str(self.directory.resolve()),
        }
        encoded = json.dumps(binding, sort_keys=True).encode()
        fd, temporary = tempfile.mkstemp(prefix=".binding-", dir=self.input_root)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                existing_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(existing_fd, "rb") as source:
                    if source.read(4097) != encoded:
                        raise SupervisorError(
                            "Lease is bound to another runtime journal."
                        ) from None
            root_fd = os.open(self.input_root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(root_fd)
            finally:
                os.close(root_fd)
        finally:
            os.unlink(temporary)

    def _refresh_lease(self) -> None:
        if self.lease is None or self.descriptor is None:
            raise SupervisorError("Runtime has no bound operation.")
        try:
            current = self._current_package(self.lease.id, self.lease.epoch)
            if current["package_digest"] == self.descriptor["control"]["package_digest"]:
                self.lease = self._lease(current)
        except Exception:
            # Retain old bounds; the supervisor's fresh validation will deny and
            # cancel possible execution when the control channel is unavailable.
            pass

    def renew(self) -> None:
        """Compatibility seam; the development service uses its durable renewal journal."""
        if self.lease is None:
            raise SupervisorError("Runtime has no bound operation.")
        self.channel.post(
            "/worker/leases/renew",
            {
                "lease_id": self.lease.id,
                "expected_epoch": self.lease.epoch,
            },
            f"runtime-renew-{uuid.uuid4().hex}",
        )
        self._refresh_lease()

    def tick(self) -> DriverResult:
        if self.operation is None or self.lease is None:
            raise SupervisorError("Runtime has no bound operation.")
        self._refresh_lease()
        return self.supervisor.tick(self.operation, self.lease)

    def report(self, result: DriverResult) -> dict[str, Any]:
        """Report authenticated observations; never infer cost or scientific success."""
        if self.lease is None:
            raise SupervisorError("Runtime has no bound operation.")
        observation = result.observation
        state = observation.state if observation.state in {"running", "exited"} else "unknown"
        provider_id = observation.container_id
        if self.provider_dispatch is not None:
            if state == "exited" and observation.termination_confirmed:
                self.provider_dispatch.complete_stable(self.operation)
            provider_id = self.provider_dispatch.provider_id(self.operation)
        launch_error = result.launch_error if state == "unknown" and provider_id is None else None
        confirmed_exit = state == "exited" and observation.termination_confirmed
        exit_code = (
            observation.exit_code if confirmed_exit and type(observation.exit_code) is int else None
        )
        # A confirmed exit control already accepted is never sent again: a resumed run (for
        # example after its egress journal could not be delivered) reuses the stored answer, so
        # control records one exit per operation, not one per attempt.
        response = self._delivered_exit() if confirmed_exit else None
        if response is None:
            payload, key, intent_id = self._observation_intent(
                state, provider_id, confirmed_exit, launch_error, exit_code
            )
            self._begin_delivery(intent_id, confirmed_exit)
            try:
                response = self._post_report("/worker/observations", payload, key)
            except Exception as error:
                self._delivery_failed(intent_id, error)
            with self._observation_connection() as connection:
                connection.execute(
                    "UPDATE reports SET completed=1, response_json=? WHERE id=?",
                    (json.dumps(response, sort_keys=True, separators=(",", ":")), intent_id),
                )
        if (
            state == "exited"
            and observation.termination_confirmed
            and self.operation is not None
            and self.operation.network_profile == EGRESS_PROFILE
        ):
            self._report_egress_journal()
        self._report_public_fetch()
        if confirmed_exit:
            self._report_image_build(exit_code)
        return response

    def _report_request_seconds(self) -> float | None:
        """The request time one report may use: the channel's timeout, capped by the lease.

        ``None`` when the channel has no timeout of its own (a test double): it is called
        without one.
        """
        channel_timeout = getattr(self.channel, "timeout_seconds", None)
        if type(channel_timeout) not in {int, float}:
            return None
        budget = float(channel_timeout)
        if self.lease is not None:
            left = (self.lease.expires_at - self._clock()).total_seconds()
            budget = min(budget, left - REPORT_LEASE_RESERVE_SECONDS)
        return max(MIN_REPORT_REQUEST_SECONDS, min(float(channel_timeout), budget))

    def _post_report(self, path: str, payload: dict[str, Any], key: str) -> dict[str, Any]:
        """Post a report, with a per-call time cap when the channel supports one."""
        seconds = self._report_request_seconds()
        try:
            accepts = "timeout_seconds" in inspect.signature(self.channel.post).parameters
        except (TypeError, ValueError):
            accepts = False
        if seconds is None or not accepts:
            return self.channel.post(path, payload, key)
        return self.channel.post(path, payload, key, timeout_seconds=seconds)

    def _delivered_exit(self) -> dict[str, Any] | None:
        """Control's stored answer to this operation's accepted confirmed exit, if any."""
        if self.operation is None:
            return None
        with self._observation_connection() as connection:
            row = connection.execute(
                """SELECT response_json FROM reports
                WHERE operation_id=? AND observed_state='exited'
                AND termination_confirmed=1 AND completed=1 AND abandoned=0
                AND response_json IS NOT NULL ORDER BY id LIMIT 1""",
                (self.operation.operation_id,),
            ).fetchone()
        return None if row is None else json.loads(row[0])

    def _begin_delivery(self, intent_id: int, confirmed_exit: bool) -> None:
        """Supersede stale intents, and honour the persisted backoff after a restart.

        A confirmed exit makes every earlier undelivered non-exit observation of the operation
        moot, so those are marked abandoned and never sent after it (control would otherwise
        see an old ``running`` after the exit). A report whose ``next_attempt_at`` has not come
        is not sent: the retry delay survives a worker restart, so a crash loop cannot hammer
        control.
        """
        with self._observation_connection() as connection:
            if confirmed_exit and self.operation is not None:
                connection.execute(
                    """UPDATE reports SET abandoned=1, last_error='superseded'
                    WHERE operation_id=? AND completed=0 AND abandoned=0 AND id<>?
                    AND observed_state NOT IN ('exited', ?, ?)""",
                    (
                        self.operation.operation_id,
                        intent_id,
                        EGRESS_REPORT_STATE,
                        PUBLIC_FETCH_REPORT_STATE,
                    ),
                )
            row = connection.execute(
                "SELECT next_attempt_at, first_failed_at FROM reports WHERE id=?", (intent_id,)
            ).fetchone()
        now = self._clock()
        if row is not None and row[0] is not None:
            wait = (datetime.fromisoformat(row[0]) - now).total_seconds()
            if wait > 0:
                raise ObservationDeliveryPending("Observation delivery is backing off.", wait)

    def _delivery_failed(self, intent_id: int, error: Exception) -> None:
        """Record a failed delivery durably and raise pending or refused (never returns).

        The record is committed before the exception leaves, so the outcome survives a
        restart. A permanent refusal, or a report older than ``DELIVERY_MAX_AGE``, is
        abandoned and raises ``ObservationRefused``; anything else is retried after an
        exponential delay capped at ``DELIVERY_BACKOFF_CAP_SECONDS``.
        """
        kind = classify_control_failure(error)
        now = self._clock()
        with self._observation_connection() as connection:
            row = connection.execute(
                "SELECT attempts, first_failed_at, operation_id FROM reports WHERE id=?",
                (intent_id,),
            ).fetchone()
            attempts = (row[0] if row else 0) + 1
            # The age bound is per operation: the earliest undelivered failure of any of its
            # reports counts, so a newer (for example superseding exit) row does not restart
            # the 24 hours.
            started = [
                datetime.fromisoformat(value[0])
                for value in connection.execute(
                    """SELECT first_failed_at FROM reports WHERE operation_id=?
                    AND completed=0 AND first_failed_at IS NOT NULL""",
                    (row[2],),
                )
            ]
            if row and row[1]:
                started.append(datetime.fromisoformat(row[1]))
            first = min(started, default=now)
            detail = f"{kind}: {type(error).__name__}"[:120]
            reason = None
            if kind == "permanent":
                reason = "refused"
            elif now - first > DELIVERY_MAX_AGE:
                reason = "aged out undelivered"
            delay = min(
                DELIVERY_BACKOFF_CAP_SECONDS,
                DELIVERY_BACKOFF_BASE_SECONDS * 2 ** (attempts - 1),
            )
            if reason is not None:
                connection.execute(
                    """UPDATE reports SET abandoned=1, attempts=?, first_failed_at=?,
                    last_error=? WHERE id=?""",
                    (attempts, first.isoformat(), f"{reason}: {detail}", intent_id),
                )
            else:
                connection.execute(
                    """UPDATE reports SET attempts=?, first_failed_at=?, next_attempt_at=?,
                    last_error=? WHERE id=?""",
                    (
                        attempts,
                        first.isoformat(),
                        (now + timedelta(seconds=delay)).isoformat(),
                        detail,
                        intent_id,
                    ),
                )
        if reason is not None:
            raise ObservationRefused(f"Observation {reason}.") from error
        raise ObservationDeliveryPending(
            "Observation delivery failed; it will be retried.",
            delay,
            auth=kind == "auth",
            status=getattr(error, "status", None),
        ) from error

    def _report_egress_journal(self) -> None:
        """Post the kept proxy journal's digest and summary exactly once, replayably."""
        if self.lease is None or self.operation is None:
            raise SupervisorError("Runtime has no bound operation.")
        operation = self.operation
        with self._observation_connection() as connection:
            done = connection.execute(
                "SELECT 1 FROM reports WHERE operation_id=? AND observed_state=? AND completed=1",
                (operation.operation_id, EGRESS_REPORT_STATE),
            ).fetchone()
            pending = connection.execute(
                """SELECT id, payload_json, idempotency_key FROM reports
                WHERE operation_id=? AND observed_state=? AND completed=0
                ORDER BY id DESC LIMIT 1""",
                (operation.operation_id, EGRESS_REPORT_STATE),
            ).fetchone()
        if done is not None:
            return
        if pending is not None:
            intent_id, payload, key = pending[0], json.loads(pending[1]), pending[2]
        else:
            teardown = getattr(self.adapter, "teardown_egress", None)
            summarize = getattr(self.adapter, "egress_summary", None)
            if summarize is None:
                raise SupervisorError("Adapter cannot report the egress journal.")
            if teardown is not None:
                teardown(operation)
            journal_sha256, summary = summarize(operation)
            payload = {
                "lease_id": self.lease.id,
                "expected_epoch": self.lease.epoch,
                "journal_sha256": journal_sha256,
                "summary": summary,
            }
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            key = (
                "runtime-egress-journal-"
                + hashlib.sha256(f"{operation.operation_id}:{encoded}".encode()).hexdigest()
            )
            with self._observation_connection() as connection:
                cursor = connection.execute(
                    """INSERT INTO reports
                    (operation_id, observed_state, provider_id, termination_confirmed,
                     payload_json, idempotency_key, completed) VALUES (?, ?, NULL, 1, ?, ?, 0)""",
                    (operation.operation_id, EGRESS_REPORT_STATE, encoded, key),
                )
                intent_id = cursor.lastrowid
        self._begin_delivery(intent_id, False)
        try:
            self._post_report(
                f"/worker/operations/{operation.operation_id}/egress-journal", payload, key
            )
        except Exception as error:
            self._delivery_failed(intent_id, error)
        with self._observation_connection() as connection:
            connection.execute("UPDATE reports SET completed=1 WHERE id=?", (intent_id,))

    def _report_public_fetch(self) -> None:
        """Post the per-file fetch rows (never the files) once, replayably (ADR 0097)."""
        if self.lease is None or self.operation is None:
            raise SupervisorError("Runtime has no bound operation.")
        fetched = (self.descriptor or {}).get("public_fetch")
        if fetched is None:
            return
        operation = self.operation
        with self._observation_connection() as connection:
            done = connection.execute(
                "SELECT 1 FROM reports WHERE operation_id=? AND observed_state=? AND completed=1",
                (operation.operation_id, PUBLIC_FETCH_REPORT_STATE),
            ).fetchone()
            pending = connection.execute(
                """SELECT id, payload_json, idempotency_key FROM reports
                WHERE operation_id=? AND observed_state=? AND completed=0
                ORDER BY id DESC LIMIT 1""",
                (operation.operation_id, PUBLIC_FETCH_REPORT_STATE),
            ).fetchone()
        if done is not None:
            return
        if pending is not None:
            intent_id, payload, key = pending[0], json.loads(pending[1]), pending[2]
        else:
            payload = {
                "lease_id": self.lease.id,
                "expected_epoch": self.lease.epoch,
                "files": fetched["rows"],
            }
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            key = (
                "runtime-public-fetch-"
                + hashlib.sha256(f"{operation.operation_id}:{encoded}".encode()).hexdigest()
            )
            with self._observation_connection() as connection:
                cursor = connection.execute(
                    """INSERT INTO reports
                    (operation_id, observed_state, provider_id, termination_confirmed,
                     payload_json, idempotency_key, completed) VALUES (?, ?, NULL, 0, ?, ?, 0)""",
                    (operation.operation_id, PUBLIC_FETCH_REPORT_STATE, encoded, key),
                )
                intent_id = cursor.lastrowid
        self._begin_delivery(intent_id, False)
        try:
            self._post_report(
                f"/worker/operations/{operation.operation_id}/public-fetch", payload, key
            )
        except Exception as error:
            self._delivery_failed(intent_id, error)
        with self._observation_connection() as connection:
            connection.execute("UPDATE reports SET completed=1 WHERE id=?", (intent_id,))

    def _read_build_result(self, exit_code: int | None) -> dict[str, Any]:
        """The report payload body: the strict result row, or a closed failure code."""
        policy = (self.operation_package or {}).get("build")
        if exit_code != 0:
            return {"failed": "build_exit_nonzero"}
        path = self.directory / BUILD_OUTPUT_DIRECTORY / "image-build.json"
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {"failed": "result_missing"}
        except OSError:
            return {"failed": "result_unsafe"}
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                return {"failed": "result_unsafe"}
            raw = source.read(MAX_RESULT_BYTES + 1)
        try:
            return {"result": parse_build_result(raw, policy)}
        except ImageBuildError as error:
            return {"failed": error.code}

    @property
    def operation_package(self) -> dict[str, Any] | None:
        control = (self.descriptor or {}).get("control") or {}
        return control.get("package")

    def _report_image_build(self, exit_code: int | None) -> None:
        """Post the build result row (never the image) or its failure code once, replayably."""
        if self.lease is None or self.operation is None:
            raise SupervisorError("Runtime has no bound operation.")
        if (self.operation_package or {}).get("build") is None:
            return
        operation = self.operation
        with self._observation_connection() as connection:
            done = connection.execute(
                "SELECT 1 FROM reports WHERE operation_id=? AND observed_state=? AND completed=1",
                (operation.operation_id, IMAGE_BUILD_REPORT_STATE),
            ).fetchone()
            pending = connection.execute(
                """SELECT id, payload_json, idempotency_key FROM reports
                WHERE operation_id=? AND observed_state=? AND completed=0
                ORDER BY id DESC LIMIT 1""",
                (operation.operation_id, IMAGE_BUILD_REPORT_STATE),
            ).fetchone()
        if done is not None:
            return
        if pending is not None:
            intent_id, payload, key = pending[0], json.loads(pending[1]), pending[2]
        else:
            payload = {
                "lease_id": self.lease.id,
                "expected_epoch": self.lease.epoch,
                **self._read_build_result(exit_code),
            }
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            key = (
                "runtime-image-build-"
                + hashlib.sha256(f"{operation.operation_id}:{encoded}".encode()).hexdigest()
            )
            with self._observation_connection() as connection:
                cursor = connection.execute(
                    """INSERT INTO reports
                    (operation_id, observed_state, provider_id, termination_confirmed,
                     payload_json, idempotency_key, completed) VALUES (?, ?, NULL, 0, ?, ?, 0)""",
                    (operation.operation_id, IMAGE_BUILD_REPORT_STATE, encoded, key),
                )
                intent_id = cursor.lastrowid
        self._begin_delivery(intent_id, False)
        try:
            self._post_report(
                f"/worker/operations/{operation.operation_id}/image-build", payload, key
            )
        except Exception as error:
            self._delivery_failed(intent_id, error)
        with self._observation_connection() as connection:
            connection.execute("UPDATE reports SET completed=1 WHERE id=?", (intent_id,))

    def _initialize_observation_journal(self) -> None:
        with self._observation_connection() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL, observed_state TEXT NOT NULL,
                provider_id TEXT, termination_confirmed INTEGER NOT NULL,
                payload_json TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                completed INTEGER NOT NULL CHECK (completed IN (0, 1)))"""
            )
            # A journal written before launch errors existed gains the column in place.
            columns = {row[1] for row in connection.execute("PRAGMA table_info(reports)")}
            if "launch_error" not in columns:
                connection.execute("ALTER TABLE reports ADD COLUMN launch_error TEXT")
            # The outbox state (ADR 0054) is added in place the same way.
            for name, definition in (
                ("attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("first_failed_at", "TEXT"),
                ("next_attempt_at", "TEXT"),
                ("last_error", "TEXT"),
                ("abandoned", "INTEGER NOT NULL DEFAULT 0"),
                ("response_json", "TEXT"),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE reports ADD COLUMN {name} {definition}")

    def _initialize_package_refresh_journal(self) -> None:
        with self._package_refresh_connection() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS package_refreshes (
                lease_id TEXT NOT NULL, epoch INTEGER NOT NULL, sequence INTEGER NOT NULL,
                key TEXT NOT NULL, completed INTEGER NOT NULL CHECK (completed IN (0, 1)),
                PRIMARY KEY (lease_id, epoch, sequence))"""
            )

    def _package_refresh_key(self, lease_id: str, epoch: int) -> str:
        with self._package_refresh_connection() as connection:
            row = connection.execute(
                """SELECT key FROM package_refreshes WHERE lease_id=? AND epoch=? AND completed=0
                ORDER BY sequence DESC LIMIT 1""",
                (lease_id, epoch),
            ).fetchone()
            if row is not None:
                return row[0]
            sequence = connection.execute(
                "SELECT coalesce(max(sequence), 0) + 1 FROM package_refreshes "
                "WHERE lease_id=? AND epoch=?",
                (lease_id, epoch),
            ).fetchone()[0]
            key = (
                "runtime-package-refresh-"
                + hashlib.sha256(f"v1:{lease_id}:{epoch}:{sequence}".encode()).hexdigest()
            )
            connection.execute(
                """INSERT INTO package_refreshes (lease_id, epoch, sequence, key, completed)
                VALUES (?, ?, ?, ?, 0)""",
                (lease_id, epoch, sequence, key),
            )
            return key

    def _complete_package_refresh(self, lease_id: str, epoch: int) -> None:
        with self._package_refresh_connection() as connection:
            connection.execute(
                """UPDATE package_refreshes SET completed=1
                WHERE lease_id=? AND epoch=? AND completed=0""",
                (lease_id, epoch),
            )

    def _observation_connection(self) -> sqlite3.Connection:
        return sqlite3.connect(self.directory / "runtime-observations.sqlite3")

    def _package_refresh_connection(self) -> sqlite3.Connection:
        return sqlite3.connect(self.directory / "runtime-package-refreshes.sqlite3")

    def _observation_intent(
        self,
        state: str,
        provider_id: str | None,
        termination_confirmed: bool,
        launch_error: str | None = None,
        exit_code: int | None = None,
    ) -> tuple[dict[str, Any], str, int]:
        if self.lease is None or self.operation is None:
            raise SupervisorError("Runtime has no bound operation.")
        with self._observation_connection() as connection:
            row = connection.execute(
                """SELECT id, payload_json, idempotency_key FROM reports
                WHERE operation_id=? AND observed_state=? AND provider_id IS ?
                AND termination_confirmed=? AND launch_error IS ? AND completed=0
                AND abandoned=0 ORDER BY id DESC LIMIT 1""",
                (
                    self.operation.operation_id,
                    state,
                    provider_id,
                    int(termination_confirmed),
                    launch_error,
                ),
            ).fetchone()
            if row is not None:
                return json.loads(row[1]), row[2], row[0]
            payload = {
                "lease_id": self.lease.id,
                "expected_epoch": self.lease.epoch,
                "operation_id": self.operation.operation_id,
                "observed_state": state,
                "observed_at": datetime.now(UTC).isoformat(),
                "provider_id": provider_id,
                "termination_confirmed": termination_confirmed,
            }
            if launch_error is not None:
                payload["launch_error"] = launch_error
            if exit_code is not None:
                payload["exit_code"] = exit_code
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            key = (
                "runtime-observation-"
                + hashlib.sha256(f"{self.operation.operation_id}:{encoded}".encode()).hexdigest()
            )
            cursor = connection.execute(
                """INSERT INTO reports
                (operation_id, observed_state, provider_id, termination_confirmed, payload_json,
                 idempotency_key, completed, launch_error) VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
                (
                    self.operation.operation_id,
                    state,
                    provider_id,
                    int(termination_confirmed),
                    encoded,
                    key,
                    launch_error,
                ),
            )
            return payload, key, cursor.lastrowid
