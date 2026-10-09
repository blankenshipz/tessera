"""Explicit, opt-in Minikube persistent-output development fixture.

Importing this module and rendering its manifest never contacts Kubernetes.  The
only mutating path is :func:`run_fixture`, called by the script with ``--run``.
It is conformance evidence only, never provider execution or result acceptance.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from .kubernetes_contracts import (
    KubernetesClusterConfig,
    KubernetesContractError,
    KubernetesJobBinding,
    KubernetesJobJournal,
    KubernetesUnresolvedError,
    canonical_json,
    job_manifest,
    parse_pod,
)

PROFILE = "development-minikube-opencode-v1"
HELLO_PATH = "hello.txt"
HELLO_BYTES = b"hello world\n"
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_UID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_COMMAND_TIMEOUT_SECONDS = 15
_WAIT_TIMEOUT_SECONDS = 90
_MAX_OUTPUT_BYTES = 16 * 1024
NETWORK_POLICY_CAPABILITY = "network-policy-enforced-v1"
NETWORK_POLICY_SELECTOR = {"matchLabels": {"app.kubernetes.io/managed-by": "tessera"}}


@dataclass(frozen=True)
class PersistentOutputTarget:
    kubeconfig: Path
    context: str
    server: str
    ca_file: Path
    namespace: str
    profile: str
    minikube_profile: str
    pvc: str
    output_mount: str
    cluster_uid: str
    namespace_uid: str
    pvc_uid: str
    pvc_spec_sha256: str
    network_policy: str
    network_policy_uid: str
    network_policy_spec_sha256: str
    network_none_capability: Literal["network-policy-enforced-v1"]

    def __post_init__(self) -> None:
        if not isinstance(self.pvc_uid, str) or not _UID.fullmatch(self.pvc_uid):
            raise ValueError("pvc_uid must be a bounded Kubernetes UID.")
        if not isinstance(self.pvc_spec_sha256, str) or not _DIGEST.fullmatch(self.pvc_spec_sha256):
            raise ValueError("pvc_spec_sha256 must be a SHA-256 digest.")

    def cluster_config(self) -> KubernetesClusterConfig:
        return KubernetesClusterConfig(
            kubeconfig=str(self.kubeconfig),
            kubeconfig_sha256=_sha256_file(self.kubeconfig),
            context=self.context,
            profile=self.profile,
            server=self.server,
            server_sha256=hashlib.sha256(self.server.encode()).hexdigest(),
            ca_sha256=_sha256_file(self.ca_file),
            namespace=self.namespace,
            cluster_uid=self.cluster_uid,
            namespace_uid=self.namespace_uid,
        )


@dataclass(frozen=True)
class FixtureResult:
    state: Literal[
        "planned", "succeeded", "smoke_only", "skipped", "unavailable", "failed", "unresolved"
    ]
    message: str
    output: str | None = None


def fixture_exit_code(result: FixtureResult) -> int:
    """Reserve zero for a verified fixture run, never a skipped prerequisite."""
    return 0 if result.state == "succeeded" else 2 if result.state == "skipped" else 1


def hello_binding(
    *,
    operation_id: str,
    attempt_id: str,
    lease_id: str,
    epoch: int,
    image: str,
    output_pvc: str,
    output_mount: str,
    output_pvc_uid: str | None = None,
    output_pvc_spec_sha256: str | None = None,
) -> KubernetesJobBinding:
    """Return the fixed Job that writes precisely one persistent output file."""
    return KubernetesJobBinding(
        operation_id=operation_id,
        attempt_id=attempt_id,
        lease_id=lease_id,
        epoch=epoch,
        package_sha256=hashlib.sha256(HELLO_BYTES).hexdigest(),
        image=image,
        command=(
            "/bin/sh",
            "-ec",
            f'umask 077; test -z "$(find {output_mount} -mindepth 1 -maxdepth 1 -print -quit)"; '
            f"printf 'hello world\\n' > {output_mount}/{HELLO_PATH}; "
            f'test "$(find {output_mount} -mindepth 1 -maxdepth 1 -type f | wc -l)" = 1; '
            f"test \"$(cat {output_mount}/{HELLO_PATH})\" = 'hello world'; "
            f"cat {output_mount}/{HELLO_PATH}",
        ),
        active_deadline_seconds=60,
        network_mode="cluster",
        output_pvc=output_pvc,
        output_mount_path=output_mount,
        output_pvc_uid=output_pvc_uid,
        output_pvc_spec_sha256=output_pvc_spec_sha256,
    )


def fixture_manifest(
    target: PersistentOutputTarget, binding: KubernetesJobBinding
) -> dict[str, object]:
    """Build the existing digest-pinned Job shape with its caller-supplied PVC."""
    if (
        binding.output_pvc != target.pvc
        or binding.output_mount_path != target.output_mount
        or binding.output_pvc_uid != target.pvc_uid
        or binding.output_pvc_spec_sha256 != target.pvc_spec_sha256
    ):
        raise ValueError("binding persistent output must exactly match the explicit target.")
    return job_manifest(binding, target.cluster_config())


def run_fixture(
    target: PersistentOutputTarget,
    binding: KubernetesJobBinding,
    journal_path: Path,
    runner: CommandRunner = subprocess.run,
) -> FixtureResult:
    """Run the one explicit action, or honestly report unavailable prerequisites."""
    try:
        fixture_manifest(target, binding)
    except (OSError, ValueError):
        return FixtureResult("unavailable", "explicit target is invalid or unavailable.")
    if shutil.which("kubectl") is None or shutil.which("minikube") is None:
        return FixtureResult(
            "skipped", "kubectl or Minikube is unavailable; no cluster was changed."
        )
    try:
        _run(
            runner,
            ("minikube", "status", "-p", target.minikube_profile),
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError, ValueError):
        return FixtureResult("skipped", "Minikube profile is unavailable; no Job was created.")

    try:
        verified_binding = _verify_target(target, binding, runner)
    except _CommandFailure as error:
        if error.kind == "NotFound":
            return FixtureResult(
                "skipped", "required Kubernetes target resource was not found; no Job was created."
            )
        return FixtureResult("failed", f"Kubernetes {error.kind} failure; no Job was created.")
    except (OSError, subprocess.TimeoutExpired):
        return FixtureResult(
            "failed", "Kubernetes connectivity or timeout failure; no Job was created."
        )
    except (KubernetesContractError, ValueError, json.JSONDecodeError, TypeError):
        return FixtureResult(
            "failed", "Kubernetes target response was malformed; no Job was created."
        )

    journal = KubernetesJobJournal(journal_path)

    def inspect(name: str) -> Mapping[str, object] | None:
        try:
            return _json_output(_kubectl(target, runner, "get", "job", name, "-o", "json"))
        except _CommandFailure as error:
            if error.kind == "NotFound":
                return None
            raise

    def create(manifest_to_create: Mapping[str, object]) -> Mapping[str, object] | None:
        completed = _kubectl(
            target,
            runner,
            "create",
            "-f",
            "-",
            "-o",
            "json",
            input=json.dumps(manifest_to_create),
        )
        return _json_output(completed)

    create_attempted = False

    def fenced_create(manifest_to_create: Mapping[str, object]) -> Mapping[str, object] | None:
        nonlocal create_attempted
        create_attempted = True
        return create(manifest_to_create)

    try:
        journal.reconcile_create(verified_binding, target.cluster_config(), inspect, fenced_create)
        _kubectl(
            target,
            runner,
            "wait",
            "--for=condition=complete",
            f"job/{verified_binding.job_name}",
            "--timeout=75s",
            command_timeout=_WAIT_TIMEOUT_SECONDS,
        )
        observation = journal.reconcile_create(
            verified_binding, target.cluster_config(), inspect, fenced_create
        )
        pods = _owned_pods(target, runner, verified_binding, observation.job_uid)
        logs = _bounded_text(
            _kubectl(
                target,
                runner,
                "logs",
                f"job/{verified_binding.job_name}",
                "--tail=1",
                "--limit-bytes=1024",
            ).stdout
        )
    except KubernetesUnresolvedError:
        return FixtureResult("unresolved", "fixture Job create remains unresolved; inspect only.")
    except _CommandFailure as error:
        if create_attempted:
            return FixtureResult("unresolved", "fixture Job outcome is uncertain; inspect only.")
        return FixtureResult("failed", f"Kubernetes {error.kind} failure; no Job was created.")
    except (OSError, subprocess.TimeoutExpired):
        if create_attempted:
            return FixtureResult("unresolved", "fixture Job outcome is uncertain; inspect only.")
        return FixtureResult(
            "failed", "Kubernetes connectivity or timeout failure; no Job was created."
        )
    except (KubernetesContractError, ValueError, json.JSONDecodeError, TypeError):
        return FixtureResult("failed", "fixture Job response was malformed or conflicted.")
    output = f"PVC {target.pvc}:{target.output_mount}/{HELLO_PATH}"
    if (
        observation.state != "succeeded"
        or not pods
        or any(pod.state != "succeeded" for pod in pods)
        or logs != HELLO_BYTES.decode()
    ):
        return FixtureResult("failed", f"Job did not prove {output}; inspect Job logs manually.")
    try:
        retained = _authorized_readback(target, runner, verified_binding)
    except (
        KubernetesContractError,
        _CommandFailure,
        OSError,
        subprocess.TimeoutExpired,
        ValueError,
    ):
        return FixtureResult(
            "smoke_only", f"narrow log smoke only for {output}; retained bytes were not verified."
        )
    if retained != HELLO_BYTES:
        return FixtureResult(
            "smoke_only", f"narrow log smoke only for {output}; retained bytes did not match."
        )
    return FixtureResult(
        "succeeded", f"verified {output}; authorized post-exit readback matched exact bytes", output
    )


def _owned_pods(
    target: PersistentOutputTarget,
    runner: CommandRunner,
    binding: KubernetesJobBinding,
    job_uid: str,
) -> list[object]:
    """Validate every selected effective workload Pod, not its Job template alone."""
    listing = _json_output(
        _kubectl(
            target,
            runner,
            "get",
            "pods",
            "--selector",
            f"batch.kubernetes.io/controller-uid={job_uid}",
            "-o",
            "json",
        )
    )
    items = listing.get("items")
    if not isinstance(items, list) or not items:
        raise KubernetesContractError("Kubernetes Job has no observable owned Pods.")
    return [parse_pod(item, binding, target.cluster_config(), job_uid) for item in items]


def _authorized_readback(
    target: PersistentOutputTarget, runner: CommandRunner, binding: KubernetesJobBinding
) -> bytes:
    """Read retained bytes only through an explicitly authorized readback Pod."""
    for resource in ("pods", "pods/exec"):
        authorized = _bounded_text(
            _kubectl(target, runner, "auth", "can-i", "create", resource).stdout
        )
        if authorized != "yes\n":
            raise KubernetesContractError(
                "Kubernetes does not explicitly authorize fixture readback."
            )
    name = f"tessera-readback-{binding.operation_id}"
    _kubectl(
        target,
        runner,
        "create",
        "-f",
        "-",
        "-o",
        "json",
        input=json.dumps(_readback_manifest(target, binding, name)),
    )
    _kubectl(
        target,
        runner,
        "wait",
        "--for=condition=Ready",
        f"pod/{name}",
        "--timeout=30s",
        command_timeout=35,
    )
    return _bounded_text(
        _kubectl(
            target,
            runner,
            "exec",
            name,
            "--",
            "/bin/cat",
            f"{target.output_mount}/{HELLO_PATH}",
        ).stdout
    ).encode()


def _readback_manifest(
    target: PersistentOutputTarget, binding: KubernetesJobBinding, name: str
) -> dict[str, object]:
    """A bounded read-only Pod used only after the workload Job has exited."""
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "labels": {"app.kubernetes.io/managed-by": "tessera"},
            "name": name,
            "namespace": target.namespace,
        },
        "spec": {
            "activeDeadlineSeconds": 60,
            "automountServiceAccountToken": False,
            "containers": [
                {
                    "command": ["/bin/sh", "-ec", "sleep 45"],
                    "image": binding.image,
                    "imagePullPolicy": "Never",
                    "name": "readback",
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                        "privileged": False,
                        "readOnlyRootFilesystem": True,
                        "runAsNonRoot": True,
                        "runAsUser": 65532,
                    },
                    "volumeMounts": [
                        {
                            "mountPath": target.output_mount,
                            "name": "persistent-output",
                            "readOnly": True,
                        }
                    ],
                }
            ],
            "enableServiceLinks": False,
            "hostIPC": False,
            "hostNetwork": False,
            "hostPID": False,
            "restartPolicy": "Never",
            "securityContext": {
                "runAsGroup": 65532,
                "runAsNonRoot": True,
                "runAsUser": 65532,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "volumes": [
                {"name": "persistent-output", "persistentVolumeClaim": {"claimName": target.pvc}}
            ],
        },
    }


def _kubectl(
    target: PersistentOutputTarget,
    runner: CommandRunner,
    *args: str,
    command_timeout: int = _COMMAND_TIMEOUT_SECONDS,
    **kwargs: object,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = _run(
            runner,
            (
                "kubectl",
                "--kubeconfig",
                str(target.kubeconfig),
                "--context",
                target.context,
                "--server",
                target.server,
                "--certificate-authority",
                str(target.ca_file),
                "--namespace",
                target.namespace,
                "--request-timeout=10s",
                *args,
            ),
            timeout=command_timeout,
            **kwargs,
        )
    except subprocess.CalledProcessError as error:
        raise _CommandFailure(_classify_command_error(error)) from None
    return completed


class _CommandFailure(RuntimeError):
    def __init__(self, kind: str) -> None:
        self.kind = kind


def _run(
    runner: CommandRunner,
    argv: Sequence[str],
    *,
    timeout: int,
    **kwargs: object,
) -> subprocess.CompletedProcess[str]:
    """Invoke one bounded argv, never leaking command output in diagnostics."""
    completed = runner(
        tuple(argv), check=True, text=True, capture_output=True, timeout=timeout, **kwargs
    )
    if completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode, tuple(argv), completed.stdout, completed.stderr
        )
    _bounded_text(completed.stdout)
    _bounded_text(completed.stderr)
    return completed


def _bounded_text(value: object) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > _MAX_OUTPUT_BYTES:
        raise ValueError("Kubernetes command output is malformed or exceeds the fixture bound.")
    return value


def _json_output(completed: subprocess.CompletedProcess[str]) -> Mapping[str, object]:
    parsed = json.loads(_bounded_text(completed.stdout))
    if not isinstance(parsed, Mapping):
        raise ValueError("Kubernetes JSON response is not an object.")
    return parsed


def _verify_target(
    target: PersistentOutputTarget,
    binding: KubernetesJobBinding,
    runner: CommandRunner,
) -> KubernetesJobBinding:
    """Verify every caller-supplied identity before the create journal is opened."""
    cluster_anchor = _json_output(
        _kubectl(target, runner, "get", "namespace", "kube-system", "-o", "json")
    )
    namespace = _json_output(
        _kubectl(target, runner, "get", "namespace", target.namespace, "-o", "json")
    )
    pvc = _json_output(_kubectl(target, runner, "get", "pvc", target.pvc, "-o", "json"))
    policy = _json_output(
        _kubectl(target, runner, "get", "networkpolicy", target.network_policy, "-o", "json")
    )
    _verify_resource(cluster_anchor, "Namespace", "kube-system", target.cluster_uid)
    _verify_resource(namespace, "Namespace", target.namespace, target.namespace_uid)
    _verify_resource(pvc, "PersistentVolumeClaim", target.pvc, target.pvc_uid, target.namespace)
    _verify_resource(
        policy,
        "NetworkPolicy",
        target.network_policy,
        target.network_policy_uid,
        target.namespace,
        api_version="networking.k8s.io/v1",
    )
    spec = pvc.get("spec")
    if not isinstance(spec, Mapping):
        raise ValueError("PVC spec is malformed.")
    observed_spec_sha256 = hashlib.sha256(canonical_json(spec)).hexdigest()
    if observed_spec_sha256 != target.pvc_spec_sha256:
        raise KubernetesContractError("PVC spec conflicts with the explicit target.")
    if (
        binding.output_pvc_uid != target.pvc_uid
        or binding.output_pvc_spec_sha256 != observed_spec_sha256
    ):
        raise KubernetesContractError("Job PVC binding conflicts with the verified target.")
    if pvc.get("status", {}).get("phase") != "Bound":
        raise KubernetesContractError("PVC is not bound for persistent output.")
    policy_spec = policy.get("spec")
    if (
        not isinstance(policy_spec, Mapping)
        or hashlib.sha256(canonical_json(policy_spec)).hexdigest()
        != target.network_policy_spec_sha256
        or policy_spec.get("podSelector") != NETWORK_POLICY_SELECTOR
        or policy_spec.get("policyTypes") != ["Ingress", "Egress"]
        or "ingress" in policy_spec
        or "egress" in policy_spec
    ):
        raise KubernetesContractError(
            "network-none enforcing policy conflicts with the explicit target."
        )
    return replace(binding, output_pvc_spec_sha256=observed_spec_sha256)


def _verify_resource(
    resource: Mapping[str, object],
    kind: str,
    name: str,
    uid: str,
    namespace: str | None = None,
    api_version: str = "v1",
) -> None:
    metadata = resource.get("metadata")
    if (
        resource.get("apiVersion") != api_version
        or resource.get("kind") != kind
        or not isinstance(metadata, Mapping)
    ):
        raise ValueError("Kubernetes target response is malformed.")
    if metadata.get("name") != name or metadata.get("uid") != uid:
        raise KubernetesContractError(
            "Kubernetes target identity conflicts with the supplied target."
        )
    if namespace is not None and metadata.get("namespace") != namespace:
        raise KubernetesContractError(
            "Kubernetes target namespace conflicts with the supplied target."
        )


def _classify_command_error(error: subprocess.CalledProcessError) -> str:
    details = " ".join(
        value.lower()
        for value in (error.stdout, error.stderr)
        if isinstance(value, str) and len(value.encode("utf-8")) <= _MAX_OUTPUT_BYTES
    )
    if "notfound" in details or " not found" in details:
        return "NotFound"
    if "forbidden" in details or "unauthorized" in details:
        return "authentication or authorization"
    if any(
        marker in details
        for marker in ("unable to connect", "connection refused", "no route", "tls", "certificate")
    ):
        return "connectivity"
    return "command"


def _sha256_file(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()
