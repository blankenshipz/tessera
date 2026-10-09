"""Opt-in two-variant orchestration; the control plane remains the sole authority.

This pilot executes constant, networkless CPU probes on a selected Kubernetes
compute provider. It does NOT activate an LLM provider or resolve model secrets.
The supplemental journal is forensic, never an execution grant or retry queue.
"""

from __future__ import annotations

import base64
import hashlib
import re
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.request import Request

from .control_channel import ControlChannelError, HttpControlChannel
from .kubernetes_contracts import canonical_json, cluster_identity_digest
from .kubernetes_execution import KubernetesExecutionPackage


class PilotError(RuntimeError):
    """A sanitized, fail-closed pilot failure."""


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat()


class PilotControlChannel(HttpControlChannel):
    def get(self, path: str) -> dict[str, Any]:
        request = Request(
            self.origin + self._path(path),
            headers={"Authorization": f"Bearer {self.bearer_token}", "Accept": "application/json"},
            method="GET",
        )
        try:
            return self._submit(request, self.timeout_seconds).result(timeout=self.timeout_seconds)
        except TimeoutError:
            raise ControlChannelError("control deadline exceeded") from None


class PilotJournal:
    """One-use run identity and append-only request/response digests, no secrets.

    A committed intent with no response is unknown. Reopening this journal never
    replays a mutation: reconcile through the retained ordinary worker journal.
    """

    def __init__(self, root: Path):
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise PilotError("journal root must be an existing absolute private directory")
        if root.stat().st_mode & 0o077:
            raise PilotError("journal root must have mode 0700")
        path = root / "pilot.sqlite3"
        if path.is_symlink():
            raise PilotError("journal cannot be a symlink")
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        path.chmod(0o600)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(
            """CREATE TABLE IF NOT EXISTS run (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), config_digest TEXT NOT NULL,
                started_at TEXT NOT NULL, evidence_kind TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY, kind TEXT NOT NULL,
                recorded_at TEXT NOT NULL, payload_json TEXT NOT NULL);"""
        )

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def begin(self, config: dict[str, Any], *, live: bool) -> None:
        try:
            with self.lock, self.db:
                self.db.execute(
                    "INSERT INTO run VALUES (1,?,?,?)",
                    (digest(config), now(), "live_remote_compute" if live else "synthetic"),
                )
        except sqlite3.IntegrityError:
            raise PilotError(
                "pilot already claimed; manual reconciliation required, no replay"
            ) from None

    def event(self, kind: str, payload: dict[str, Any]) -> None:
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO events(kind,recorded_at,payload_json) VALUES (?,?,?)",
                (kind, now(), canonical_json(payload).decode()),
            )

    def blocked(self, stage: str, reason: str) -> None:
        """Retain a sanitized, actionable stop reason without authorizing a retry."""
        self.event("blocked", {"stage": stage, "reason": reason, "reconciliation_required": True})

    def contains(self, kind: str, name: str) -> bool:
        with self.lock:
            return (
                self.db.execute(
                    "SELECT 1 FROM events WHERE kind=? "
                    "AND json_extract(payload_json,'$.name')=? LIMIT 1",
                    (kind, name),
                ).fetchone()
                is not None
            )


class PilotReadinessJournal:
    """Append-only readiness outcomes, separate from the one-use execution journal.

    A blocked readiness check is evidence for an operator, not a submitted run and
    therefore must not claim the ``pilot.sqlite3`` execution identity.
    """

    def __init__(self, root: Path):
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise PilotError("journal root must be an existing absolute private directory")
        if root.stat().st_mode & 0o077:
            raise PilotError("journal root must have mode 0700")
        path = root / "pilot-readiness.sqlite3"
        if path.is_symlink():
            raise PilotError("readiness journal cannot be a symlink")
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        path.chmod(0o600)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS readiness_events (
                sequence INTEGER PRIMARY KEY, config_digest TEXT NOT NULL,
                stage TEXT NOT NULL, outcome TEXT NOT NULL, reason TEXT NOT NULL,
                recorded_at TEXT NOT NULL)"""
        )

    def record(self, config: dict[str, Any], stage: str, outcome: str, reason: str) -> None:
        if outcome not in {"passed", "blocked"}:
            raise ValueError("readiness outcome is invalid")
        with self.lock, self.db:
            self.db.execute(
                """INSERT INTO readiness_events(config_digest,stage,outcome,reason,recorded_at)
                VALUES (?,?,?,?,?)""",
                (digest(config), stage, outcome, reason, now()),
            )

    def close(self) -> None:
        with self.lock:
            self.db.close()


class ComparisonPilot:
    """Admit/package/dispatch two existing approved variants, one attempt each.

    ``execute`` must run the ordinary dispatch worker, not post observations on
    its behalf. ``collect`` supplies bounded observed stdout from its owned Pod.
    Tests inject only external effects. No human research outcome is inferred.
    """

    def __init__(self, owner, worker, cluster, output, journal, execute, collect):
        self.owner, self.worker = owner, worker
        self.cluster, self.output, self.journal = cluster, output, journal
        self.execute, self.collect = execute, collect
        self.sequence = 0

    def post(self, channel, path, body):
        self.sequence += 1
        key = f"comparison-pilot-{self.run_id}-{self.sequence}"
        self.journal.event(
            "request_intent",
            {"path": path, "key": key, "request_digest": digest(body)},
        )
        try:
            response = channel.post(path, body, key)
        except Exception:
            self.journal.event("reply_unknown", {"key": key, "automatic_retry": False})
            raise PilotError("control reply unavailable; retain journal and reconcile") from None
        self.journal.event("response", {"key": key, "response_digest": digest(response)})
        return response

    def run(self, config: dict[str, Any], *, live: bool = False) -> dict[str, Any]:
        validate_config(config)
        self.run_id = digest(config)[:32]
        self.journal.begin(config, live=live)
        results = []
        try:
            variants = self.preflight(config)
            for kind, variant, grant in variants:
                result = self.run_variant(config, kind, variant, grant)
                results.append(result)
                if result["status"] not in {"succeeded", "failed"}:
                    break  # Unknown exposure never authorizes the other variant.
        except PilotError as error:
            self.journal.blocked("orchestration", str(error))
            self.journal.event(
                "stopped", {"reconciliation_required": True, "automatic_retry": False}
            )
            raise PilotError(
                "pilot stopped; inspect retained journals; no automatic retry"
            ) from None
        except Exception:
            self.journal.blocked("orchestration", "unexpected_pilot_failure")
            self.journal.event(
                "stopped", {"reconciliation_required": True, "automatic_retry": False}
            )
            raise PilotError(
                "pilot stopped; inspect retained journals; no automatic retry"
            ) from None
        result = {
            "state": "completed" if len(results) == 2 else "reconciliation_required",
            "evidence_kind": "live_remote_compute" if live else "synthetic",
            "target_name": config["target_name"],
            "target_digest": cluster_identity_digest(self.cluster),
            "variants": results,
            "model_provider_enabled": False,
            "model_calls": 0,
            "model_tokens": 0,
            "usage": "unknown",
            "cost": "unknown",
            "human_review": "pending",
            "scientific_validity": "not_assessed",
        }
        self.journal.event("final", result)
        return result

    def preflight(self, config):
        exported = self.owner.get(f"/experiments/{config['experiment_id']}")
        experiment = exported["experiment"]
        if experiment["project_id"] != config["project_id"]:
            raise PilotError("experiment project mismatch")
        if experiment["current_design_revision"] != config["design_revision"]:
            raise PilotError("design revision mismatch")
        if exported["admitted_attempts"] or exported["attempts"]:
            raise PilotError("experiment already has attempts; reconcile rather than replace")
        limits = self.owner.get(f"/projects/{config['project_id']}/execution-limits")
        # Control reserves concurrency for unresolved grants, not just running
        # Jobs. Two grants must remain held even after unknown billing on exit.
        if limits["max_concurrency"] != 2:
            raise PilotError("pilot requires exactly two project reservation slots")
        variants = experiment["design"]["variants"]
        if len(variants) != 2 or {v["kind"] for v in variants} != {"baseline", "intervention"}:
            raise PilotError("pilot requires exactly the registered baseline and intervention")
        result = []
        for kind, number in (("baseline", 1), ("intervention", 2)):
            variant = next(v for v in variants if v["kind"] == kind)
            grant = self.owner.get(f"/execution-grants/{config['grants'][kind]}")
            task = self.owner.get(f"/tasks/{variant['execution_task_id']}")
            plan = self.owner.get(f"/tasks/{task['id']}/plans/{variant['execution_plan_digest']}")
            spec = plan["execution_spec"]
            if (
                task["status"] != "approved"
                or grant["status"] != "active"
                or grant["project_id"] != config["project_id"]
                or grant["task_id"] != task["id"]
                or grant["plan_digest"] != variant["execution_plan_digest"]
                or task["current_plan_digest"] != variant["execution_plan_digest"]
                or grant["currency"] != "USD"
                # A control grant is an internal reservation.  It is deliberately
                # not treated as a Kubernetes/provider billing cap; that distinct
                # assertion is checked by the opt-in runner before this flow starts.
                or grant["max_exposure_cents"]
                > config["max_internal_reservation_cents_per_variant"]
                or task["max_duration_seconds"] > config["max_seconds_per_variant"]
                or spec["argv"] != ["python", "-c", f"print({number})"]
                or spec["profile"] != "kubernetes-opencode"
                or spec["network_enabled"] is not False
                or spec["pinned_skill_digests"]
                or spec["output_paths"] != ["result.txt"]
                or not 2 <= spec["max_output_bytes"] <= config["max_output_bytes"]
            ):
                raise PilotError("approved plan/grant exceeds the constant-probe pilot contract")
            try:
                grant_expires_at = datetime.fromisoformat(grant["expires_at"])
            except (KeyError, TypeError, ValueError):
                raise PilotError("grant expiry is missing or malformed") from None
            if grant_expires_at.tzinfo is None or grant_expires_at <= datetime.now(UTC):
                raise PilotError("grant is expired before pilot admission")
            result.append((kind, variant, grant))
        if result[0][2]["worker_principal_id"] != result[1][2]["worker_principal_id"]:
            raise PilotError("both variants must use the same project-scoped worker")
        self.journal.event(
            "preflight",
            {
                "experiment_id": config["experiment_id"],
                "target_digest": cluster_identity_digest(self.cluster),
                "max_concurrency": 1,
                "max_attempts_per_variant": 1,
                "automatic_retry": False,
                "cost_enforcement_digest": config["cost_enforcement_digest"],
            },
        )
        return result

    def attempt(self, config, attempt_id):
        exported = self.owner.get(f"/experiments/{config['experiment_id']}/export")
        return next(item for item in exported["admitted_attempts"] if item["id"] == attempt_id)

    def run_variant(self, config, kind, variant, grant):
        self.recheck_grant(config, kind, grant)
        admitted = self.post(
            self.owner,
            f"/experiment-variants/{variant['id']}/admitted-attempts",
            {
                "expected_variant_revision": variant["revision"],
                "expected_design_revision": config["design_revision"],
                "grant_id": grant["id"],
            },
        )
        self.post(self.worker, "/worker/heartbeat", {})
        lease = self.post(self.worker, "/worker/leases", {"grant_id": grant["id"]})
        delivered = self.post(
            self.worker,
            f"/worker/leases/{lease['id']}/package",
            {
                "expected_epoch": lease["epoch"],
                "execution_adapter": "kubernetes-job",
                "admitted_attempt_id": admitted["id"],
            },
        )
        package = KubernetesExecutionPackage.from_admitted_package(delivered, self.output)
        self.recheck_grant(config, kind, grant)
        dispatch = self.post(
            self.owner,
            f"/experiment-admitted-attempts/{admitted['id']}/dispatch",
            {
                "expected_revision": self.attempt(config, admitted["id"])["revision"],
                "target_digest": cluster_identity_digest(self.cluster),
                "profile_version": self.cluster.profile,
            },
        )
        identity = {
            "kind": kind,
            "operation_id": package.operation_id,
            "package_digest": package.package_digest,
            "admitted_attempt_id": admitted["id"],
            "dispatch_id": dispatch["id"],
            "lease_id": lease["id"],
            "epoch": lease["epoch"],
            "hard_expires_at": package.hard_expires_at.isoformat(),
        }
        self.journal.event("dispatch_intent", identity)
        observed = self.execute(dispatch, package)
        self.journal.event("worker_response", identity | {"response_digest": digest(observed)})
        current = self.attempt(config, admitted["id"])
        status = observed["status"]
        if not current.get("operational_termination_confirmed"):
            status = "unknown"
        result = identity | {
            "status": status,
            "human_review": "pending",
            "provider_id": None,
            "usage": "unknown",
            "cost": "unknown",
        }
        if status == "succeeded":
            limit = package.package["plan"]["execution_spec"]["max_output_bytes"]
            content, provider = self.collect(package, limit)
            if not isinstance(content, bytes) or len(content) > limit:
                raise PilotError("observed output exceeds the approved limit")
            # The probes have exact bounded stdout; never substitute expected bytes.
            if content != (b"1\n" if kind == "baseline" else b"2\n"):
                raise PilotError("observed output differs from the registered probe")
            binding = {"expected_epoch": lease["epoch"], "operation_id": package.operation_id}
            path = f"/worker/leases/{lease['id']}"
            artifact = self.post(
                self.worker,
                path + "/artifacts",
                binding
                | {
                    "relative_path": "result.txt",
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "content_base64": base64.b64encode(content).decode(),
                },
            )
            self.post(
                self.worker,
                path + "/project-artifacts",
                binding | {"artifacts": [{k: artifact[k] for k in ("id", "sha256", "size")}]},
            )
            manifest = self.post(
                self.worker,
                path + "/artifact-manifest",
                binding
                | {
                    "artifacts": [
                        {k: artifact[k] for k in ("id", "relative_path", "sha256", "size")}
                    ]
                },
            )
            result.update(
                provider_id=provider,
                artifact_id=artifact["id"],
                artifact_sha256=artifact["sha256"],
                manifest_id=manifest["id"],
                manifest_digest=manifest["manifest_digest"],
                artifact_source="owned_pod_stdout",
                artifact_status=manifest["status"],
            )
        self.journal.event("variant_finished", result)
        return result

    def recheck_grant(self, config, kind, expected_grant):
        """Read current grant state at each mutable boundary; cached preflight is insufficient."""
        grant = self.owner.get(f"/execution-grants/{expected_grant['id']}")
        try:
            expires_at = datetime.fromisoformat(grant["expires_at"])
        except (KeyError, TypeError, ValueError):
            raise PilotError("grant expiry is missing or malformed") from None
        if (
            grant.get("id") != expected_grant["id"]
            or grant.get("project_id") != config["project_id"]
            or grant.get("task_id") != expected_grant["task_id"]
            or grant.get("plan_digest") != expected_grant["plan_digest"]
            or grant.get("status") != "active"
            or expires_at.tzinfo is None
            or expires_at <= datetime.now(UTC)
        ):
            raise PilotError("grant is no longer active for the registered variant")
        self.journal.event(
            "grant_rechecked",
            {"kind": kind, "grant_id": grant["id"], "expires_at": grant["expires_at"]},
        )


def validate_config(config):
    expected = {
        "project_id",
        "experiment_id",
        "design_revision",
        "target_name",
        "grants",
        "max_seconds_per_variant",
        "max_output_bytes",
        "max_internal_reservation_cents_per_variant",
        "cost_enforcement_digest",
    }
    if set(config) != expected:
        raise PilotError("pilot configuration fields do not match the version-one contract")
    for field in ("project_id", "experiment_id", "target_name"):
        if not isinstance(config[field], str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", config[field]
        ):
            raise PilotError("pilot identity is malformed")
    for field, maximum in (
        ("design_revision", 100000),
        ("max_seconds_per_variant", 120),
        ("max_output_bytes", 4096),
        ("max_internal_reservation_cents_per_variant", 10),
    ):
        if type(config[field]) is not int or not 1 <= config[field] <= maximum:
            raise PilotError("pilot bound is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", config["cost_enforcement_digest"]):
        raise PilotError("target-side hard cost enforcement evidence is required")
    if (
        not isinstance(config["grants"], dict)
        or set(config["grants"]) != {"baseline", "intervention"}
        or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", v) for v in config["grants"].values())
        or len(set(config["grants"].values())) != 2
    ):
        raise PilotError("two distinct existing approved execution grants are required")
