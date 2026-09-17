"""Run Astra's explicitly confirmed ten-turn remote Kubernetes OAuth fixture.

This opt-in script is intentionally outside pytest and CI.  It never obtains an
OAuth token, API key, cluster credential, or control credential.  The supplied
OAuth value is an opaque, owner-bound runtime reference resolved only by an
already provisioned remote runner.
"""

# ruff: noqa: E501, E701, E702, I001
# fmt: off

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tessera_worker.kubernetes_contracts import KubernetesProviderIdentity, canonical_json


PROFILE = "remote-kubernetes-openai-chatgpt-oauth-e2e-v2"
PROVIDER = "openai-chatgpt-oauth"
PROTOCOL = "v43"
TURN_COUNT = 10
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_HEX = re.compile(r"[0-9a-f]{32}\Z")
_NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")


class LiveOAuthV2Error(RuntimeError):
    """The fixture cannot safely make or interpret a provider side effect."""


@dataclass(frozen=True)
class Target:
    kubeconfig: Path
    context: str
    namespace: str
    server: str
    ca_sha256: str
    cluster_uid: str
    namespace_uid: str
    target_digest: str


@dataclass(frozen=True)
class OAuthRuntimeReference:
    owner_id: str
    reference: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.reference.encode("utf-8")).hexdigest()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _encoded(value: Mapping[str, Any]) -> str:
    return canonical_json(dict(value)).decode("utf-8")


def _image_digest(image: str) -> str:
    if "@" not in image:
        raise LiveOAuthV2Error("--image must be an immutable repository@sha256 reference")
    _repository, digest = image.rsplit("@", 1)
    if not _DIGEST.fullmatch(digest):
        raise LiveOAuthV2Error("--image must end with a lowercase sha256 digest")
    return digest


def _oauth_reference(args: argparse.Namespace) -> OAuthRuntimeReference:
    parsed = urlparse(args.oauth_runtime_ref)
    if (
        parsed.scheme != PROVIDER or parsed.netloc != args.oauth_owner_id
        or not parsed.path.strip("/") or parsed.query or parsed.fragment
        or parsed.username or parsed.password
    ):
        raise LiveOAuthV2Error("--oauth-runtime-ref must be openai-chatgpt-oauth://<owner>/<opaque-reference>")
    return OAuthRuntimeReference(args.oauth_owner_id, args.oauth_runtime_ref)


class LiveProfileJournal:
    """Crash-safe, private evidence for one immutable ten-turn conversation.

    This is deliberately a fixture-local journal, not a control-plane execution
    ledger.  It makes intent, reservation, confirmation consumption, and state
    durable before Kubernetes/provider effects and never stores bearer material.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser().resolve()
        if self.path.exists() and (self.path.is_symlink() or not self.path.is_file()):
            raise LiveOAuthV2Error("live profile journal must be a regular file")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.connection.execute("PRAGMA journal_mode=DELETE")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(
            """CREATE TABLE IF NOT EXISTS live_profile_v2_runs (
            operation_id TEXT PRIMARY KEY, profile TEXT NOT NULL, conversation_id TEXT NOT NULL UNIQUE,
            invocation_id TEXT NOT NULL UNIQUE, state TEXT NOT NULL CHECK(state IN
              ('preflight_passed','submit_intended','submitting','submitted','local_observation_paused',
               'reconnected','succeeded','cancel_requested','reconciliation_required')),
            target_digest TEXT NOT NULL, manifest_digest TEXT NOT NULL, image_digest TEXT NOT NULL,
            oauth_owner_id TEXT NOT NULL, oauth_runtime_ref_sha256 TEXT NOT NULL, model TEXT NOT NULL,
            turn_count INTEGER NOT NULL CHECK(turn_count=10), max_turn_tokens INTEGER NOT NULL,
            max_total_tokens INTEGER NOT NULL, max_turn_seconds INTEGER NOT NULL,
            max_total_seconds INTEGER NOT NULL, max_model_calls INTEGER NOT NULL CHECK(max_model_calls=10),
            billing_mode TEXT NOT NULL CHECK(billing_mode='chatgpt_oauth_subscription_only'),
            reservation_state TEXT NOT NULL CHECK(reservation_state IN ('none','reserved','unknown')),
            confirmation_digest TEXT, confirmation_consumed INTEGER NOT NULL DEFAULT 0 CHECK(confirmation_consumed IN (0,1)),
            job_uid TEXT, pod_uid TEXT, reconciliation_required INTEGER NOT NULL DEFAULT 0 CHECK(reconciliation_required IN (0,1)),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS live_profile_v2_turns (
            operation_id TEXT NOT NULL REFERENCES live_profile_v2_runs(operation_id), turn_number INTEGER NOT NULL CHECK(turn_number BETWEEN 1 AND 10),
            conversation_id TEXT NOT NULL, invocation_id TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('planned','observed','unknown','cancelled')),
            model_calls INTEGER, total_tokens INTEGER, elapsed_seconds INTEGER, tool_calls_json TEXT,
            usage_json TEXT, evidence_json TEXT, recorded_at TEXT, PRIMARY KEY(operation_id,turn_number));
            CREATE TABLE IF NOT EXISTS live_profile_v2_evidence (
            id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES live_profile_v2_runs(operation_id),
            classification TEXT NOT NULL CHECK(classification IN ('local_observation_paused','remote_progress','uncertain','cancellation_requested')),
            evidence_json TEXT NOT NULL, recorded_at TEXT NOT NULL);"""
        )
        columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(live_profile_v2_runs)")
        }
        if "pod_uid" not in columns:
            self.connection.execute("ALTER TABLE live_profile_v2_runs ADD COLUMN pod_uid TEXT")

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> LiveProfileJournal:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _identity(args: argparse.Namespace) -> tuple[str, str]:
        # Stable IDs bind every record to one durable conversation/invocation,
        # without making an external provider identity from local randomness.
        conversation = uuid.uuid5(uuid.NAMESPACE_URL, f"tessera:{PROFILE}:conversation:{args.operation_id}").hex
        invocation = uuid.uuid5(uuid.NAMESPACE_URL, f"tessera:{PROFILE}:invocation:{args.operation_id}").hex
        return conversation, invocation

    def preflight(self, args: argparse.Namespace, target: Target, digest: str) -> None:
        conversation_id, invocation_id = self._identity(args)
        values = (
            PROFILE, conversation_id, invocation_id, target.target_digest, digest, _image_digest(args.image),
            args.oauth_owner_id, _oauth_reference(args).digest, args.model, TURN_COUNT, args.max_turn_tokens,
            args.max_total_tokens, args.max_turn_seconds, args.timeout, 10, "chatgpt_oauth_subscription_only",
        )
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute("SELECT profile,conversation_id,invocation_id,target_digest,manifest_digest,image_digest,oauth_owner_id,oauth_runtime_ref_sha256,model,turn_count,max_turn_tokens,max_total_tokens,max_turn_seconds,max_total_seconds,max_model_calls,billing_mode,state FROM live_profile_v2_runs WHERE operation_id=?", (args.operation_id,)).fetchone()
            if row is not None:
                if tuple(row[:16]) != values or row[16] != "preflight_passed":
                    raise LiveOAuthV2Error("live profile journal conflicts with immutable preflight or is no longer runnable")
            else:
                now = _now()
                self.connection.execute("""INSERT INTO live_profile_v2_runs
                    (operation_id,profile,conversation_id,invocation_id,state,target_digest,manifest_digest,image_digest,oauth_owner_id,oauth_runtime_ref_sha256,model,turn_count,max_turn_tokens,max_total_tokens,max_turn_seconds,max_total_seconds,max_model_calls,billing_mode,reservation_state,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (args.operation_id, *values[:3], "preflight_passed", *values[3:], "none", now, now))
                self.connection.executemany("INSERT INTO live_profile_v2_turns(operation_id,turn_number,conversation_id,invocation_id,state) VALUES (?,?,?,?,?)", [(args.operation_id, number, conversation_id, invocation_id, "planned") for number in range(1, TURN_COUNT + 1)])
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def require_preflight(self, args: argparse.Namespace, target: Target, digest: str) -> None:
        self.preflight(args, target, digest)  # Matching existing records are checked atomically.

    def reserve_and_claim_confirmation(self, args: argparse.Namespace, digest: str) -> tuple[str, str]:
        expected = f"TEN_OAUTH_MODEL_CALLS:{digest}"
        if args.confirm_manifest_digest != digest or args.confirm_provider_calls != expected:
            raise LiveOAuthV2Error("--run needs matching --confirm-manifest-digest and --confirm-provider-calls=TEN_OAUTH_MODEL_CALLS:<digest>")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute("SELECT state,confirmation_consumed,manifest_digest,conversation_id,invocation_id FROM live_profile_v2_runs WHERE operation_id=?", (args.operation_id,)).fetchone()
            if row is None or row[0] != "preflight_passed" or row[1] or row[2] != digest:
                raise LiveOAuthV2Error("matching unused preflight confirmation is required; run is already claimed or unsafe")
            self.connection.execute("UPDATE live_profile_v2_runs SET state='submit_intended',reservation_state='reserved',confirmation_digest=?,confirmation_consumed=1,updated_at=? WHERE operation_id=?", (digest, _now(), args.operation_id))
            self.connection.execute("COMMIT")
            return str(row[3]), str(row[4])
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def transition(self, operation_id: str, state: str, *, job_uid: str | None = None, pod_uid: str | None = None) -> None:
        if self.connection.execute("UPDATE live_profile_v2_runs SET state=?,job_uid=COALESCE(?,job_uid),pod_uid=COALESCE(?,pod_uid),updated_at=? WHERE operation_id=?", (state, job_uid, pod_uid, _now(), operation_id)).rowcount != 1:
            raise LiveOAuthV2Error("live profile intent record disappeared")

    def evidence(self, operation_id: str, classification: str, evidence: Mapping[str, Any]) -> None:
        self.connection.execute("INSERT INTO live_profile_v2_evidence(operation_id,classification,evidence_json,recorded_at) VALUES (?,?,?,?)", (operation_id, classification, _encoded(evidence), _now()))

    def reconciliation(self, operation_id: str, reason: str, *, job_uid: str | None, pod_uid: str | None, evidence: Mapping[str, Any]) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.transition(operation_id, "reconciliation_required", job_uid=job_uid, pod_uid=pod_uid)
            self.connection.execute("UPDATE live_profile_v2_runs SET reservation_state='unknown',reconciliation_required=1 WHERE operation_id=?", (operation_id,))
            self.evidence(operation_id, "uncertain", {"reason": reason, **dict(evidence)})
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def cancel(self, operation_id: str) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute("SELECT state FROM live_profile_v2_runs WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None or row[0] not in {"preflight_passed", "submit_intended", "submitting", "submitted", "local_observation_paused", "reconnected"}:
                raise LiveOAuthV2Error("fixture run cannot be cancelled from its current state")
            self.transition(operation_id, "cancel_requested")
            self.evidence(operation_id, "cancellation_requested", {"automatic_provider_termination": False})
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def record_turns(self, operation_id: str, turns: list[dict[str, Any]]) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for turn in turns:
                self.connection.execute("""UPDATE live_profile_v2_turns SET state='observed',model_calls=?,total_tokens=?,elapsed_seconds=?,tool_calls_json=?,usage_json=?,evidence_json=?,recorded_at=? WHERE operation_id=? AND turn_number=? AND state='planned'""", (turn["model_calls"], turn["usage"]["total_tokens"], turn["elapsed_seconds"], _encoded({"tool_calls": turn["tool_calls"]}), _encoded(turn["usage"]), _encoded(turn), _now(), operation_id, turn["turn_number"]))
            if self.connection.execute("SELECT count(*) FROM live_profile_v2_turns WHERE operation_id=? AND state='observed'", (operation_id,)).fetchone()[0] != TURN_COUNT:
                raise LiveOAuthV2Error("remote attestation did not produce exactly ten new durable turn records")
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise


def _run(argv: list[str], *, input_text: str | None = None, check: bool = True, timeout: int = 60):
    return subprocess.run(argv, input=input_text, capture_output=True, text=True, check=check, timeout=timeout)


def _kubectl(args: argparse.Namespace, *parts: str, input_text: str | None = None, check: bool = True, timeout: int = 60):
    return _run(["kubectl", "--kubeconfig", str(args.kubeconfig), "--context", args.context, *parts], input_text=input_text, check=check, timeout=timeout)


def _json_command(args: argparse.Namespace, *parts: str) -> dict[str, Any]:
    completed = _kubectl(args, *parts, "-o", "json")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise LiveOAuthV2Error("Kubernetes returned malformed JSON") from error
    if not isinstance(value, dict):
        raise LiveOAuthV2Error("Kubernetes returned a non-object")
    return value


def _target(args: argparse.Namespace) -> Target:
    config = _json_command(args, "config", "view", "--raw", "--minify")
    try:
        context = next(item["context"] for item in config["contexts"] if item["name"] == args.context)
        cluster = next(item["cluster"] for item in config["clusters"] if item["name"] == context["cluster"])
        server, ca_data = cluster["server"], cluster["certificate-authority-data"]
        namespace_uid = _json_command(args, "get", "namespace", args.namespace)["metadata"]["uid"]
        cluster_uid = _json_command(args, "get", "namespace", "kube-system")["metadata"]["uid"]
    except (KeyError, StopIteration, TypeError) as error:
        raise LiveOAuthV2Error("explicit kubeconfig/context lacks a complete HTTPS target identity") from error
    if not all(isinstance(value, str) and value for value in (server, ca_data, namespace_uid, cluster_uid)) or not server.startswith("https://"):
        raise LiveOAuthV2Error("explicit Kubernetes target identity is malformed")
    values = {"profile": PROFILE, "context": args.context, "namespace": args.namespace, "server": server, "ca_sha256": hashlib.sha256(ca_data.encode("ascii")).hexdigest(), "cluster_uid": cluster_uid, "namespace_uid": namespace_uid, "kubeconfig_sha256": hashlib.sha256(args.kubeconfig.read_bytes()).hexdigest()}
    return Target(args.kubeconfig, args.context, args.namespace, server, values["ca_sha256"], cluster_uid, namespace_uid, hashlib.sha256(canonical_json(values)).hexdigest())


def _deployment_preflight(args: argparse.Namespace) -> None:
    deployment = _json_command(args, "get", "deployment", args.runner_deployment, "-n", args.namespace)
    try:
        annotations = deployment["metadata"]["annotations"]
        spec = deployment["spec"]["template"]["spec"]
        containers, available = spec["containers"], deployment["status"]["availableReplicas"]
    except KeyError as error:
        raise LiveOAuthV2Error("runner deployment lacks v2 readiness evidence") from error
    reference = _oauth_reference(args)
    if not isinstance(annotations, dict) or annotations.get("tessera.dev/live-profile") != PROFILE or annotations.get("tessera.dev/openai-chatgpt-oauth-owner") != args.oauth_owner_id or annotations.get("tessera.dev/openai-chatgpt-oauth-runtime-ref-sha256") != reference.digest or annotations.get("tessera.dev/remote-workflow-protocol") != PROTOCOL or spec.get("serviceAccountName") != args.service_account or type(available) is not int or available < 1 or not isinstance(containers, list) or not any(isinstance(item, dict) and item.get("image") == args.image for item in containers):
        raise LiveOAuthV2Error("runner deployment is not ready for the exact v2 image, owner, reference, and service account")


def _manifest_digest(manifest: Mapping[str, Any]) -> str:
    value = json.loads(canonical_json(dict(manifest)))
    for annotation_map in (value.get("metadata", {}).get("annotations", {}), value.get("spec", {}).get("template", {}).get("metadata", {}).get("annotations", {})):
        annotation_map.pop("tessera.dev/spec-sha256", None)
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _manifest(args: argparse.Namespace, target: Target) -> dict[str, Any]:
    reference = _oauth_reference(args)
    conversation_id, invocation_id = LiveProfileJournal._identity(args)
    annotations = {"tessera.dev/live-profile": PROFILE, "tessera.dev/operation-id": args.operation_id, "tessera.dev/conversation-id": conversation_id, "tessera.dev/invocation-id": invocation_id, "tessera.dev/target-digest": target.target_digest, "tessera.dev/image-manifest-digest": _image_digest(args.image), "tessera.dev/oauth-owner-id": reference.owner_id, "tessera.dev/oauth-runtime-ref-sha256": reference.digest, "tessera.dev/remote-workflow-protocol": PROTOCOL}
    environment = [
        {"name": "TESSERA_OPENAI_CHATGPT_OAUTH_RUNTIME_REF", "value": reference.reference},
        {"name": "TESSERA_ALLOW_API_KEYS", "value": "false"}, {"name": "TESSERA_ALLOW_PROVIDER_FALLBACK", "value": "false"}, {"name": "TESSERA_ALLOW_TOP_UP", "value": "false"},
        {"name": "TESSERA_BILLING_MODE", "value": "chatgpt_oauth_subscription_only"}, {"name": "TESSERA_ALLOWED_TOOLS", "value": "read_experiment_status"}, {"name": "TESSERA_MAX_MODEL_CALLS_PER_TURN", "value": "1"}, {"name": "TESSERA_MAX_MODEL_CALLS", "value": "10"},
        {"name": "TESSERA_DISABLE_SHELL", "value": "true"}, {"name": "TESSERA_DISABLE_MCP", "value": "true"}, {"name": "TESSERA_DISABLE_SKILLS", "value": "true"}, {"name": "TESSERA_DISABLE_AUTOMATIC_RETRIES", "value": "true"},
        # Downward API metadata.uid is the Pod UID. A Job UID is an owner
        # identity and must be derived and attested separately.
        {"name": "TESSERA_KUBERNETES_POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}},
    ]
    command = ["/tessera-live-oauth-e2e", "run-ten-turns", "--operation-id", args.operation_id, "--conversation-id", conversation_id, "--invocation-id", invocation_id, "--model", args.model, "--turn-count", str(TURN_COUNT), "--max-model-calls-per-turn", "1", "--max-model-calls", "10", "--max-turn-tokens", str(args.max_turn_tokens), "--max-total-tokens", str(args.max_total_tokens), "--max-turn-seconds", str(args.max_turn_seconds), "--max-total-seconds", str(args.timeout)]
    manifest: dict[str, Any] = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"annotations": annotations, "labels": {"app.kubernetes.io/managed-by": "tessera", "tessera.dev/operation": args.operation_id}, "name": f"tessera-live-oauth-v2-{args.operation_id}", "namespace": args.namespace}, "spec": {"activeDeadlineSeconds": args.timeout, "backoffLimit": 0, "template": {"metadata": {"annotations": annotations, "labels": {"tessera.dev/operation": args.operation_id}}, "spec": {"automountServiceAccountToken": False, "restartPolicy": "Never", "serviceAccountName": args.service_account, "containers": [{"name": "remote-worker", "image": args.image, "imagePullPolicy": "IfNotPresent", "command": command, "env": environment, "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}, "privileged": False, "readOnlyRootFilesystem": True, "runAsNonRoot": True}}]}}}}
    annotations["tessera.dev/spec-sha256"] = _manifest_digest(manifest)
    return manifest


def _preflight(args: argparse.Namespace) -> tuple[Target, dict[str, Any], str]:
    target = _target(args); _deployment_preflight(args)
    if _kubectl(args, "auth", "can-i", "create", "jobs", "-n", args.namespace).stdout.strip() != "yes":
        raise LiveOAuthV2Error("selected identity cannot create Jobs in the explicit namespace")
    manifest = _manifest(args, target); digest = _manifest_digest(manifest)
    if manifest["metadata"]["annotations"]["tessera.dev/spec-sha256"] != digest:
        raise LiveOAuthV2Error("manifest self-digest is inconsistent")
    _kubectl(args, "create", "--dry-run=server", "-f", "-", input_text=_encoded(manifest))
    return target, manifest, digest


def _job(args: argparse.Namespace, name: str) -> dict[str, Any]:
    return _json_command(args, "get", "job", name, "-n", args.namespace)


def _job_uid(job: Mapping[str, Any]) -> str:
    value = job.get("metadata", {}).get("uid") if isinstance(job.get("metadata"), Mapping) else None
    if not isinstance(value, str) or not value:
        raise LiveOAuthV2Error("Job UID is unavailable")
    return value


def _owner_job_and_pod_uid(
    args: argparse.Namespace, name: str, expected_job_uid: str
) -> tuple[str, str]:
    """Attest the actual Job owner of the one Pod created by this Job."""
    listed = _json_command(args, "get", "pods", "-l", f"job-name={name}", "-n", args.namespace)
    items = listed.get("items")
    if not isinstance(items, list) or len(items) != 1:
        raise LiveOAuthV2Error("operation Job does not resolve exactly one Pod for identity attestation")
    pod = items[0]
    metadata = pod.get("metadata") if isinstance(pod, Mapping) else None
    if not isinstance(metadata, Mapping):
        raise LiveOAuthV2Error("operation Pod lacks identity metadata")
    pod_uid = metadata.get("uid")
    owners = metadata.get("ownerReferences")
    if not isinstance(pod_uid, str) or not pod_uid or not isinstance(owners, list):
        raise LiveOAuthV2Error("operation Pod lacks a UID or owner identity")
    job_owners = [
        owner
        for owner in owners
        if isinstance(owner, Mapping)
        and owner.get("kind") == "Job"
        and owner.get("name") == name
        and owner.get("uid") == expected_job_uid
        and owner.get("controller") is True
    ]
    if len(job_owners) != 1:
        raise LiveOAuthV2Error("operation Pod is not owned by the observed Job UID")
    return expected_job_uid, pod_uid


def _job_state(job: Mapping[str, Any]) -> str:
    status = job.get("status")
    if not isinstance(status, Mapping): return "unknown"
    for condition in status.get("conditions", []) if isinstance(status.get("conditions", []), list) else []:
        if isinstance(condition, Mapping) and condition.get("status") == "True":
            if condition.get("type") == "Complete": return "succeeded"
            if condition.get("type") == "Failed": return "failed"
    return "running" if isinstance(status.get("active"), int) and status["active"] > 0 else "pending"


def _existing_job(args: argparse.Namespace, name: str) -> bool:
    # This is a read-only duplicate probe.  It must never make another Job.
    completed = _kubectl(args, "get", "job", name, "-n", args.namespace, check=False)
    if completed.returncode == 0:
        return True
    if "NotFound" in (completed.stderr + completed.stdout):
        return False
    raise LiveOAuthV2Error("could not determine whether the operation Job already exists")


def _wait_for_start(args: argparse.Namespace, name: str) -> dict[str, Any]:
    deadline = time.monotonic() + min(args.timeout, 120)
    while time.monotonic() < deadline:
        job = _job(args, name); state = _job_state(job)
        if state in {"running", "succeeded"}: return job
        if state == "failed": raise LiveOAuthV2Error("remote runner failed before local observation pause")
        time.sleep(2)
    raise LiveOAuthV2Error("remote runner did not start before the observation deadline")


def _assert_one_identity(args: argparse.Namespace, name: str, job_uid: str, invocation_id: str) -> None:
    job = _job(args, name)
    if _job_uid(job) != job_uid:
        raise LiveOAuthV2Error("reconnect resolved a different Kubernetes Job UID")
    annotations = job.get("metadata", {}).get("annotations", {}) if isinstance(job.get("metadata"), Mapping) else {}
    if not isinstance(annotations, Mapping) or annotations.get("tessera.dev/operation-id") != args.operation_id or annotations.get("tessera.dev/invocation-id") != invocation_id:
        raise LiveOAuthV2Error("reconnect Job does not retain operation and invocation identity")


def _assert_no_duplicate_execution(args: argparse.Namespace, name: str, job_uid: str) -> None:
    """Read the operation label only; this probe cannot create a replacement Job."""
    listed = _json_command(args, "get", "jobs", "-l", f"tessera.dev/operation={args.operation_id}", "-n", args.namespace)
    items = listed.get("items")
    if not isinstance(items, list) or len(items) != 1:
        raise LiveOAuthV2Error("operation label resolves zero or multiple Jobs; duplicate execution requires reconciliation")
    item = items[0]
    metadata = item.get("metadata") if isinstance(item, Mapping) else None
    if not isinstance(metadata, Mapping) or metadata.get("name") != name or metadata.get("uid") != job_uid:
        raise LiveOAuthV2Error("operation label resolves a different Job identity")


def _attestation(args: argparse.Namespace, name: str, conversation_id: str, invocation_id: str, job_uid: str, pod_uid: str) -> dict[str, Any]:
    output = _kubectl(args, "logs", f"job/{name}", "-n", args.namespace, timeout=60).stdout
    for line in reversed(output.splitlines()):
        try: value = json.loads(line)
        except json.JSONDecodeError: continue
        if isinstance(value, dict):
            required = {"operation_id": args.operation_id, "conversation_id": conversation_id, "invocation_id": invocation_id, "job_uid": job_uid, "pod_uid": pod_uid, "provider": PROVIDER, "model": args.model, "turn_count": TURN_COUNT, "model_calls": TURN_COUNT, "billing_mode": "chatgpt_oauth_subscription_only", "api_key_used": False, "fallback_used": False, "top_up_used": False, "automatic_retries_used": False, "shell_used": False, "mcp_used": False, "skills_used": False, "remote_workflow_protocol": PROTOCOL}
            if all(value.get(field) == expected for field, expected in required.items()): return value
    raise LiveOAuthV2Error("remote runner did not attest the bound subscription-only ten-turn profile")


def _validate_attestation(args: argparse.Namespace, value: Mapping[str, Any]) -> list[dict[str, Any]]:
    if value.get("usage_known") is not True:
        raise LiveOAuthV2Error("provider usage is unknown and requires reconciliation")
    turns = value.get("turns")
    aggregate = value.get("usage")
    if not isinstance(turns, list) or len(turns) != TURN_COUNT or not isinstance(aggregate, Mapping):
        raise LiveOAuthV2Error("attestation lacks exactly ten turn records and aggregate usage")
    normalized: list[dict[str, Any]] = []
    for number, turn in enumerate(turns, 1):
        if not isinstance(turn, Mapping) or turn.get("turn_number") != number or turn.get("model_calls") != 1 or turn.get("tool_calls") != ["read_experiment_status"] or turn.get("model") != args.model or turn.get("elapsed_seconds") is None or not isinstance(turn.get("usage"), Mapping):
            raise LiveOAuthV2Error("each turn must attest exactly one permitted status read and model call")
        usage = turn["usage"]
        tokens, elapsed = usage.get("total_tokens"), turn["elapsed_seconds"]
        if type(tokens) is not int or tokens < 0 or tokens > args.max_turn_tokens or type(elapsed) is not int or elapsed < 0 or elapsed > args.max_turn_seconds:
            raise LiveOAuthV2Error("per-turn token or time bound was exceeded or malformed")
        normalized.append(dict(turn))
    total_tokens = aggregate.get("total_tokens"); elapsed = aggregate.get("elapsed_seconds")
    if type(total_tokens) is not int or total_tokens != sum(item["usage"]["total_tokens"] for item in normalized) or total_tokens > args.max_total_tokens or type(elapsed) is not int or elapsed < 0 or elapsed > args.timeout or value.get("model_calls") != sum(item["model_calls"] for item in normalized):
        raise LiveOAuthV2Error("aggregate token, time, or model-call bound was exceeded or malformed")
    return normalized


def _run_live(args: argparse.Namespace) -> dict[str, Any]:
    target, manifest, digest = _preflight(args)
    name = str(manifest["metadata"]["name"])
    with LiveProfileJournal(args.journal) as journal:
        journal.require_preflight(args, target, digest)
        conversation_id, invocation_id = journal.reserve_and_claim_confirmation(args, digest)
        job_uid: str | None = None
        pod_uid: str | None = None
        try:
            if _existing_job(args, name):
                journal.reconciliation(args.operation_id, "operation_job_already_exists_before_submit", job_uid=None, pod_uid=None, evidence={"create_attempted": False})
                raise LiveOAuthV2Error("existing Job requires reconciliation; this fixture will not submit another Job")
            journal.transition(args.operation_id, "submitting")
            _kubectl(args, "create", "-f", "-", input_text=_encoded(manifest))
            journal.transition(args.operation_id, "submitted")
            initial = _wait_for_start(args, name); job_uid = _job_uid(initial)
            _assert_one_identity(args, name, job_uid, invocation_id)
            _assert_no_duplicate_execution(args, name, job_uid)
            owner_job_uid, pod_uid = _owner_job_and_pod_uid(args, name, job_uid)
            journal.transition(args.operation_id, "local_observation_paused", job_uid=owner_job_uid, pod_uid=pod_uid)
            journal.evidence(args.operation_id, "local_observation_paused", {"remote_progress_not_observed_during_pause": True, "job_uid": owner_job_uid, "pod_uid": pod_uid, "operation_id": args.operation_id, "invocation_id": invocation_id})
            time.sleep(args.disconnect_seconds)
            reconnected = _job(args, name); _assert_one_identity(args, name, job_uid, invocation_id)
            _assert_no_duplicate_execution(args, name, job_uid)
            reconnected_job_uid, reconnected_pod_uid = _owner_job_and_pod_uid(args, name, job_uid)
            if (reconnected_job_uid, reconnected_pod_uid) != (job_uid, pod_uid):
                raise LiveOAuthV2Error("reconnect resolved a replacement Pod or a different owner Job UID")
            state = _job_state(reconnected)
            if state in {"failed", "unknown", "pending"}:
                raise LiveOAuthV2Error("remote progress cannot be established after local observation pause")
            journal.transition(args.operation_id, "reconnected", job_uid=job_uid, pod_uid=pod_uid)
            journal.evidence(args.operation_id, "remote_progress", {"source": "kubernetes_reconnect", "state": state, "same_job_uid": True, "same_pod_uid": True, "job_uid": job_uid, "pod_uid": pod_uid, "operation_id": args.operation_id, "invocation_id": invocation_id})
            waited = _kubectl(args, "wait", "--for=condition=complete", f"job/{name}", "-n", args.namespace, f"--timeout={args.timeout}s", timeout=args.timeout + 10, check=False)
            final = _job(args, name); _assert_one_identity(args, name, job_uid, invocation_id)
            _assert_no_duplicate_execution(args, name, job_uid)
            if waited.returncode != 0 or _job_state(final) != "succeeded":
                raise LiveOAuthV2Error("remote operation requires reconciliation; no replacement will be submitted")
            attestation = _attestation(args, name, conversation_id, invocation_id, job_uid, pod_uid)
            turns = _validate_attestation(args, attestation)
            journal.record_turns(args.operation_id, turns)
            provider_id = KubernetesProviderIdentity(target.cluster_uid, target.namespace_uid, job_uid).provider_id
            journal.transition(args.operation_id, "succeeded", job_uid=job_uid, pod_uid=pod_uid)
        except Exception as error:
            journal.reconciliation(args.operation_id, "uncertain_create_submit_report_or_post_submit", job_uid=job_uid, pod_uid=pod_uid, evidence={"error": str(error)[:1000], "automatic_resubmit": False})
            raise
    return {"fixture_verdict": "passed", "state": "succeeded", "operation_id": args.operation_id, "conversation_id": conversation_id, "invocation_id": invocation_id, "job": name, "job_uid": job_uid, "pod_uid": pod_uid, "provider_id": provider_id, "manifest_digest": digest, "target_digest": target.target_digest, "turns": TURN_COUNT, "billing_mode": "chatgpt_oauth_subscription_only", "reconnect_evidence": "local_observation_paused_then_remote_progress", "journal": str(args.journal), "retained": "Job and private journal are retained; no automatic deletion or resubmission was issued"}


def _validate_args(args: argparse.Namespace) -> str | None:
    if sum(bool(getattr(args, mode)) for mode in ("preflight", "run", "cancel")) != 1: return "supply exactly one of --preflight, --run, or --cancel"
    required = ("kubeconfig", "context", "namespace", "runner_deployment", "service_account", "image", "oauth_owner_id", "oauth_runtime_ref", "model", "operation_id", "journal")
    if any(getattr(args, field) in {None, ""} for field in required): return "all target, image, owner-bound runtime reference, operation, and journal arguments are required"
    if not args.kubeconfig.is_absolute() or not args.kubeconfig.is_file() or args.kubeconfig.is_symlink(): return "--kubeconfig must name an existing absolute regular file"
    if not (_NAME.fullmatch(args.namespace) and _NAME.fullmatch(args.runner_deployment) and _NAME.fullmatch(args.service_account)): return "namespace, runner deployment, and service account must be lowercase Kubernetes names"
    if not _HEX.fullmatch(args.operation_id): return "--operation-id must be exactly 32 lowercase hexadecimal characters"
    if not _MODEL.fullmatch(args.model): return "--model is malformed"
    if not 30 <= args.timeout <= 900 or not 5 <= args.disconnect_seconds <= 120: return "--timeout must be 30-900 and --disconnect-seconds must be 5-120"
    if not 1 <= args.max_turn_tokens <= 100_000 or not args.max_turn_tokens <= args.max_total_tokens <= 1_000_000: return "token bounds must be positive and aggregate must cover one turn"
    if not 1 <= args.max_turn_seconds <= args.timeout: return "--max-turn-seconds must be positive and not exceed --timeout"
    try: _image_digest(args.image); _oauth_reference(args)
    except LiveOAuthV2Error as error: return str(error)
    if not args.run and (args.confirm_manifest_digest or args.confirm_provider_calls): return "confirmation flags are valid only with --run"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(); modes.add_argument("--preflight", action="store_true"); modes.add_argument("--run", action="store_true"); modes.add_argument("--cancel", action="store_true", help="record cancellation requested only; never terminates a Job")
    parser.add_argument("--kubeconfig", type=Path); parser.add_argument("--context"); parser.add_argument("--namespace"); parser.add_argument("--runner-deployment"); parser.add_argument("--service-account"); parser.add_argument("--image"); parser.add_argument("--oauth-owner-id"); parser.add_argument("--oauth-runtime-ref"); parser.add_argument("--model"); parser.add_argument("--operation-id"); parser.add_argument("--journal", type=Path); parser.add_argument("--timeout", type=int, default=600); parser.add_argument("--disconnect-seconds", type=int, default=15); parser.add_argument("--max-turn-tokens", type=int, default=2_000); parser.add_argument("--max-total-tokens", type=int, default=20_000); parser.add_argument("--max-turn-seconds", type=int, default=60); parser.add_argument("--confirm-manifest-digest"); parser.add_argument("--confirm-provider-calls")
    args = parser.parse_args()
    if not (args.preflight or args.run or args.cancel):
        print(json.dumps({"fixture_verdict": "skipped", "state": "not_started", "message": "no live mode was selected"}, sort_keys=True)); return 1
    error = _validate_args(args)
    if error is not None: print(json.dumps({"fixture_verdict": "failed", "state": "not_started", "message": error}, sort_keys=True)); return 1
    try:
        if args.preflight:
            target, _manifest_value, digest = _preflight(args)
            with LiveProfileJournal(args.journal) as journal: journal.preflight(args, target, digest)
            result = {"fixture_verdict": "preflight_passed", "state": "preflight_passed", "operation_id": args.operation_id, "manifest_digest": digest, "target_digest": target.target_digest, "turns": TURN_COUNT, "provider_call": "not_started", "journal": str(args.journal)}
        elif args.cancel:
            with LiveProfileJournal(args.journal) as journal: journal.cancel(args.operation_id)
            result = {"fixture_verdict": "cancel_requested", "state": "cancel_requested", "operation_id": args.operation_id, "provider_termination": "not_attempted", "journal": str(args.journal)}
        else: result = _run_live(args)
    except (LiveOAuthV2Error, OSError, sqlite3.Error, subprocess.SubprocessError, ValueError) as exception:
        result = {"fixture_verdict": "failed", "state": "failed", "message": str(exception)}
    print(json.dumps(result, sort_keys=True)); return 0 if result["fixture_verdict"] in {"passed", "preflight_passed", "cancel_requested"} else 1


if __name__ == "__main__": raise SystemExit(main())
