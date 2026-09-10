"""Explicit, opt-in Minikube persistent-output development fixture.

Importing this module and rendering its manifest never contacts Kubernetes.  The
only mutating path is :func:`run_fixture`, called by the script with ``--run``.
It is conformance evidence only, never provider execution or result acceptance.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .kubernetes_contracts import (
    KubernetesClusterConfig,
    KubernetesJobBinding,
    KubernetesJobJournal,
    KubernetesUnresolvedError,
    job_manifest,
)

PROFILE = "development-minikube-opencode-v1"
HELLO_PATH = "hello.txt"
HELLO_BYTES = b"hello world\n"
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


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
    state: Literal["planned", "succeeded", "skipped", "unavailable", "failed", "unresolved"]
    message: str
    output: str | None = None


def hello_binding(
    *,
    operation_id: str,
    attempt_id: str,
    lease_id: str,
    epoch: int,
    image: str,
    output_pvc: str,
    output_mount: str,
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
        output_pvc=output_pvc,
        output_mount_path=output_mount,
    )


def fixture_manifest(
    target: PersistentOutputTarget, binding: KubernetesJobBinding
) -> dict[str, object]:
    """Build the existing digest-pinned Job shape with its caller-supplied PVC."""
    if binding.output_pvc != target.pvc or binding.output_mount_path != target.output_mount:
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
    except (OSError, ValueError) as error:
        return FixtureResult("unavailable", f"explicit target unavailable: {error}")
    if shutil.which("kubectl") is None or shutil.which("minikube") is None:
        return FixtureResult(
            "skipped", "kubectl or Minikube is unavailable; no cluster was changed."
        )
    try:
        runner(
            "minikube",
            "status",
            "-p",
            target.minikube_profile,
            check=True,
            text=True,
            capture_output=True,
        )
        _kubectl(target, runner, "get", "pvc", target.pvc, "-o", "json")
    except (OSError, subprocess.CalledProcessError) as error:
        return FixtureResult(
            "skipped", f"Minikube or PVC prerequisite unavailable: {error}; no Job was created."
        )

    journal = KubernetesJobJournal(journal_path)

    def inspect(name: str) -> Mapping[str, object] | None:
        try:
            return json.loads(_kubectl(target, runner, "get", "job", name, "-o", "json").stdout)
        except subprocess.CalledProcessError as error:
            if error.returncode == 1:
                return None
            raise

    def create(manifest_to_create: Mapping[str, object]) -> Mapping[str, object] | None:
        completed = _kubectl(
            target, runner, "create", "-f", "-", "-o", "json", input=json.dumps(manifest_to_create)
        )
        return json.loads(completed.stdout)

    try:
        journal.reconcile_create(binding, target.cluster_config(), inspect, create)
        _kubectl(
            target,
            runner,
            "wait",
            "--for=condition=complete",
            f"job/{binding.job_name}",
            "--timeout=75s",
        )
        observation = journal.reconcile_create(binding, target.cluster_config(), inspect, create)
        logs = _kubectl(target, runner, "logs", f"job/{binding.job_name}").stdout
    except KubernetesUnresolvedError as error:
        return FixtureResult("unresolved", str(error))
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        return FixtureResult("failed", f"fixture Job was not confirmed: {error}")
    output = f"PVC {target.pvc}:{target.output_mount}/{HELLO_PATH}"
    if observation.state != "succeeded" or logs != HELLO_BYTES.decode():
        return FixtureResult("failed", f"Job did not prove {output}; inspect Job logs manually.")
    return FixtureResult("succeeded", f"verified {output}; logs contain hello world", output)


def _kubectl(
    target: PersistentOutputTarget,
    runner: CommandRunner,
    *args: str,
    **kwargs: object,
) -> subprocess.CompletedProcess[str]:
    return runner(
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
        *args,
        check=True,
        text=True,
        capture_output=True,
        **kwargs,
    )


def _sha256_file(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()
