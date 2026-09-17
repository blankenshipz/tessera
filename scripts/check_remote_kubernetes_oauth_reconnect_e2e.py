"""Run one explicit, retained remote Kubernetes OpenAI OAuth reconnect fixture.

This is deliberately outside pytest and CI.  It accepts only an explicit target,
an exact runner image digest, and opaque owner-bound runtime references.  It never
accepts an API key, provider fallback, or billing top-up setting.
"""

# ruff: noqa: E501
# fmt: off

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tessera_worker.kubernetes_contracts import KubernetesProviderIdentity, canonical_json

PROFILE = "remote-kubernetes-openai-chatgpt-oauth-e2e-v1"
PROVIDER = "openai-chatgpt-oauth"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_OPERATION = re.compile(r"[0-9a-f]{32}\Z")
_NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")


class LiveOAuthE2EError(RuntimeError):
    """An opt-in provider fixture cannot safely proceed."""


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


class LiveProfileJournal:
    """Private local provenance, usage, and reconciliation records for this live profile.

    These tables intentionally do not share provider-disabled operation or receipt
    records.  A journal records observed fixture evidence only; it is not control
    plane authority and it never stores an OAuth bearer token.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser().resolve()
        if self.path.exists() and (self.path.is_symlink() or not self.path.is_file()):
            raise LiveOAuthE2EError("live profile journal must be a regular file")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA journal_mode=DELETE")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(
            """CREATE TABLE IF NOT EXISTS live_profile_runs (
            operation_id TEXT PRIMARY KEY, profile TEXT NOT NULL, state TEXT NOT NULL,
            target_digest TEXT NOT NULL, manifest_digest TEXT NOT NULL, image_digest TEXT NOT NULL,
            oauth_owner_id TEXT NOT NULL, oauth_runtime_ref_sha256 TEXT NOT NULL,
            model TEXT NOT NULL, max_model_calls INTEGER NOT NULL CHECK(max_model_calls=1),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS live_profile_usage (
            operation_id TEXT PRIMARY KEY REFERENCES live_profile_runs(operation_id),
            usage_json TEXT NOT NULL, recorded_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS live_profile_reconciliation (
            operation_id TEXT PRIMARY KEY REFERENCES live_profile_runs(operation_id),
            job_uid TEXT, observed_state TEXT NOT NULL, reconciliation_required INTEGER NOT NULL,
            evidence_json TEXT NOT NULL, recorded_at TEXT NOT NULL);"""
        )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> LiveProfileJournal:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def preflight(self, args: argparse.Namespace, target: Target, manifest_digest: str) -> None:
        now = _now()
        values = {
            "operation_id": args.operation_id,
            "profile": PROFILE,
            "state": "preflight_passed",
            "target_digest": target.target_digest,
            "manifest_digest": manifest_digest,
            "image_digest": _image_digest(args.image),
            "oauth_owner_id": args.oauth_owner_id,
            "oauth_runtime_ref_sha256": _oauth_reference(args).digest,
            "model": args.model,
            "max_model_calls": 1,
            "created_at": now,
            "updated_at": now,
        }
        existing = self.connection.execute(
            "SELECT * FROM live_profile_runs WHERE operation_id=?", (args.operation_id,)
        ).fetchone()
        if existing is not None:
            expected = tuple(
                values[field] for field in values if field not in {"state", "created_at", "updated_at"}
            )
            actual = tuple(existing[index] for index in (0, 1, 3, 4, 5, 6, 7, 8, 9))
            if actual != expected:
                raise LiveOAuthE2EError("live profile journal conflicts with immutable preflight")
            self.connection.execute(
                "UPDATE live_profile_runs SET state=?,updated_at=? WHERE operation_id=?",
                ("preflight_passed", now, args.operation_id),
            )
        else:
            self.connection.execute(
                """INSERT INTO live_profile_runs
                (operation_id,profile,state,target_digest,manifest_digest,image_digest,oauth_owner_id,
                 oauth_runtime_ref_sha256,model,max_model_calls,created_at,updated_at)
                VALUES (:operation_id,:profile,:state,:target_digest,:manifest_digest,:image_digest,
                :oauth_owner_id,:oauth_runtime_ref_sha256,:model,:max_model_calls,:created_at,:updated_at)""",
                values,
            )
        self.connection.commit()

    def require_preflight(self, args: argparse.Namespace, target: Target, manifest_digest: str) -> None:
        row = self.connection.execute(
            """SELECT profile,target_digest,manifest_digest,image_digest,oauth_owner_id,
            oauth_runtime_ref_sha256,model,max_model_calls,state FROM live_profile_runs WHERE operation_id=?""",
            (args.operation_id,),
        ).fetchone()
        expected = (
            PROFILE,
            target.target_digest,
            manifest_digest,
            _image_digest(args.image),
            args.oauth_owner_id,
            _oauth_reference(args).digest,
            args.model,
            1,
        )
        if row is None or tuple(row[:8]) != expected or row[8] != "preflight_passed":
            raise LiveOAuthE2EError("a matching successful preflight record is required before --run")

    def transition(self, operation_id: str, state: str) -> None:
        if self.connection.execute(
            "UPDATE live_profile_runs SET state=?,updated_at=? WHERE operation_id=?",
            (state, _now(), operation_id),
        ).rowcount != 1:
            raise LiveOAuthE2EError("live profile preflight record disappeared")
        self.connection.commit()

    def reconciliation(
        self, operation_id: str, job_uid: str | None, observed_state: str, required: bool, evidence: Mapping[str, Any]
    ) -> None:
        self.connection.execute(
            """INSERT INTO live_profile_reconciliation
            (operation_id,job_uid,observed_state,reconciliation_required,evidence_json,recorded_at)
            VALUES (?,?,?,?,?,?) ON CONFLICT(operation_id) DO UPDATE SET job_uid=excluded.job_uid,
            observed_state=excluded.observed_state,reconciliation_required=excluded.reconciliation_required,
            evidence_json=excluded.evidence_json,recorded_at=excluded.recorded_at""",
            (operation_id, job_uid, observed_state, int(required), _encoded(evidence), _now()),
        )
        self.connection.commit()

    def usage(self, operation_id: str, usage: Mapping[str, Any]) -> None:
        self.connection.execute(
            """INSERT INTO live_profile_usage(operation_id,usage_json,recorded_at) VALUES (?,?,?)
            ON CONFLICT(operation_id) DO NOTHING""",
            (operation_id, _encoded(usage), _now()),
        )
        self.connection.commit()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _encoded(value: Mapping[str, Any]) -> str:
    return canonical_json(dict(value)).decode("utf-8")


def _image_digest(image: str) -> str:
    if "@" not in image:
        raise LiveOAuthE2EError("--image must be an immutable repository@sha256 reference")
    _repository, digest = image.rsplit("@", 1)
    if not _DIGEST.fullmatch(digest):
        raise LiveOAuthE2EError("--image must end with a lowercase sha256 digest")
    return digest


def _oauth_reference(args: argparse.Namespace) -> OAuthRuntimeReference:
    parsed = urlparse(args.oauth_runtime_ref)
    if (
        parsed.scheme != PROVIDER
        or parsed.netloc != args.oauth_owner_id
        or not parsed.path.strip("/")
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise LiveOAuthE2EError(
            "--oauth-runtime-ref must be openai-chatgpt-oauth://<--oauth-owner-id>/<opaque-reference>"
        )
    return OAuthRuntimeReference(args.oauth_owner_id, args.oauth_runtime_ref)


def _run(argv: list[str], *, input_text: str | None = None, check: bool = True, timeout: int = 60):
    return subprocess.run(
        argv, input=input_text, capture_output=True, text=True, check=check, timeout=timeout
    )


def _kubectl(args: argparse.Namespace, *parts: str, input_text: str | None = None, check: bool = True, timeout: int = 60):
    return _run(
        ["kubectl", "--kubeconfig", str(args.kubeconfig), "--context", args.context, *parts],
        input_text=input_text,
        check=check,
        timeout=timeout,
    )


def _json_command(args: argparse.Namespace, *parts: str) -> dict[str, Any]:
    completed = _kubectl(args, *parts, "-o", "json")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise LiveOAuthE2EError("Kubernetes returned malformed JSON during preflight") from error
    if not isinstance(value, dict):
        raise LiveOAuthE2EError("Kubernetes returned a non-object during preflight")
    return value


def _target(args: argparse.Namespace) -> Target:
    config = _json_command(args, "config", "view", "--raw", "--minify")
    contexts = config.get("contexts")
    clusters = config.get("clusters")
    try:
        context = next(item["context"] for item in contexts if item["name"] == args.context)
        cluster = next(item["cluster"] for item in clusters if item["name"] == context["cluster"])
        server = cluster["server"]
        ca_data = cluster["certificate-authority-data"]
    except (KeyError, StopIteration, TypeError) as error:
        raise LiveOAuthE2EError("explicit kubeconfig/context lacks an HTTPS cluster identity") from error
    if not isinstance(server, str) or not server.startswith("https://") or not isinstance(ca_data, str):
        raise LiveOAuthE2EError("explicit kubeconfig/context lacks an HTTPS server or CA identity")
    namespace = _json_command(args, "get", "namespace", args.namespace)
    cluster_namespace = _json_command(args, "get", "namespace", "kube-system")
    try:
        namespace_uid = namespace["metadata"]["uid"]
        cluster_uid = cluster_namespace["metadata"]["uid"]
    except KeyError as error:
        raise LiveOAuthE2EError("target namespace identity is unavailable") from error
    if not isinstance(namespace_uid, str) or not isinstance(cluster_uid, str):
        raise LiveOAuthE2EError("target namespace identity is malformed")
    kubeconfig_bytes = args.kubeconfig.read_bytes()
    target_data = {
        "ca_sha256": hashlib.sha256(ca_data.encode("ascii")).hexdigest(),
        "cluster_uid": cluster_uid,
        "context": args.context,
        "kubeconfig_sha256": hashlib.sha256(kubeconfig_bytes).hexdigest(),
        "namespace": args.namespace,
        "namespace_uid": namespace_uid,
        "profile": PROFILE,
        "server": server,
    }
    return Target(
        args.kubeconfig,
        args.context,
        args.namespace,
        server,
        target_data["ca_sha256"],
        cluster_uid,
        namespace_uid,
        hashlib.sha256(canonical_json(target_data)).hexdigest(),
    )


def _deployment_preflight(args: argparse.Namespace) -> None:
    deployment = _json_command(args, "get", "deployment", args.runner_deployment, "-n", args.namespace)
    try:
        annotations = deployment["metadata"]["annotations"]
        pod_spec = deployment["spec"]["template"]["spec"]
        containers = pod_spec["containers"]
        available = deployment["status"]["availableReplicas"]
    except KeyError as error:
        raise LiveOAuthE2EError("runner deployment lacks required ready-profile evidence") from error
    reference = _oauth_reference(args)
    if (
        not isinstance(annotations, dict)
        or annotations.get("tessera.dev/live-profile") != PROFILE
        or annotations.get("tessera.dev/openai-chatgpt-oauth-owner") != args.oauth_owner_id
        or annotations.get("tessera.dev/openai-chatgpt-oauth-runtime-ref-sha256") != reference.digest
        or annotations.get("tessera.dev/remote-workflow-protocol") != "v43"
        or pod_spec.get("serviceAccountName") != args.service_account
        or type(available) is not int
        or available < 1
        or not isinstance(containers, list)
        or not any(isinstance(item, dict) and item.get("image") == args.image for item in containers)
    ):
        raise LiveOAuthE2EError(
            "runner deployment is not ready for this exact image, owner, runtime reference, and service account"
        )


def _manifest(args: argparse.Namespace, target: Target) -> dict[str, Any]:
    reference = _oauth_reference(args)
    operation = args.operation_id
    name = f"tessera-live-oauth-{operation}"
    annotations = {
        "tessera.dev/image-manifest-digest": _image_digest(args.image),
        "tessera.dev/live-profile": PROFILE,
        "tessera.dev/oauth-owner-id": reference.owner_id,
        "tessera.dev/oauth-runtime-ref-sha256": reference.digest,
        "tessera.dev/operation-id": operation,
        "tessera.dev/remote-workflow-protocol": "v43",
        "tessera.dev/target-digest": target.target_digest,
    }
    manifest: dict[str, Any] = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "annotations": annotations,
            "labels": {"app.kubernetes.io/managed-by": "tessera", "tessera.dev/operation": operation},
            "name": name,
            "namespace": args.namespace,
        },
        "spec": {
            "activeDeadlineSeconds": args.timeout,
            "backoffLimit": 0,
            "template": {
                "metadata": {"annotations": annotations, "labels": {"tessera.dev/operation": operation}},
                "spec": {
                    "automountServiceAccountToken": False,
                    "containers": [
                        {
                            "command": [
                                "/tessera-live-oauth-e2e",
                                "run",
                                "--operation-id",
                                operation,
                                "--model",
                                args.model,
                                "--max-model-calls",
                                "1",
                                "--hold-after-model-call-seconds",
                                str(args.disconnect_seconds),
                            ],
                            "env": [
                                {"name": "TESSERA_OPENAI_CHATGPT_OAUTH_RUNTIME_REF", "value": reference.reference},
                                {"name": "TESSERA_ALLOW_API_KEYS", "value": "false"},
                                {"name": "TESSERA_ALLOW_PROVIDER_FALLBACK", "value": "false"},
                                {"name": "TESSERA_ALLOW_TOP_UP", "value": "false"},
                                {"name": "TESSERA_MAX_MODEL_CALLS", "value": "1"},
                                {"name": "TESSERA_REMOTE_WORKFLOW_PROTOCOL", "value": "v43"},
                            ],
                            "image": args.image,
                            "imagePullPolicy": "IfNotPresent",
                            "name": "remote-worker",
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                                "privileged": False,
                                "readOnlyRootFilesystem": True,
                                "runAsNonRoot": True,
                            },
                        }
                    ],
                    "restartPolicy": "Never",
                    "serviceAccountName": args.service_account,
                },
            },
        },
    }
    annotations["tessera.dev/spec-sha256"] = _manifest_digest(manifest)
    return manifest


def _manifest_digest(manifest: Mapping[str, Any]) -> str:
    value = json.loads(canonical_json(dict(manifest)))
    for annotation_map in (
        value.get("metadata", {}).get("annotations", {}),
        value.get("spec", {}).get("template", {}).get("metadata", {}).get("annotations", {}),
    ):
        annotation_map.pop("tessera.dev/spec-sha256", None)
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _preflight(args: argparse.Namespace) -> tuple[Target, dict[str, Any], str]:
    target = _target(args)
    _deployment_preflight(args)
    permitted = _kubectl(args, "auth", "can-i", "create", "jobs", "-n", args.namespace).stdout.strip()
    if permitted != "yes":
        raise LiveOAuthE2EError("selected identity cannot create Jobs in the explicit namespace")
    manifest = _manifest(args, target)
    digest = _manifest_digest(manifest)
    if manifest["metadata"]["annotations"]["tessera.dev/spec-sha256"] != digest:
        raise LiveOAuthE2EError("manifest self-digest is inconsistent")
    _kubectl(args, "create", "--dry-run=server", "-f", "-", input_text=_encoded(manifest))
    return target, manifest, digest


def _job(args: argparse.Namespace, name: str) -> dict[str, Any]:
    return _json_command(args, "get", "job", name, "-n", args.namespace)


def _job_state(job: Mapping[str, Any]) -> str:
    status = job.get("status")
    if not isinstance(status, Mapping):
        return "unknown"
    conditions = status.get("conditions", [])
    if isinstance(conditions, list):
        for condition in conditions:
            if isinstance(condition, Mapping) and condition.get("status") == "True":
                if condition.get("type") == "Complete":
                    return "succeeded"
                if condition.get("type") == "Failed":
                    return "failed"
    return "running" if isinstance(status.get("active"), int) and status["active"] > 0 else "pending"


def _job_uid(job: Mapping[str, Any]) -> str:
    try:
        value = job["metadata"]["uid"]
    except KeyError as error:
        raise LiveOAuthE2EError("created Job lacks a UID") from error
    if not isinstance(value, str) or not value:
        raise LiveOAuthE2EError("created Job UID is malformed")
    return value


def _wait_for_start(args: argparse.Namespace, name: str) -> dict[str, Any]:
    deadline = time.monotonic() + min(args.timeout, 120)
    while time.monotonic() < deadline:
        job = _job(args, name)
        state = _job_state(job)
        if state in {"running", "succeeded"}:
            return job
        if state == "failed":
            raise LiveOAuthE2EError("remote worker failed before desktop disconnect")
        time.sleep(2)
    raise LiveOAuthE2EError("remote worker did not start before the disconnect deadline")


def _assert_duplicate_rejected(args: argparse.Namespace, manifest: Mapping[str, Any]) -> None:
    repeated = _kubectl(
        args, "create", "-f", "-", input_text=_encoded(manifest), check=False, timeout=60
    )
    if repeated.returncode == 0 or "AlreadyExists" not in (repeated.stderr + repeated.stdout):
        raise LiveOAuthE2EError("duplicate execution was not rejected by the existing operation identity")


def _attestation(args: argparse.Namespace, name: str) -> dict[str, Any]:
    output = _kubectl(args, "logs", f"job/{name}", "-n", args.namespace, timeout=60).stdout
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            required = {
                "operation_id": args.operation_id,
                "provider": PROVIDER,
                "model": args.model,
                "model_calls": 1,
                "api_key_used": False,
                "fallback_used": False,
                "top_up_used": False,
                "remote_workflow_protocol": "v43",
                "control_continued": True,
                "same_operation_reconnected": True,
                "checkpoint_sequence": 1,
            }
            if all(value.get(field) == expected for field, expected in required.items()):
                usage = value.get("usage")
                if not isinstance(usage, dict):
                    raise LiveOAuthE2EError("remote runner did not attest structured usage")
                return usage
    raise LiveOAuthE2EError("remote runner did not attest one OAuth model call without fallback")


def _run_live(args: argparse.Namespace) -> dict[str, Any]:
    target, manifest, digest = _preflight(args)
    if args.confirm_manifest_digest != digest:
        raise LiveOAuthE2EError("--confirm-manifest-digest must equal this preflight manifest digest")
    if args.confirm_provider_call != f"ONE_OAUTH_MODEL_CALL:{digest}":
        raise LiveOAuthE2EError("--confirm-provider-call must be ONE_OAUTH_MODEL_CALL:<manifest-digest>")
    name = manifest["metadata"]["name"]
    with LiveProfileJournal(args.journal) as journal:
        journal.require_preflight(args, target, digest)
        _kubectl(args, "create", "-f", "-", input_text=_encoded(manifest))
        journal.transition(args.operation_id, "submitted")
        initial = _wait_for_start(args, name)
        job_uid = _job_uid(initial)
        journal.transition(args.operation_id, "desktop_disconnected")
        # There is deliberately no desktop/control client connection during this interval.
        time.sleep(args.disconnect_seconds)
        reconnected = _job(args, name)
        if _job_uid(reconnected) != job_uid:
            raise LiveOAuthE2EError("reconnect did not resolve the same remote operation Job UID")
        state = _job_state(reconnected)
        if state == "failed":
            journal.reconciliation(args.operation_id, job_uid, state, True, {"phase": "reconnect"})
            raise LiveOAuthE2EError("remote worker failed while the desktop client was disconnected")
        journal.transition(args.operation_id, "reconnected")
        _assert_duplicate_rejected(args, manifest)
        waited = _kubectl(
            args,
            "wait",
            "--for=condition=complete",
            f"job/{name}",
            "-n",
            args.namespace,
            f"--timeout={args.timeout}s",
            timeout=args.timeout + 10,
            check=False,
        )
        final = _job(args, name)
        final_state = _job_state(final)
        if waited.returncode != 0 or final_state != "succeeded" or _job_uid(final) != job_uid:
            journal.reconciliation(
                args.operation_id,
                job_uid,
                final_state,
                True,
                {"wait_output": (waited.stderr or waited.stdout)[-1000:]},
            )
            raise LiveOAuthE2EError("remote operation requires reconciliation; no replacement was submitted")
        usage = _attestation(args, name)
        provider_id = KubernetesProviderIdentity(
            target.cluster_uid, target.namespace_uid, job_uid
        ).provider_id
        journal.usage(args.operation_id, usage)
        journal.reconciliation(
            args.operation_id,
            job_uid,
            "succeeded",
            False,
            {"duplicate_execution": "rejected", "provider_id": provider_id, "same_job_uid": True},
        )
        journal.transition(args.operation_id, "succeeded")
    return {
        "fixture_verdict": "passed",
        "state": "succeeded",
        "operation_id": args.operation_id,
        "job": name,
        "job_uid": job_uid,
        "provider_id": provider_id,
        "manifest_digest": digest,
        "target_digest": target.target_digest,
        "duplicate_execution": "rejected",
        "desktop_disconnect": "remote_job_reconnected_by_same_uid",
        "journal": str(args.journal),
        "retained": "Job and local live-profile journal; no automatic deletion was issued",
    }


def _validate_args(args: argparse.Namespace) -> str | None:
    if args.preflight == args.run:
        return "supply exactly one of --preflight or --run"
    required = ("kubeconfig", "context", "namespace", "runner_deployment", "service_account", "image", "oauth_owner_id", "oauth_runtime_ref", "model", "operation_id", "journal")
    if any(getattr(args, field) in {None, ""} for field in required):
        return "all target, image, owner-bound runtime reference, operation, and journal arguments are required"
    if not args.kubeconfig.is_absolute() or not args.kubeconfig.is_file() or args.kubeconfig.is_symlink():
        return "--kubeconfig must name an existing absolute regular file"
    if not (_NAME.fullmatch(args.namespace) and _NAME.fullmatch(args.runner_deployment) and _NAME.fullmatch(args.service_account)):
        return "namespace, runner deployment, and service account must be lowercase Kubernetes names"
    if not _OPERATION.fullmatch(args.operation_id):
        return "--operation-id must be exactly 32 lowercase hexadecimal characters"
    if not _MODEL.fullmatch(args.model):
        return "--model is malformed"
    if not 30 <= args.timeout <= 900 or not 5 <= args.disconnect_seconds <= 120:
        return "--timeout must be 30-900 and --disconnect-seconds must be 5-120"
    try:
        _image_digest(args.image)
        _oauth_reference(args)
    except LiveOAuthE2EError as error:
        return str(error)
    if args.preflight and (args.confirm_manifest_digest or args.confirm_provider_call):
        return "confirmation flags are valid only with --run"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true", help="validate only; no provider call")
    mode.add_argument("--run", action="store_true", help="perform one explicitly confirmed provider call")
    parser.add_argument("--kubeconfig", type=Path)
    parser.add_argument("--context")
    parser.add_argument("--namespace")
    parser.add_argument("--runner-deployment")
    parser.add_argument("--service-account")
    parser.add_argument("--image", help="immutable remote runner repository@sha256:...")
    parser.add_argument("--oauth-owner-id")
    parser.add_argument("--oauth-runtime-ref", help="opaque openai-chatgpt-oauth://owner/reference")
    parser.add_argument("--model")
    parser.add_argument("--operation-id", help="new 32-character hexadecimal operation identity")
    parser.add_argument("--journal", type=Path, help="private local SQLite provenance journal")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--disconnect-seconds", type=int, default=15)
    parser.add_argument("--confirm-manifest-digest")
    parser.add_argument("--confirm-provider-call")
    args = parser.parse_args()
    if not args.preflight and not args.run:
        print(json.dumps({"fixture_verdict": "skipped", "state": "not_started", "message": "no live mode was selected"}, sort_keys=True))
        return 1
    error = _validate_args(args)
    if error is not None:
        print(json.dumps({"fixture_verdict": "failed", "state": "not_started", "message": error}, sort_keys=True))
        return 1
    try:
        if args.preflight:
            target, _manifest_value, digest = _preflight(args)
            with LiveProfileJournal(args.journal) as journal:
                journal.preflight(args, target, digest)
            result = {
                "fixture_verdict": "preflight_passed",
                "state": "preflight_passed",
                "operation_id": args.operation_id,
                "manifest_digest": digest,
                "target_digest": target.target_digest,
                "journal": str(args.journal),
                "provider_call": "not_started",
            }
        else:
            result = _run_live(args)
    except (LiveOAuthE2EError, OSError, subprocess.SubprocessError, ValueError) as exception:
        result = {"fixture_verdict": "failed", "state": "failed", "message": str(exception)}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["fixture_verdict"] in {"passed", "preflight_passed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
