"""Opt-in retained two-Job CPU comparison against a fresh Docker Desktop lab fixture.

Creates a NEW fixture namespace and retains it and both distinct output PVCs.
No provider credentials, registry pushes, namespace reset, or automatic deletion.
Worker token is supplied as a private runtime file; never place it in a receipt.
The owner approval here is fixture-only; scientific/human review remains pending.
Separate --owner-pre-manifest-recovery uses no worker credential and only the
original retained journal/target to make an explicitly owner-attributed no-start
claim; it never submits replacement work or proves provider termination.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from check_kubernetes_lab_experiment_e2e import (
    CONTEXT,
    ROOT,
    _adopt_snapshot,
    _Api,
    _HostControlChannel,
    _kubectl,
    _load_script,
    _owner_token,
    _run,
)

from tessera_worker import docker_desktop_kubernetes as docker
from tessera_worker import kubernetes_contracts as contracts
from tessera_worker.kubernetes_dispatch_service import (
    KubernetesDispatchWorkerConfig,
    KubernetesDispatchWorkerService,
)
from tessera_worker.kubernetes_execution import KubernetesOutputBinding


class TwoVariantError(RuntimeError):
    pass


def _expected(kind: str) -> int:
    return {"baseline": 385, "intervention": 2870}[kind]


def _output(content: bytes, kind: str) -> dict[str, Any]:
    """Check actual bytes, not a fabricated value or precomputed fixture digest."""
    if not content or len(content) > 1024:
        raise TwoVariantError("collector returned no bounded output")
    try:
        value = json.loads(content, object_pairs_hook=_unique_pairs)
    except (UnicodeDecodeError, ValueError, TypeError) as error:
        raise TwoVariantError("collector output is not unambiguous JSON") from error
    if (
        not isinstance(value, dict)
        or set(value) != {"iterations", "sum_squares"}
        or type(value.get("sum_squares")) is not int
        or type(value.get("iterations")) is not int
        or value["iterations"] != (10 if kind == "baseline" else 20)
    ):
        raise TwoVariantError("collector output has no integer sum_squares")
    if value["sum_squares"] != _expected(kind):
        raise TwoVariantError("collector CPU calculation disagrees with selected variant")
    return {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


class Receipt:
    """Persist each acknowledged command under an idempotent per-fixture key."""

    def __init__(self, path: Path, namespace: str, nonce: str):
        self.path = path
        self.value: dict[str, Any] = {
            "namespace": namespace,
            "nonce": nonce,
            "steps": {},
            "failure": None,
        }
        if path.exists():
            if path.is_symlink() or path.stat().st_mode & 0o077:
                raise TwoVariantError("receipt ownership/mode is unsafe")
            existing = json.loads(path.read_text())
            if existing.get("namespace") != namespace or existing.get("nonce") != nonce:
                raise TwoVariantError("receipt belongs to another fixture identity")
            self.value = existing
        self.save()

    def save(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = self.path.parent / f".{self.path.name}.{os.getpid()}.tmp"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump(self.value, output, sort_keys=True, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    def record(self, name: str, value: dict[str, Any]) -> dict[str, Any]:
        if name in self.value["steps"] and self.value["steps"][name] != value:
            raise TwoVariantError("retained command response changed across replay")
        self.value["steps"][name] = value
        self.value["last_step"] = name
        self.save()
        return value

    def call(
        self, client: _Api, name: str, method: str, path: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        if name in self.value["steps"]:
            return self.value["steps"][name]
        # Unknown response is not authority to change key/body or reset state.
        value = client.request(method, path, body, key=f"two-variant-{self.value['nonce']}-{name}")
        return self.record(name, value)


class _PostJobLeaseKeeper:
    """Keep the original worker lease current while collecting and sealing output.

    The private, separate journal makes an uncertain renewal replay the *same*
    key after a process restart. It is never a way to reacquire an expired lease.
    """

    def __init__(
        self,
        path: Path,
        namespace: str,
        nonce: str,
        lease: dict[str, Any],
        worker: _Api,
        *,
        clock=None,
        interval: float = 8.0,
    ) -> None:
        self.lease = lease
        self.worker = worker
        self.clock = clock or (lambda: datetime.now(UTC))
        self.interval = interval
        self.journal = Receipt(path, namespace, nonce + "-" + lease["id"])
        self.journal.record("identity", {"lease_id": lease["id"], "epoch": lease["epoch"]})
        steps = self.journal.value["steps"]
        completed = sorted(
            int(name.removeprefix("renew-")) for name in steps if re.fullmatch(r"renew-\d+", name)
        )
        if completed != list(range(len(completed))):
            raise TwoVariantError("post-Job lease journal has missing renewals")
        self.sequence = len(completed)
        previous = steps.get(f"renew-{self.sequence - 1}", lease)
        self.expires_at = self._date(previous["expires_at"])
        self.hard_expires_at = self._date(lease["hard_expires_at"])
        self.failure: BaseException | None = None
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.thread: threading.Thread | None = None

    @staticmethod
    def _date(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise TwoVariantError("lease deadline has no timezone")
        return parsed

    def check(self) -> None:
        if self.failure is not None:
            raise TwoVariantError(
                "post-Job lease renewal failed; do not upload output"
            ) from self.failure
        # Leave a guard interval for an in-flight HTTP request, rather than
        # treating a near-expiry lease as safe authority for a new effect.
        if self.clock() + timedelta(seconds=2) >= min(self.expires_at, self.hard_expires_at):
            raise TwoVariantError("post-Job lease expired or is too close to its hard limit")

    def renew(self) -> None:
        self.check()
        step = f"renew-{self.sequence}"
        body = {"lease_id": self.lease["id"], "expected_epoch": self.lease["epoch"]}
        self.journal.record(step + "-intent", body)
        response = self.journal.call(self.worker, step, "POST", "/worker/leases/renew", body)
        if (
            response.get("id") != self.lease["id"]
            or response.get("epoch") != self.lease["epoch"]
            or response.get("hard_expires_at") != self.lease["hard_expires_at"]
        ):
            raise TwoVariantError("lease renewal changed its original authority")
        updated = self._date(response["expires_at"])
        if updated > self.hard_expires_at or updated <= self.clock() + timedelta(seconds=2):
            raise TwoVariantError("renewed lease is expired or exceeds its hard limit")
        self.expires_at = updated
        self.sequence += 1

    def _loop(self) -> None:
        try:
            self.renew()  # Start immediately, before any blocking Pod wait.
            self.ready.set()
            while not self.stop_event.wait(self.interval):
                self.renew()
        except BaseException as error:
            self.failure = error
            self.ready.set()

    def __enter__(self):
        self.check()
        self.thread = threading.Thread(target=self._loop, name="post-job-lease", daemon=True)
        self.thread.start()
        # Never proceed to collection on an unacknowledged/uncertain renewal.
        if not self.ready.wait(timeout=12):
            self.stop_event.set()
            self.thread.join(timeout=15)
            raise TwoVariantError("initial post-Job lease renewal did not complete")
        self.check()
        return self

    def __exit__(self, *_exc):
        self.stop_event.set()
        assert self.thread is not None
        self.thread.join(timeout=15)
        if self.thread.is_alive():
            raise TwoVariantError("post-Job lease thread did not stop")


def _worker_lease_deadline(journal: Path, dispatch_id: str, lease: dict) -> dict:
    """Recover the worker's last acknowledged renewal after its terminal report."""
    if not journal.is_file() or journal.is_symlink():
        raise TwoVariantError("worker supervision journal is missing")
    with sqlite3.connect(f"file:{journal}?mode=ro", uri=True) as db:
        row = db.execute(
            "SELECT lease_expires_at FROM dispatch_supervision WHERE dispatch_id=?", (dispatch_id,)
        ).fetchone()
    if row is None:
        raise TwoVariantError("worker has no original lease supervision")
    expiry = _PostJobLeaseKeeper._date(row[0])
    if not (
        _PostJobLeaseKeeper._date(lease["expires_at"])
        <= expiry
        <= _PostJobLeaseKeeper._date(lease["hard_expires_at"])
    ):
        raise TwoVariantError("worker lease supervision exceeds issued authority")
    return {**lease, "expires_at": expiry.isoformat()}


def _require_no_cancelled_variant(journal: Path) -> None:
    if not journal.exists():
        return
    with sqlite3.connect(f"file:{journal}?mode=ro", uri=True) as connection:
        cancelled = connection.execute(
            "SELECT count(*) FROM kubernetes_execution_bindings WHERE cancellation_requested=1"
        ).fetchone()[0]
    if cancelled:
        raise TwoVariantError(
            "earlier variant dispatch is cancelled without terminal proof; "
            "do not replay or issue another grant"
        )


def _wait_for_variant_pvc(
    live: Any, namespace: str, name: str, uid: str, spec_digest: str, timeout: int
) -> None:
    """Verify this variant's PVC, not the old single-Job `outputs` claim."""
    expected_labels = docker._pvc(namespace)["metadata"]["labels"]
    deadline = time.monotonic() + timeout
    while True:
        pvc = live._get("pvc", name, namespace)
        if (
            docker._uid(pvc, "PersistentVolumeClaim", name, namespace) != uid
            or docker._pvc_spec_sha256(pvc) != spec_digest
            or any(
                pvc.get("metadata", {}).get("labels", {}).get(k) != v
                for k, v in expected_labels.items()
            )
        ):
            raise TwoVariantError("variant output PVC identity changed")
        if pvc.get("status", {}).get("phase") == "Bound":
            return
        if time.monotonic() >= deadline:
            raise TwoVariantError("variant output PVC did not bind")
        time.sleep(0.5)


def _resume_terminal_binding(
    journal: Path, dispatch: dict, recorded: dict, cluster: Any
) -> contracts.KubernetesJobBinding:
    """Use the original successful Job journal, never re-claim a resolved dispatch."""
    if not journal.is_file() or journal.is_symlink():
        raise TwoVariantError("original successful Job journal is missing")
    with sqlite3.connect(f"file:{journal}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        adapter = db.execute(
            "SELECT * FROM kubernetes_execution_bindings WHERE operation_id=?",
            (dispatch["operation_id"],),
        ).fetchone()
        job = db.execute(
            "SELECT * FROM kubernetes_jobs WHERE operation_id=?",
            (dispatch["operation_id"],),
        ).fetchone()
    if adapter is None or job is None:
        raise TwoVariantError("recorded Job has no original durable binding")
    intent = json.loads(adapter["intent_json"])
    binding = contracts.KubernetesJobBinding(
        **{**intent["binding"], "command": tuple(intent["binding"]["command"])}
    )
    if (
        intent["target_digest"] != contracts.cluster_identity_digest(cluster)
        or intent["package_digest"] != dispatch["package_digest"]
        or binding.operation_id != dispatch["operation_id"]
        or binding.package_sha256 != dispatch["package_digest"]
        or binding.job_name != recorded["name"]
        or adapter["namespace"] != cluster.namespace
        or adapter["namespace_uid"] != cluster.namespace_uid
        or adapter["job_name"] != binding.job_name
        or adapter["pvc_name"] != binding.output_pvc
        or adapter["pvc_uid"] != binding.output_pvc_uid
        or adapter["pvc_spec_sha256"] != binding.output_pvc_spec_sha256
        or job["binding_json"] != contracts.canonical_json(binding.__dict__).decode()
        or job["job_uid"] != adapter["job_uid"]
        or job["job_uid"] != recorded["uid"]
        or job["create_intent"] != 1
        or job["cancellation_requested"] != 0
        or adapter["cancellation_requested"] != 0
        or adapter["observed_state"] != "succeeded"
    ):
        raise TwoVariantError("recorded Job conflicts with original terminal execution")
    return binding


def _owner_pre_manifest_absence_proof(
    live: Any,
    journal_path: Path,
    cluster: contracts.KubernetesClusterConfig,
    operation_id: str,
    package_digest: str,
) -> dict[str, Any]:
    """Only the retained original journal and exact target may attest no-start.

    The four callbacks perform real, bounded Kubernetes reads when called with
    DockerDesktopKubernetesAdapter. Tests inject a fake *only* in isolated tests.
    No worker credential is used, and no Job, Pod, or namespace is modified.
    """
    if (
        journal_path.is_symlink()
        or not journal_path.is_file()
        or journal_path.stat().st_mode & 0o077
        or journal_path.parent.is_symlink()
        or journal_path.parent.stat().st_mode & 0o077
    ):
        raise TwoVariantError("original worker journal is missing or not private")
    with sqlite3.connect(f"file:{journal_path}?mode=ro", uri=True) as connection:
        row = connection.execute(
            "SELECT intent_json FROM kubernetes_execution_bindings WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
    if row is None:
        raise TwoVariantError("original adapter operation intent is missing")
    intent = json.loads(row[0])
    if (
        intent.get("package_digest") != package_digest
        or intent.get("target_digest") != contracts.cluster_identity_digest(cluster)
        or not isinstance(intent.get("binding"), dict)
    ):
        raise TwoVariantError("retained adapter intent conflicts with original authority")
    binding = contracts.KubernetesJobBinding(**intent["binding"])
    if binding.operation_id != operation_id or binding.package_sha256 != package_digest:
        raise TwoVariantError("retained Job binding conflicts with original operation")

    def listed(kind: str, exact_operation: str) -> list[dict[str, Any]]:
        if exact_operation != operation_id:
            raise TwoVariantError("provider list selector is not the original operation")
        selector = "tessera.dev/operation=" + contracts.kubernetes_resource_name(
            operation_id, "operation_id"
        )
        raw = live._kubectl(
            "get",
            kind,
            "--selector",
            selector,
            "-o",
            "json",
            namespace=cluster.namespace,
            timeout=10,
        ).stdout
        value = json.loads(raw)
        if not isinstance(value, dict) or not isinstance(value.get("items"), list):
            raise TwoVariantError("Kubernetes operation-labelled list was incomplete")
        return value["items"]

    return contracts.KubernetesJobJournal(journal_path).attest_pre_manifest_never_submitted(
        binding,
        cluster,
        inspect_namespace=lambda name: live._get("namespace", name, None),
        inspect_job=lambda name: live._maybe_get("job", name, cluster.namespace),
        list_operation_jobs=lambda op: listed("jobs", op),
        list_operation_pods=lambda op: listed("pods", op),
    )


def run_owner_pre_manifest_recovery(args: argparse.Namespace) -> dict[str, Any]:
    """Opt-in, owner-attributed recovery for the original expired-worker baseline.

    Does not bootstrap, repair, refresh credentials, recreate worker authority,
    delete resources, or submit a replacement. The owner must assert continuity
    of the retained journal; control cannot independently inspect its bytes.
    """
    restart = _load_script("two_variant_restart", "check_kubernetes_control_plane_restart.py")
    authenticated = _load_script(
        "two_variant_dispatch", "check_authenticated_kubernetes_dispatch_e2e.py"
    )
    kubeconfig = args.kubeconfig.resolve(strict=True)
    if args.receipt is None or not args.receipt.is_file() or args.receipt.stat().st_mode & 0o077:
        raise TwoVariantError("private original fixture receipt is required")
    previous = json.loads(args.receipt.read_text())
    if previous.get("namespace") != args.namespace or not isinstance(previous.get("nonce"), str):
        raise TwoVariantError("original receipt namespace identity is missing")
    receipt = Receipt(args.receipt, args.namespace, previous["nonce"])
    steps = receipt.value["steps"]
    dispatch = steps["baseline-dispatch"]
    response = steps["baseline-package"]
    grant_receipt = steps["baseline-grant"]
    if (
        steps["context"]["name"] != CONTEXT
        or steps["context"]["kubeconfig_sha256"]
        != hashlib.sha256(kubeconfig.read_bytes()).hexdigest()
        or dispatch["grant_id"] != grant_receipt["id"]
        or dispatch["operation_id"] != response["operation"]["operation_id"]
        or dispatch["package_digest"] != response["package_digest"]
        or response["package"]["grant_id"] != dispatch["grant_id"]
        or response["package"]["lease_id"] != dispatch["lease_id"]
        or response["package"]["epoch"] != dispatch["epoch"]
        or "baseline-result" in steps
        or "intervention-result" in steps
    ):
        raise TwoVariantError("retained baseline authority conflicts with no-start recovery")
    target = docker.target_from_kubeconfig(kubeconfig)
    live = docker.DockerDesktopKubernetesAdapter(target, args.receipt.with_suffix(".sqlite3"))
    namespace_uid = docker._uid(
        live._get("namespace", args.namespace, None), "Namespace", args.namespace, None
    )
    if namespace_uid != steps["namespace"]["uid"]:
        raise TwoVariantError("original namespace UID changed")
    cluster = contracts.KubernetesClusterConfig(
        kubeconfig=str(target.kubeconfig),
        kubeconfig_sha256=target.kubeconfig_sha256,
        context=target.context,
        profile="development-minikube-opencode-v1",
        server=target.server,
        server_sha256=hashlib.sha256(target.server.encode()).hexdigest(),
        ca_sha256=target.ca_sha256,
        namespace=args.namespace,
        cluster_uid=docker._uid(
            live._get("namespace", "kube-system", None), "Namespace", "kube-system", None
        ),
        namespace_uid=namespace_uid,
        network_none_evidence=None,
    )
    if contracts.cluster_identity_digest(cluster) != dispatch["target_digest"]:
        raise TwoVariantError("original cluster target digest changed")
    port = authenticated._port()
    forward = restart._start_port_forward(kubeconfig, args.namespace, port)
    try:
        restart._wait_ready(port)
        if forward.poll() is not None:
            raise TwoVariantError("original control port forward exited")
        owner = _Api(f"http://127.0.0.1:{port}", _owner_token(kubeconfig, args.namespace))
        grant = owner.request("GET", f"/execution-grants/{dispatch['grant_id']}")
        completed = steps.get("baseline-owner-pre-manifest-recovery-result")
        pending = steps.get("baseline-owner-pre-manifest-recovery-intent")
        if completed is not None:
            if (
                grant["status"] != "reconciled"
                or grant["settlement_cents"] != 0
                or completed.get("grant_id") != dispatch["grant_id"]
                or completed.get("operation_id") != dispatch["operation_id"]
                or pending is None
                or grant["revision"] != pending["expected_revision"] + 1
            ):
                raise TwoVariantError("retained owner recovery and current grant conflict")
            return completed
        if grant["id"] != dispatch["grant_id"] or grant["status"] not in {
            "cancel_requested",
            "reconciled",
        }:
            raise TwoVariantError("original grant is not a cancelled unresolved authority")
        journal_path = (
            args.receipt.parent / f"{args.namespace}-worker-journal" / "baseline-jobs.sqlite3"
        )
        if pending is None:
            if grant["status"] != "cancel_requested":
                raise TwoVariantError("reconciled grant has no retained owner recovery intent")
            proof = _owner_pre_manifest_absence_proof(
                live, journal_path, cluster, dispatch["operation_id"], dispatch["package_digest"]
            )
            pending = receipt.record(
                "baseline-owner-pre-manifest-recovery-intent",
                proof
                | {
                    "expected_revision": grant["revision"],
                    "owner_attests_original_exclusive_journal": True,
                },
            )
        elif (
            not isinstance(pending, dict)
            or type(pending.get("expected_revision")) is not int
            or (
                pending["expected_revision"] != grant["revision"]
                if grant["status"] == "cancel_requested"
                else pending["expected_revision"] + 1 != grant["revision"]
            )
            or pending.get("operation_id") != dispatch["operation_id"]
            or pending.get("target_digest") != dispatch["target_digest"]
            or (
                grant["status"] == "reconciled"
                and (
                    grant["settlement_cents"] != 0
                    or grant["reconciled_by_principal_id"] != grant["created_by_principal_id"]
                )
            )
        ):
            raise TwoVariantError("pending owner recovery body conflicts with original authority")
        recovered = owner.request(
            "POST",
            f"/execution-grants/{dispatch['grant_id']}/reconcile-owner-pre-manifest-no-start",
            pending,
            key=f"two-variant-{receipt.value['nonce']}-baseline-owner-pre-manifest-recovery",
        )
        if recovered.get("status") != "reconciled" or recovered.get("settlement_cents") != 0:
            raise TwoVariantError("owner no-start API did not reconcile at zero settlement")
        return receipt.record(
            "baseline-owner-pre-manifest-recovery-result",
            {
                "grant_id": grant["id"],
                "operation_id": dispatch["operation_id"],
                "status": recovered["status"],
                "settlement_cents": 0,
                "proof_kind": "pre_manifest",
                "attribution": "owner_attested_not_independent",
                "provider_termination_inferred": False,
            },
        )
    finally:
        restart._stop_port_forward(forward)


def _variant(
    kind: str,
    owner: _Api,
    worker: _Api,
    receipt: Receipt,
    project: dict,
    snapshot: dict,
    credential: dict,
    experiment: dict,
    cluster: Any,
    live: Any,
    authenticated: Any,
    image: Any,
    port: int,
    namespace: str,
    token_file: Path,
    worker_root: Path,
    timeout: int,
    stack: ExitStack,
) -> dict[str, Any]:
    prefix = kind + "-"
    if not cluster.network_none_evidence or not cluster.network_none_evidence.is_fresh_for(
        cluster.cluster_uid, cluster.namespace_uid
    ):
        raise TwoVariantError("network-none enforcement evidence expired before execution grant")
    previous = receipt.value["steps"].get(prefix + "result")
    if previous:
        grant = owner.request("GET", f"/execution-grants/{previous['grant_id']}")
        job = live._get("job", previous["job_name"], namespace)
        collector = live._get("pod", previous["collector_name"], namespace)
        exported = owner.request("GET", f"/experiments/{experiment['id']}")
        if (
            grant.get("status") != "reconciled"
            or docker._uid(job, "Job", previous["job_name"], namespace) != previous["job_uid"]
            or docker._uid(collector, "Pod", previous["collector_name"], namespace)
            != previous["collector_uid"]
            or not any(
                m.get("id") == previous["measurement_id"] for m in exported.get("measurements", [])
            )
        ):
            raise TwoVariantError("retained terminal variant identities changed")
        return previous
    journal = worker_root / f"{kind}-jobs.sqlite3"
    _require_no_cancelled_variant(journal)

    def human(name, method, path, body):
        return receipt.call(owner, prefix + name, method, path, body)

    def service(name, path, body):
        if name in {"artifact", "placement", "manifest"} and keeper is not None:
            keeper.check()
        return receipt.call(worker, prefix + name, "POST", path, body)

    keeper: _PostJobLeaseKeeper | None = None
    grant = human(
        "grant",
        "POST",
        f"/projects/{project['id']}/execution-grants",
        {
            "task_id": receipt.value["steps"][prefix + "approval"]["id"],
            "expected_task_revision": receipt.value["steps"][prefix + "approval"]["revision"],
            "expected_plan_digest": receipt.value["steps"][prefix + "approval"][
                "current_plan_digest"
            ],
            "worker_principal_id": credential["principal_id"],
            "snapshot_id": snapshot["id"],
            "pinned_image": image.reference,
            "expires_at": receipt.value["steps"][prefix + "expiry"]["at"],
            "max_exposure_cents": 0,
            "currency": "USD",
            "acknowledges_unmeasured_infrastructure": True,
        },
    )
    design = next(item for item in experiment["design"]["variants"] if item["kind"] == kind)
    admitted = human(
        "admit",
        "POST",
        f"/experiment-variants/{design['id']}/admitted-attempts",
        {
            "expected_variant_revision": design["revision"],
            "expected_design_revision": experiment["current_design_revision"],
            "grant_id": grant["id"],
        },
    )
    service("heartbeat", "/worker/heartbeat", {})
    lease = service("lease", "/worker/leases", {"grant_id": grant["id"]})
    service(
        "package",
        f"/worker/leases/{lease['id']}/package",
        {
            "expected_epoch": lease["epoch"],
            "execution_adapter": "kubernetes-job",
            "admitted_attempt_id": admitted["id"],
        },
    )
    current = next(
        item
        for item in owner.request("GET", f"/experiments/{experiment['id']}")["admitted_attempts"]
        if item["id"] == admitted["id"]
    )
    dispatch = human(
        "dispatch",
        "POST",
        f"/experiment-admitted-attempts/{admitted['id']}/dispatch",
        {
            "expected_revision": current["revision"],
            "target_digest": contracts.cluster_identity_digest(cluster),
            "profile_version": "development-minikube-opencode-v1",
        },
    )
    pvc_name = f"outputs-{kind}"
    pvc = live._get("pvc", pvc_name, namespace)
    pvc_uid = docker._uid(pvc, "PersistentVolumeClaim", pvc_name, namespace)
    pvc_spec = docker._pvc_spec_sha256(pvc)
    adapter = authenticated._LiveAdapter(
        live, cluster, journal, {"tessera.io/lab-experiment-fixture": receipt.value["nonce"]}
    )
    worker_dir = worker_root / kind
    worker_dir.mkdir(mode=0o700, exist_ok=True)

    def run_worker(*, max_cycles: int | None = None):
        return KubernetesDispatchWorkerService(
            KubernetesDispatchWorkerConfig(
                f"http://127.0.0.1:{port}", dispatch["id"], token_file, worker_dir
            ),
            cluster,
            KubernetesOutputBinding(pvc_name, "/outputs", pvc_uid, pvc_spec),
            adapter,
            channel=_HostControlChannel(f"http://127.0.0.1:{port}", worker.token),
            poll_seconds=0.5,
        ).run(max_cycles=max_cycles)

    previous_job = receipt.value["steps"].get(prefix + "job")
    if previous_job:
        binding = _resume_terminal_binding(journal, dispatch, previous_job, cluster)
        job_uid = previous_job["uid"]
    else:
        status = run_worker()
        if status.get("status") != "succeeded" or adapter.package is None:
            raise TwoVariantError(f"{kind} worker did not observe a succeeded Kubernetes Job")
        binding = adapter.package.binding
        job_uid = docker._uid(
            live._get("job", binding.job_name, namespace), "Job", binding.job_name, namespace
        )
        receipt.record(
            prefix + "job",
            {"name": binding.job_name, "uid": job_uid, "operation_id": binding.operation_id},
        )
        run_worker(max_cycles=1)  # durable replay must retain Job UID; never issue a second Job
    # The worker stops renewing on acknowledged terminal status. Use its durable
    # last expiry and the SAME lease/worker; the server remains authoritative.
    current_lease = _worker_lease_deadline(
        worker_dir / "kubernetes-claims.sqlite3", dispatch["id"], lease
    )
    keeper = stack.enter_context(
        _PostJobLeaseKeeper(
            worker_dir / "post-job-lease.json",
            namespace,
            receipt.value["nonce"] + "-" + kind,
            current_lease,
            _Api(worker.origin, worker.token),
        )
    )
    if (
        docker._uid(
            live._get("job", binding.job_name, namespace), "Job", binding.job_name, namespace
        )
        != job_uid
    ):
        raise TwoVariantError("worker replay changed Job UID")
    jobs = json.loads(
        live._kubectl(
            "get",
            "jobs",
            "-l",
            f"tessera.dev/operation={binding.operation_id}",
            "-o",
            "json",
            namespace=namespace,
        ).stdout
    ).get("items", [])
    if len(jobs) != 1:
        raise TwoVariantError("worker replay produced duplicate Jobs")
    _wait_for_variant_pvc(live, namespace, pvc_name, pvc_uid, pvc_spec, min(timeout, 60))
    keeper.check()
    pods = live._owned_pods(binding, cluster, job_uid)
    if not pods or any(pod.state != "succeeded" for pod in pods):
        raise TwoVariantError("owned Job Pods have no successful terminal evidence")
    collector = f"tessera-collector-{binding.operation_id}"
    live._require_collector_authorization(namespace)
    manifest = docker._collector_manifest(namespace, collector, image.reference)
    manifest["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = pvc_name
    if live._maybe_get("pod", collector, namespace) is None:
        live._create(manifest, namespace)
    live._kubectl(
        "wait",
        "--for=jsonpath={.status.phase}=Succeeded",
        f"pod/{collector}",
        "--timeout=60s",
        namespace=namespace,
        timeout=70,
    )
    keeper.check()
    collector_uid, _ = docker._verify_collector(
        live._get("pod", collector, namespace), namespace, collector, image, pvc_name=pvc_name
    )
    content = live._kubectl(
        "logs", f"pod/{collector}", "--limit-bytes=1024", namespace=namespace
    ).stdout.encode()
    output = _output(content, kind)
    keeper.check()
    receipt.record(prefix + "collector", {"uid": collector_uid, **output})
    artifact = service(
        "artifact",
        f"/worker/leases/{lease['id']}/artifacts",
        {
            "expected_epoch": lease["epoch"],
            "operation_id": binding.operation_id,
            "relative_path": "result.txt",
            "sha256": output["sha256"],
            "content_base64": base64.b64encode(content).decode(),
        },
    )
    if artifact.get("sha256") != output["sha256"] or artifact.get("size") != output["size"]:
        raise TwoVariantError("retained artifact disagrees with collected output")
    if artifact.get("late") is not False:
        raise TwoVariantError("artifact was not uploaded within live lease authority")
    placement = service(
        "placement",
        f"/worker/leases/{lease['id']}/project-artifacts",
        {
            "expected_epoch": lease["epoch"],
            "operation_id": binding.operation_id,
            "artifacts": [
                {"id": artifact["id"], "sha256": output["sha256"], "size": output["size"]}
            ],
        },
    )
    if not any(p.get("artifact_id") == artifact["id"] for p in placement.get("placements", [])):
        raise TwoVariantError("project output placement missing")
    manifest_record = service(
        "manifest",
        f"/worker/leases/{lease['id']}/artifact-manifest",
        {
            "expected_epoch": lease["epoch"],
            "operation_id": binding.operation_id,
            "artifacts": [
                {
                    "id": artifact["id"],
                    "relative_path": "result.txt",
                    "sha256": output["sha256"],
                    "size": output["size"],
                }
            ],
        },
    )
    if (manifest_record.get("status"), manifest_record.get("classification")) != (
        "quarantined",
        "unclassified",
    ):
        raise TwoVariantError("output was not quarantined")
    if manifest_record.get("late") is not False:
        raise TwoVariantError("manifest contains late output")
    keeper.check()
    latest = next(
        item
        for item in owner.request("GET", f"/experiments/{experiment['id']}")["admitted_attempts"]
        if item["id"] == admitted["id"]
    )
    linked = human(
        "link",
        "POST",
        f"/experiment-attempts/{admitted['id']}/artifacts",
        {
            "expected_revision": latest["revision"],
            "artifact_ids": [artifact["id"]],
        },
    )
    keeper.check()
    measurement = human(
        "derive",
        "POST",
        f"/experiment-attempts/{admitted['id']}/derived-measurements",
        {
            "expected_revision": linked["revision"],
            "name": "sum_squares",
            "artifact_id": artifact["id"],
            "json_pointer": "/sum_squares",
        },
    )
    if (
        not isinstance(measurement.get("value"), dict)
        or measurement["value"].get("number") != _expected(kind)
        or measurement.get("artifact_id") != artifact["id"]
    ):
        raise TwoVariantError("server-derived numeric value does not match actual collector output")
    keeper.check()
    # Lease renewal is no longer needed for human review/grant reconciliation.
    stack.close()
    terminal = owner.request("GET", f"/experiments/{experiment['id']}")
    attempt = next(item for item in terminal["admitted_attempts"] if item["id"] == admitted["id"])
    if attempt.get("outcome") is None:
        human(
            "resolve",
            "POST",
            f"/experiment-admitted-attempts/{admitted['id']}/resolve",
            {
                "expected_revision": attempt["revision"],
                "outcome": "succeeded",
            },
        )
    grant_now = owner.request("GET", f"/execution-grants/{grant['id']}")
    settled = human(
        "reconcile",
        "POST",
        f"/execution-grants/{grant['id']}/reconcile",
        {
            "expected_revision": grant_now["revision"],
            "settlement_cents": 0,
            "provider_terminated": True,
        },
    )
    if settled.get("status") != "reconciled":
        raise TwoVariantError("grant remains unresolved; second grant must not be issued")
    result = {
        "attempt_id": admitted["id"],
        "dispatch_id": dispatch["id"],
        "operation_id": binding.operation_id,
        "job_uid": job_uid,
        "collector_uid": collector_uid,
        "pvc_uid": pvc_uid,
        "artifact_id": artifact["id"],
        "sha256": output["sha256"],
        "sum_squares": measurement["value"],
        "measurement_id": measurement["id"],
        "grant_id": grant["id"],
        "grant_status": settled["status"],
        "job_name": binding.job_name,
        "collector_name": collector,
    }
    return receipt.record(prefix + "result", result)


def _serial_variants(run_one, receipt: Receipt) -> dict[str, dict[str, Any]]:
    """Enforce the single-grant limit across the two actual terminal executions."""
    results = {}
    for kind in ("baseline", "intervention"):
        name = kind + "-expiry"
        if name not in receipt.value["steps"]:
            receipt.record(name, {"at": (datetime.now(UTC) + timedelta(minutes=20)).isoformat()})
        result = run_one(kind)
        if result.get("grant_status") != "reconciled" or result.get("sum_squares", {}).get(
            "number"
        ) != _expected(kind):
            raise TwoVariantError("unresolved grant or incorrect server-derived measurement")
        results[kind] = result
    if results["baseline"]["sha256"] == results["intervention"]["sha256"]:
        raise TwoVariantError("baseline and intervention output digests unexpectedly match")
    return results


def _bootstrap_nonce(receipt_path: Path, occupied: bool) -> str:
    if not receipt_path.exists():
        if occupied:
            raise TwoVariantError("fixture namespace already exists; no adoption or reset")
        return uuid.uuid4().hex[:12]
    if receipt_path.is_symlink() or receipt_path.stat().st_mode & 0o077:
        raise TwoVariantError("existing receipt is not a private regular file")
    previous = json.loads(receipt_path.read_text())
    steps = previous.get("steps", {})
    if occupied and not steps.get("namespace", {}).get("uid"):
        raise TwoVariantError("partial bootstrap needs operator UID reconciliation")
    if not occupied and (
        set(steps)
        - {
            "context",
            "resume-runner",
            "workload-image",
            "prebuilt-control-image",
            "prebuilt-workload-image",
        }
        - {key for key in steps if key.startswith("resume-runner-")}
        - {key for key in steps if key.startswith("prebuilt-control-image-revision-")}
    ):
        raise TwoVariantError("missing fixture namespace needs operator UID reconciliation")
    return previous["nonce"]


def _control_build_sources(root: Path) -> str:
    """Fence reuse of an explicitly prebuilt host image to unchanged build inputs."""
    digest = hashlib.sha256()
    paths = [root / name for name in ("Dockerfile", "pyproject.toml", "uv.lock", "README.md")]
    paths.extend(
        sorted(
            path
            for path in (root / "src").rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
    )
    for path in paths:
        digest.update(str(path.relative_to(root)).encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _workload_build_sources(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((root / "examples" / "kubernetes-hello").iterdir()):
        if path.is_file():
            digest.update(path.name.encode() + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _retain_network_proof(receipt: Receipt, proof: dict[str, str]) -> datetime:
    prior = receipt.value["steps"].get("network-proof")
    if prior is not None:
        proof["observed_at"] = prior["observed_at"]
    receipt.record("network-proof", proof)
    return datetime.fromisoformat(proof["observed_at"])


def _fixture_manifest(restart: Any, namespace: str, nonce: str, image: str) -> list[dict]:
    """Make the existing lab template's two trusted-host arguments concrete."""
    documents = list(yaml.safe_load_all(restart._manifest(namespace, nonce, image)))
    config = next(
        doc
        for doc in documents
        if doc.get("kind") == "ConfigMap"
        and doc.get("metadata", {}).get("name") == "tessera-control-remote"
    )
    if config["data"].get("TESSERA_TRUSTED_HOST") != "localhost":
        raise TwoVariantError("fixture trusted host has changed unexpectedly")
    config["data"]["TESSERA_TLS_GATEWAY_HOST"] = "localhost"
    deployment = next(
        doc
        for doc in documents
        if doc.get("kind") == "Deployment" and doc.get("metadata", {}).get("name") == "control"
    )
    template = deployment["spec"]["template"]
    template.setdefault("metadata", {}).setdefault("annotations", {})[
        "tessera.io/fixture-control-config"
    ] = hashlib.sha256(json.dumps(config["data"], sort_keys=True).encode()).hexdigest()
    return documents


def _reconcile_owned_control(
    restart: Any,
    kubeconfig: Path,
    namespace: str,
    nonce: str,
    documents: list[dict],
    receipt: Receipt,
) -> None:
    """Only patch this already-owned fixture's ConfigMap and Deployment."""
    recorded = receipt.value["steps"]["namespace"]["uid"]
    if restart._namespace_uid(kubeconfig, namespace, nonce) != recorded:
        raise TwoVariantError("fixture namespace UID changed before control recovery")
    owned: dict[str, str] = {}
    for kind, name in (("ConfigMap", "tessera-control-remote"), ("Deployment", "control")):
        resource = json.loads(
            _run(_kubectl(kubeconfig, namespace) + ["get", kind, name, "-o", "json"], timeout=20)
        )
        metadata = resource.get("metadata", {})
        if (
            metadata.get("namespace") != namespace
            or metadata.get("labels", {}).get(restart.LABEL) != nonce
            or not metadata.get("uid")
        ):
            raise TwoVariantError(f"fixture {kind} ownership unavailable")
        owned[kind] = metadata["uid"]
    receipt.record("control-reconcile-identity", owned)
    target = [doc for doc in documents if doc.get("kind") in owned]
    _run(
        _kubectl(kubeconfig, namespace) + ["apply", "-f", "-"],
        input_bytes=yaml.safe_dump_all(target, sort_keys=False).encode(),
    )
    for kind, name in (("ConfigMap", "tessera-control-remote"), ("Deployment", "control")):
        resource = json.loads(
            _run(_kubectl(kubeconfig, namespace) + ["get", kind, name, "-o", "json"], timeout=20)
        )
        if resource.get("metadata", {}).get("uid") != owned[kind]:
            raise TwoVariantError(f"fixture {kind} UID changed during control recovery")


def _network_evidence(
    live: Any,
    authenticated: Any,
    namespace: str,
    nonce: str,
    receipt: Receipt,
    cluster_uid: str,
    namespace_uid: str,
    image: Any,
    policy: dict,
) -> contracts.KubernetesNetworkPolicyEvidence:
    """Observe real allowed/denied traffic before admitting network-none CPU work."""
    labels = {"tessera.io/lab-experiment-fixture": nonce}
    server_labels = {
        "app.kubernetes.io/managed-by": "tessera",
        "tessera.io/connectivity-role": "server",
    }
    allowed_labels = {
        "app.kubernetes.io/managed-by": "tessera",
        "tessera.io/connectivity-role": "allowed",
    }

    def ensure(manifest: dict, resource: str, name: str, step: str) -> dict:
        existing = live._maybe_get(resource, name, namespace)
        if existing is None:
            live._create(authenticated._fixture_owned_manifest(manifest, labels), namespace)
        observed = live._get(resource, name, namespace)
        uid = docker._uid(observed, manifest["kind"], name, namespace)
        if (
            observed.get("metadata", {}).get("labels", {}).get("tessera.io/lab-experiment-fixture")
            != nonce
        ):
            raise TwoVariantError("network probe resource is not fixture-owned")
        receipt.record(step, {"uid": uid})
        return observed

    for name, selector, rule, direction in (
        (
            "two-variant-allow-probe-ingress",
            server_labels,
            [
                {
                    "from": [{"podSelector": {"matchLabels": allowed_labels}}],
                    "ports": [{"protocol": "TCP", "port": 8080}],
                }
            ],
            "Ingress",
        ),
        (
            "two-variant-allow-probe-egress",
            allowed_labels,
            [
                {
                    "to": [{"podSelector": {"matchLabels": server_labels}}],
                    "ports": [{"protocol": "TCP", "port": 8080}],
                }
            ],
            "Egress",
        ),
    ):
        spec = {
            "podSelector": {"matchLabels": selector},
            "policyTypes": [direction],
            direction.lower(): rule,
        }
        observed = ensure(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": name, "namespace": namespace},
                "spec": spec,
            },
            "networkpolicy",
            name,
            name,
        )
        if contracts.canonical_json(observed.get("spec")) != contracts.canonical_json(spec):
            raise TwoVariantError("network probe policy spec changed")

    server_name = "two-variant-network-server"
    server = ensure(
        authenticated._network_probe_pod(
            namespace, server_name, "server", image.reference, ["--http-server"], labels
        ),
        "pod",
        server_name,
        "network-server",
    )
    live._kubectl(
        "wait",
        "--for=condition=Ready",
        f"pod/{server_name}",
        "--timeout=30s",
        namespace=namespace,
        timeout=40,
    )
    server_ip = live._get("pod", server_name, namespace).get("status", {}).get("podIP")
    if not isinstance(server_ip, str) or not server_ip:
        raise TwoVariantError("network probe server has no Pod IP")
    observations = {}
    probe_times = []
    for outcome in ("allowed", "denied"):
        name = f"two-variant-network-{outcome}"
        ensure(
            authenticated._network_probe_pod(
                namespace,
                name,
                outcome,
                image.reference,
                ["--http-probe", f"{server_ip}:8080", "allow" if outcome == "allowed" else "deny"],
                labels,
            ),
            "pod",
            name,
            "network-" + outcome,
        )
        live._kubectl(
            "wait",
            "--for=jsonpath={.status.phase}=Succeeded",
            f"pod/{name}",
            "--timeout=30s",
            namespace=namespace,
            timeout=40,
        )
        log = live._kubectl("logs", name, namespace=namespace).stdout.strip()
        if (
            log != "allowed connection observed"
            if outcome == "allowed"
            else not log.startswith("denied connection observed:")
        ):
            raise TwoVariantError(
                "network probe did not observe the expected " + outcome + " connection"
            )
        observed = live._get("pod", name, namespace)
        observations[outcome] = docker._uid(observed, "Pod", name, namespace)
        statuses = observed.get("status", {}).get("containerStatuses", [])
        finished = (
            statuses[0].get("state", {}).get("terminated", {}).get("finishedAt")
            if len(statuses) == 1
            else None
        )
        if not isinstance(finished, str):
            raise TwoVariantError("connectivity probe has no terminal timestamp")
        probe_times.append(datetime.fromisoformat(finished.replace("Z", "+00:00")))
    now = datetime.now(UTC)
    if any(now - finished > timedelta(minutes=15) for finished in probe_times):
        raise TwoVariantError("network probe outcome expired; fresh probes required")
    server_uid = docker._uid(server, "Pod", server_name, namespace)
    policy_uid = docker._uid(policy, "NetworkPolicy", "default-deny", namespace)
    policy_digest = hashlib.sha256(contracts.canonical_json(policy["spec"])).hexdigest()
    proof = {
        "observed_at": now.isoformat(),
        "server_uid": server_uid,
        "allowed_uid": observations["allowed"],
        "denied_uid": observations["denied"],
        "policy_uid": policy_uid,
        "policy_digest": policy_digest,
    }
    observed_at = _retain_network_proof(receipt, proof)
    evidence = contracts.KubernetesNetworkPolicyEvidence(
        "network-policy-enforced-v1",
        "default-deny",
        policy_uid,
        policy_digest,
        cluster_uid,
        namespace_uid,
        observed_at,
        contracts.KubernetesConnectivityEvidence(
            observations["allowed"], server_uid, "allowed", 3, observed_at
        ),
        contracts.KubernetesConnectivityEvidence(
            observations["denied"], server_uid, "denied", 3, observed_at
        ),
    )
    if not evidence.is_fresh_for(cluster_uid, namespace_uid):
        raise TwoVariantError("retained network enforcement proof is stale")
    return evidence


def run(args: argparse.Namespace) -> dict[str, Any]:
    restart = _load_script("two_variant_restart", "check_kubernetes_control_plane_restart.py")
    authenticated = _load_script(
        "two_variant_dispatch", "check_authenticated_kubernetes_dispatch_e2e.py"
    )
    kubeconfig = args.kubeconfig.resolve(strict=True)
    occupied = bool(
        _run(
            _kubectl(kubeconfig)
            + ["get", "namespace", args.namespace, "--ignore-not-found", "-o", "name"],
            timeout=20,
        ).strip()
    )
    nonce = _bootstrap_nonce(args.receipt, occupied)
    receipt = Receipt(args.receipt, args.namespace, nonce)
    context = {
        "name": CONTEXT,
        "kubeconfig_sha256": hashlib.sha256(kubeconfig.read_bytes()).hexdigest(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    original = receipt.value["steps"].get("context")
    if original and original != context:
        if (
            original.get("name") != context["name"]
            or original.get("kubeconfig_sha256") != context["kubeconfig_sha256"]
        ):
            raise TwoVariantError("fixture context or runner changed after bootstrap")
        if (
            occupied
            and restart._namespace_uid(kubeconfig, args.namespace, nonce)
            != receipt.value["steps"]["namespace"]["uid"]
        ):
            raise TwoVariantError("fixture namespace UID changed before runner revision")
        receipt.record(
            "resume-runner-" + context["runner_sha256"][:12],
            {"runner_sha256": context["runner_sha256"]},
        )
    else:
        receipt.record("context", context)
    restart._require_containerd_image_store()
    restart._require_docker_desktop_kind_cluster()
    if occupied:
        saved = receipt.value["steps"].get("workload-image")
        if not saved:
            raise TwoVariantError(
                "workload image receipt missing; operator reconciliation required"
            )
        image = authenticated._node_image(saved["reference"])
        if (
            image.manifest_digest != saved["manifest_digest"]
            or image.config_digest != saved["config_digest"]
        ):
            raise TwoVariantError("node-resident workload image changed")
    else:
        prebuilt = receipt.value["steps"].get("prebuilt-control-image")
        source_digest = _control_build_sources(ROOT)
        if prebuilt is not None and prebuilt["source_sha256"] != source_digest:
            revision_step = "prebuilt-control-image-revision-" + source_digest[:12]
            amended = receipt.value["steps"].get(revision_step)
            if amended is None:
                if args.prebuilt_control_image_id is None:
                    raise TwoVariantError(
                        "prebuilt control image sources changed after preparation"
                    )
                amended = receipt.record(
                    revision_step,
                    {
                        "image_id": args.prebuilt_control_image_id,
                        "source_sha256": source_digest,
                        "supersedes": prebuilt["image_id"],
                    },
                )
            prebuilt = amended
        if args.prebuilt_control_image_id is not None:
            if prebuilt is None:
                prebuilt = receipt.record(
                    "prebuilt-control-image",
                    {
                        "image_id": args.prebuilt_control_image_id,
                        "source_sha256": source_digest,
                    },
                )
            elif prebuilt["image_id"] != args.prebuilt_control_image_id:
                raise TwoVariantError("prebuilt control image ID changed across replay")
        if prebuilt and prebuilt["source_sha256"] != source_digest:
            raise TwoVariantError("prebuilt control image sources changed after preparation")
        control_image = restart._load_image(
            ROOT, nonce, prebuilt_image_id=prebuilt["image_id"] if prebuilt else None
        )
        prebuilt_workload = receipt.value["steps"].get("prebuilt-workload-image")
        if args.prebuilt_workload_image_id is not None:
            if prebuilt_workload is None:
                prebuilt_workload = receipt.record(
                    "prebuilt-workload-image",
                    {
                        "image_id": args.prebuilt_workload_image_id,
                        "source_sha256": _workload_build_sources(ROOT),
                    },
                )
            elif prebuilt_workload["image_id"] != args.prebuilt_workload_image_id:
                raise TwoVariantError("prebuilt workload image ID changed across replay")
        if prebuilt_workload and prebuilt_workload["source_sha256"] != _workload_build_sources(
            ROOT
        ):
            raise TwoVariantError("prebuilt workload image sources changed after preparation")
        image = docker.build_and_load_exact_image(
            ROOT,
            nonce,
            prebuilt_image_id=prebuilt_workload["image_id"] if prebuilt_workload else None,
        )
        receipt.record(
            "workload-image",
            {
                "reference": image.reference,
                "manifest_digest": image.manifest_digest,
                "config_digest": image.config_digest,
            },
        )
    if occupied:
        control_image = f"docker.io/library/tessera-control-restart:{nonce}"
    documents = _fixture_manifest(restart, args.namespace, nonce, control_image)
    if occupied:
        _reconcile_owned_control(restart, kubeconfig, args.namespace, nonce, documents, receipt)
    else:
        _run(
            _kubectl(kubeconfig) + ["apply", "-f", "-"],
            input_bytes=yaml.safe_dump_all(documents, sort_keys=False).encode(),
        )
    namespace_uid = restart._namespace_uid(kubeconfig, args.namespace, nonce)
    receipt.record("namespace", {"uid": namespace_uid})
    _run(
        _kubectl(kubeconfig, args.namespace)
        + ["wait", "--for=condition=available", "deployment/control", "--timeout=90s"],
        timeout=100,
    )
    target = docker.target_from_kubeconfig(kubeconfig)
    live = docker.DockerDesktopKubernetesAdapter(target, args.receipt.with_suffix(".sqlite3"))
    namespace_resource = live._get("namespace", args.namespace, None)
    if docker._uid(namespace_resource, "Namespace", args.namespace, None) != namespace_uid:
        raise TwoVariantError("namespace UID changed after bootstrap")
    for kind in ("baseline", "intervention"):
        pvc = docker._pvc(args.namespace)
        pvc["metadata"]["name"] = f"outputs-{kind}"
        if live._maybe_get("pvc", f"outputs-{kind}", args.namespace) is None:
            live._create(pvc, args.namespace)
        resource = live._get("pvc", f"outputs-{kind}", args.namespace)
        receipt.record(
            kind + "-pvc",
            {
                "uid": docker._uid(
                    resource, "PersistentVolumeClaim", f"outputs-{kind}", args.namespace
                )
            },
        )
    policy = live._get("networkpolicy", "default-deny", args.namespace)
    spec = policy.get("spec", {})
    if spec.get("podSelector") != {} or sorted(spec.get("policyTypes", [])) != [
        "Egress",
        "Ingress",
    ]:
        raise TwoVariantError("fixture default-deny policy is not the required shape")
    cluster_uid = docker._uid(
        live._get("namespace", "kube-system", None), "Namespace", "kube-system", None
    )
    network_evidence = _network_evidence(
        live,
        authenticated,
        args.namespace,
        nonce,
        receipt,
        cluster_uid,
        namespace_uid,
        image,
        policy,
    )
    cluster = contracts.KubernetesClusterConfig(
        kubeconfig=str(target.kubeconfig),
        kubeconfig_sha256=target.kubeconfig_sha256,
        context=target.context,
        profile="development-minikube-opencode-v1",
        server=target.server,
        server_sha256=hashlib.sha256(target.server.encode()).hexdigest(),
        ca_sha256=target.ca_sha256,
        namespace=args.namespace,
        cluster_uid=cluster_uid,
        namespace_uid=namespace_uid,
        network_none_evidence=network_evidence,
    )
    port = authenticated._port()
    forward = restart._start_port_forward(kubeconfig, args.namespace, port)
    try:
        health = restart._wait_ready(port)
        if forward.poll() is not None:
            raise TwoVariantError("expected port-forward process exited")
        owner = _Api(f"http://127.0.0.1:{port}", _owner_token(kubeconfig, args.namespace))
        worker_token = args.worker_token_file.read_text().strip()
        worker = _Api(f"http://127.0.0.1:{port}", worker_token)

        def human(name, method, path, body):
            return receipt.call(owner, name, method, path, body)

        project = human(
            "project", "POST", "/projects", {"name": "Two-variant deterministic CPU pilot"}
        )
        snapshot = _adopt_snapshot(kubeconfig, args.namespace, project["id"], nonce)
        receipt.record("snapshot", {"id": snapshot["id"], "digest": snapshot["digest"]})
        expiry = receipt.value["steps"].get("credential-expiry", {}).get("at")
        if expiry is None:
            expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        receipt.record("credential-expiry", {"at": expiry})
        credential = human(
            "worker-credential",
            "POST",
            "/credentials",
            {
                "token": worker_token,
                "kind": "worker",
                "audience": "worker",
                "project_id": project["id"],
                "expires_at": expiry,
            },
        )
        human(
            "worker-register",
            "POST",
            f"/projects/{project['id']}/workers",
            {
                "worker_principal_id": credential["principal_id"],
                "approved_build_identifier": "lab-two-variant-e2e",
                "approved_build_digest": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "trust_class": "development",
                "region": "docker-desktop",
            },
        )
        release_expiry = receipt.value["steps"].get("release-expiry", {}).get("at")
        if release_expiry is None:
            release_expiry = (datetime.now(UTC) + timedelta(minutes=40)).isoformat()
        receipt.record("release-expiry", {"at": release_expiry})
        human(
            "snapshot-release",
            "POST",
            f"/snapshots/{snapshot['id']}/releases",
            {
                "expected_digest": snapshot["digest"],
                "worker_principal_id": credential["principal_id"],
                "classification": "public",
                "permits_worker_transfer": True,
                "rationale": "Explicit public deterministic CPU fixture input release.",
                "expires_at": release_expiry,
            },
        )
        human(
            "limits",
            "PUT",
            f"/projects/{project['id']}/execution-limits",
            {
                "expected_revision": 0,
                "currency": "USD",
                "monetary_limit_cents": 0,
                "max_concurrency": 1,
            },
        )
        variants = []
        for kind, iterations in (("baseline", 10), ("intervention", 20)):
            task = human(
                kind + "-task",
                "POST",
                f"/projects/{project['id']}/tasks",
                {
                    "title": f"CPU {kind}",
                    "purpose": "Public deterministic sum-of-squares comparison",
                    "budget_cents": 0,
                    "max_duration_seconds": args.timeout,
                },
            )
            command = ["/hello", "--cpu-sum-squares", str(iterations), "/outputs/result.txt"]
            plan = human(
                kind + "-plan",
                "PUT",
                f"/tasks/{task['id']}/plan",
                {
                    "expected_revision": task["revision"],
                    "title": task["title"],
                    "purpose": task["purpose"],
                    "budget_cents": 0,
                    "max_duration_seconds": args.timeout,
                    "execution_spec": {
                        "argv": command,
                        "input_snapshot_digest": snapshot["digest"],
                        "pinned_skill_digests": [],
                        "output_paths": ["result.txt"],
                        "profile": "kubernetes-opencode",
                        "network_enabled": False,
                        "max_output_bytes": 1024,
                    },
                },
            )
            approval = human(
                kind + "-approval",
                "POST",
                f"/tasks/{task['id']}/approve",
                {
                    "expected_revision": plan["revision"],
                    "expected_plan_digest": plan["current_plan_digest"],
                },
            )
            if approval.get("execution_granted") is not False:
                raise TwoVariantError("plan approval incorrectly granted execution")
            variants.append(
                {
                    "kind": kind,
                    "name": f"sum of squares {iterations}",
                    "method_digest": hashlib.sha256(json.dumps(command).encode()).hexdigest(),
                    "input_digest": snapshot["digest"],
                    "metadata": {"iterations": iterations},
                    "execution_task_id": task["id"],
                    "execution_plan_digest": approval["current_plan_digest"],
                }
            )
        experiment = human(
            "experiment",
            "POST",
            f"/projects/{project['id']}/experiments",
            {
                "goal": "Compare public bounded CPU sums of squares",
                "success_criterion": "Two retained distinct JSON results, 385 and 2870",
                "protocol": (
                    "Separate immutable commands and output PVCs; one concurrent grant. "
                    "Scientific validity not assessed."
                ),
                "variants": variants,
            },
        )
        worker_root = args.receipt.parent / f"{args.namespace}-worker-journal"
        worker_root.mkdir(mode=0o700, exist_ok=True)
        if worker_root.stat().st_mode & 0o077:
            raise TwoVariantError("worker journal directory must be owner-only")

        def run_one(kind):
            with ExitStack() as stack:
                return _variant(
                    kind,
                    owner,
                    worker,
                    receipt,
                    project,
                    snapshot,
                    credential,
                    experiment,
                    cluster,
                    live,
                    authenticated,
                    image,
                    port,
                    args.namespace,
                    args.worker_token_file,
                    worker_root,
                    args.timeout,
                    stack,
                )

        results = _serial_variants(run_one, receipt)
        final = owner.request("GET", f"/experiments/{experiment['id']}")
        if len(final.get("measurements", [])) != 2 or final.get("conclusions"):
            raise TwoVariantError(
                "comparison lacks two measurements or has an automated conclusion"
            )
        if results["baseline"]["sha256"] == results["intervention"]["sha256"]:
            raise TwoVariantError("baseline/intervention output digests unexpectedly match")
        if restart._wait_ready(port).get("backend_identity") != health.get("backend_identity"):
            raise TwoVariantError("control backend identity changed")
        receipt.record(
            "result",
            {
                "state": "two_jobs_succeeded_review_pending",
                "namespace_uid": namespace_uid,
                "experiment_id": experiment["id"],
                "baseline": results["baseline"],
                "intervention": results["intervention"],
            },
        )
        receipt.value["failure"] = None
        receipt.save()
        return receipt.value["steps"]["result"]
    finally:
        restart._stop_port_forward(forward)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--owner-pre-manifest-recovery", action="store_true")
    parser.add_argument("--kubeconfig", type=Path)
    parser.add_argument("--context", choices=[CONTEXT], default=CONTEXT)
    parser.add_argument("--namespace")
    parser.add_argument("--worker-token-file", type=Path)
    parser.add_argument("--prebuilt-control-image-id")
    parser.add_argument("--prebuilt-workload-image-id")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    if args.owner_pre_manifest_recovery:
        if (
            args.run
            or not args.kubeconfig
            or not args.namespace
            or args.worker_token_file
            or not args.receipt
        ):
            parser.error(
                "recovery requires --kubeconfig, --namespace, --receipt; no --run or worker token"
            )
    elif not args.run or not args.kubeconfig or not args.namespace or not args.worker_token_file:
        parser.error("--run, --kubeconfig, --namespace and --worker-token-file are required")
    if (
        not re.fullmatch(
            r"tessera-lab-two-variant-e2e-[a-z0-9]([-a-z0-9]{0,29}[a-z0-9])?", args.namespace
        )
        or not 1 <= args.timeout <= 180
    ):
        parser.error(
            "fresh tessera-lab-two-variant-e2e-* namespace and 1..180 second timeout required"
        )
    if args.prebuilt_control_image_id is not None and not re.fullmatch(
        r"sha256:[0-9a-f]{64}", args.prebuilt_control_image_id
    ):
        parser.error("prebuilt control image ID must be an exact sha256 digest")
    if args.prebuilt_workload_image_id is not None and not re.fullmatch(
        r"sha256:[0-9a-f]{64}", args.prebuilt_workload_image_id
    ):
        parser.error("prebuilt workload image ID must be an exact sha256 digest")
    if not args.owner_pre_manifest_recovery:
        if (
            not args.worker_token_file.is_file()
            or args.worker_token_file.is_symlink()
            or args.worker_token_file.stat().st_mode & 0o077
        ):
            parser.error("worker token file must be private, owner-only, and not a symlink")
    if args.receipt is None:
        args.receipt = ROOT / ".tessera" / "receipts" / f"{args.namespace}.json"
    receipts_root = ROOT / ".tessera" / "receipts"
    if (
        (ROOT / ".tessera").is_symlink()
        or receipts_root.is_symlink()
        or args.receipt.parent.resolve() != receipts_root.resolve()
        or args.receipt.suffix != ".json"
        or args.receipt.is_symlink()
    ):
        parser.error("receipt must be a JSON file under this repository's .tessera/receipts")
    try:
        result = (
            run_owner_pre_manifest_recovery(args) if args.owner_pre_manifest_recovery else run(args)
        )
    except Exception as error:
        # Do not emit exception contents: transports or subprocesses may include secrets.
        if not args.owner_pre_manifest_recovery and (
            args.receipt.exists()
            and not args.receipt.is_symlink()
            and not args.receipt.stat().st_mode & 0o077
        ):
            try:
                data = json.loads(args.receipt.read_text())
                receipt = Receipt(args.receipt, args.namespace, data["nonce"])
                steps = data.get("steps", {})
                active = next(
                    (
                        kind
                        for kind in ("baseline", "intervention")
                        if kind + "-result" not in steps
                    ),
                    "post_execution",
                )
                receipt.value["failure"] = {
                    "type": type(error).__name__,
                    "stage": receipt.value.get("last_step") or list(steps)[-1:],
                    "variant": active,
                    "attempt_id": steps.get(active + "-admit", {}).get("id"),
                    "dispatch_id": steps.get(active + "-dispatch", {}).get("id"),
                    "grant_id": steps.get(active + "-grant", {}).get("id"),
                    "state": "unresolved_needs_real_observation",
                }
                receipt.save()
            except (OSError, ValueError, KeyError, TwoVariantError):
                pass  # corrupt receipts require manual UID reconciliation
        print(
            json.dumps(
                {
                    (
                        "recovery_verdict"
                        if args.owner_pre_manifest_recovery
                        else "fixture_verdict"
                    ): "blocked" if args.owner_pre_manifest_recovery else "failed",
                    "error_type": type(error).__name__,
                    "receipt": str(args.receipt),
                    "retained": "namespace and artifacts; inspect by UID",
                }
            )
        )
        return 1
    print(
        json.dumps(
            {
                ("recovery_verdict" if args.owner_pre_manifest_recovery else "fixture_verdict"): (
                    "reconciled_no_start" if args.owner_pre_manifest_recovery else "passed"
                ),
                **result,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
