"""Focused fake-runner tests for the bounded live Astra inspection adapter."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tessera_provisioner.astra_installation import (
    AstraInstallationError,
    InstallationIdentity,
    InstallationInspection,
    InstallationPlan,
    ObservedInstallation,
    ResourcePrecondition,
    TargetIdentity,
)
from tessera_provisioner.astra_live_inspection import (
    MAX_KUBECTL_OUTPUT_BYTES,
    CommandResult,
    LiveAstraInspector,
    LiveInspectionError,
    _config_identity,
    revalidate_plan,
)


def _config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("""clusters:
- name: c
  cluster:
    server: https://cluster.test
    certificate-authority-data: Y2E=
contexts:
- name: ctx
  context:
    cluster: c
""")


def _object(kind: str, name: str, uid: str, spec: dict | None = None, **metadata: object) -> str:
    return json.dumps(
        {
            "kind": kind,
            "metadata": {
                "name": name,
                "uid": uid,
                "resourceVersion": "7",
                **({} if kind == "Namespace" else {"namespace": "astra"}),
                **metadata,
            },
            "spec": spec or {},
        }
    )


def _runner(calls: list[tuple[str, ...]], foreign: bool = False):
    def run(argv: tuple[str, ...], timeout: float) -> CommandResult:
        calls.append(argv)
        resource, name = argv[argv.index("get") + 1 : argv.index("get") + 3]
        values = {
            ("namespace", "kube-system"): _object("Namespace", name, "cluster"),
            ("namespace", "astra"): _object("Namespace", name, "ns"),
            ("deployment", "astra-control"): _object(
                "Deployment",
                name,
                "work",
                {"replicas": 1},
                labels={"app.kubernetes.io/managed-by": "foreign-owner" if foreign else "tessera"},
            ),
        }
        return CommandResult(argv, 0, values[(resource, name)], "token=leak")

    return run


def _inspect(tmp_path: Path, foreign: bool = False):
    kubeconfig = tmp_path / "config"
    _config(kubeconfig)
    calls: list[tuple[str, ...]] = []
    evidence = LiveAstraInspector(kubeconfig, "ctx", runner=_runner(calls, foreign)).inspect(
        InstallationIdentity("astra-control", "astra"), "astra", "deployment/astra-control", ()
    )
    return evidence, calls, kubeconfig


def test_only_allowlisted_read_commands_and_secret_values_are_redacted(tmp_path: Path) -> None:
    evidence, calls, _ = _inspect(tmp_path)
    assert all(
        command[command.index("get") + 1] in {"namespace", "deployment"} for command in calls
    )
    assert all("--context" in command and "--output=json" in command for command in calls)
    assert "token=leak" not in json.dumps(evidence)
    assert "secret" not in json.dumps(evidence).lower()


def test_forbidden_secret_resource_is_rejected(tmp_path: Path) -> None:
    kubeconfig = tmp_path / "config"
    _config(kubeconfig)
    with pytest.raises(LiveInspectionError, match="explicit allowed"):
        LiveAstraInspector(kubeconfig, "ctx").inspect(
            InstallationIdentity("astra-control", "astra"), "astra", "secret/nope", ()
        )


def _observed_plan(kubeconfig: Path, evidence: dict[str, object]) -> InstallationPlan:
    target = TargetIdentity(
        hashlib.sha256(kubeconfig.read_bytes()).hexdigest(),
        "ctx",
        "https://cluster.test",
        hashlib.sha256(b"ca").hexdigest(),
        "cluster",
        "astra",
        "ns",
    )
    observed = evidence["inspection"]["observation"]  # type: ignore[index]
    inspection = InstallationInspection(
        InstallationIdentity("astra-control", "astra"),
        target,
        ObservedInstallation(
            InstallationIdentity("astra-control", "astra"),
            target,
            observed["uid"],  # type: ignore[index]
            observed["spec_sha256"],  # type: ignore[index]
            observed["observed_at"],  # type: ignore[index]
            observed["resource_version"],  # type: ignore[index]
        ),
        resources=tuple(
            ResourcePrecondition(**value)  # type: ignore[arg-type]
            for value in evidence["inspection"]["resources"]  # type: ignore[index]
        ),
    )
    return InstallationPlan.create(
        mode="adopt",
        inspection=inspection,
        desired_spec_sha256=observed["spec_sha256"],  # type: ignore[index]
        approval_expires_at="2026-09-20T00:00:00Z",
    )


def test_target_mismatch_uid_spec_drift_and_expiry_block_revalidation(tmp_path: Path) -> None:
    evidence, _, kubeconfig = _inspect(tmp_path)
    adopted = _observed_plan(kubeconfig, evidence)
    revalidate_plan(adopted, evidence, datetime(2026, 9, 19, tzinfo=UTC))
    with pytest.raises(AstraInstallationError, match="expired"):
        revalidate_plan(adopted, evidence, datetime(2026, 9, 21, tzinfo=UTC))
    changed = json.loads(json.dumps(evidence))
    changed["inspection"]["observation"]["uid"] = "other"  # type: ignore[index]
    with pytest.raises(LiveInspectionError, match="UID, resourceVersion, or spec"):
        revalidate_plan(adopted, changed, datetime(2026, 9, 19, tzinfo=UTC))
    changed = json.loads(json.dumps(evidence))
    changed["inspection"]["observation"]["spec_sha256"] = "b" * 64  # type: ignore[index]
    with pytest.raises(LiveInspectionError, match="UID, resourceVersion, or spec"):
        revalidate_plan(adopted, changed, datetime(2026, 9, 19, tzinfo=UTC))
    changed = json.loads(json.dumps(evidence))
    changed["inspection"]["observation"]["resource_version"] = "8"  # type: ignore[index]
    with pytest.raises(LiveInspectionError, match="UID, resourceVersion, or spec"):
        revalidate_plan(adopted, changed, datetime(2026, 9, 19, tzinfo=UTC))
    changed = json.loads(json.dumps(evidence))
    changed["inspection"]["target"]["namespace_uid"] = "other"  # type: ignore[index]
    changed["inspection"]["observation"]["target"]["namespace_uid"] = "other"  # type: ignore[index]
    with pytest.raises(AstraInstallationError, match="target identity"):
        revalidate_plan(adopted, changed, datetime(2026, 9, 19, tzinfo=UTC))


def test_foreign_ownership_blocks_mutating_revalidation(tmp_path: Path) -> None:
    evidence, _, kubeconfig = _inspect(tmp_path, foreign=True)
    with pytest.raises(LiveInspectionError, match="ownership"):
        revalidate_plan(
            _observed_plan(kubeconfig, evidence), evidence, datetime(2026, 9, 19, tzinfo=UTC)
        )


def test_not_found_is_classified_before_stderr_redaction_and_response_is_identity_fenced(
    tmp_path: Path,
) -> None:
    kubeconfig = tmp_path / "config"
    _config(kubeconfig)
    calls: list[tuple[str, ...]] = []

    def missing(argv: tuple[str, ...], _timeout: float) -> CommandResult:
        calls.append(argv)
        return CommandResult(argv, 1, "", "[kubectl stderr redacted]", not_found=True)

    assert (
        LiveAstraInspector(kubeconfig, "team/admin@prod", runner=missing)._get(
            "deployment", "x", "astra"
        )
        is None
    )
    assert calls and "secret" not in " ".join(calls[0]).lower()

    def wrong_kind(argv: tuple[str, ...], _timeout: float) -> CommandResult:
        return CommandResult(argv, 0, _object("Service", "x", "uid"), "")

    with pytest.raises(LiveInspectionError, match="unexpected"):
        LiveAstraInspector(kubeconfig, "ctx", runner=wrong_kind)._get("deployment", "x", "astra")


def test_ca_bytes_file_insecure_tls_output_and_inventory_drift_fail_closed(tmp_path: Path) -> None:
    certificate = tmp_path / "cluster-ca.pem"
    certificate.write_bytes(b"same-ca-bytes")
    kubeconfig = tmp_path / "config"
    kubeconfig.write_text(
        f"""clusters:
- name: c
  cluster:
    server: https://cluster.test
    certificate-authority: {certificate.name}
contexts:
- name: team/admin@prod
  context:
    cluster: c
"""
    )
    assert (
        _config_identity(kubeconfig, "team/admin@prod")[2]
        == hashlib.sha256(b"same-ca-bytes").hexdigest()
    )
    kubeconfig.write_text(
        kubeconfig.read_text().replace(
            "certificate-authority:", "insecure-skip-tls-verify: true\n    certificate-authority:"
        )
    )
    with pytest.raises(LiveInspectionError, match="skip TLS"):
        _config_identity(kubeconfig, "team/admin@prod")

    evidence, _, config = _inspect(tmp_path / "live")
    plan = _observed_plan(config, evidence)
    changed = json.loads(json.dumps(evidence))
    changed["inspection"]["resources"] = []  # type: ignore[index]
    with pytest.raises(LiveInspectionError, match="inventory"):
        revalidate_plan(plan, changed, datetime(2026, 9, 19, tzinfo=UTC))

    def huge(argv: tuple[str, ...], _timeout: float) -> CommandResult:
        return CommandResult(argv, 0, "x" * (MAX_KUBECTL_OUTPUT_BYTES + 1), "")

    with pytest.raises(LiveInspectionError, match="byte bound"):
        LiveAstraInspector(config, "ctx", runner=huge)._get("deployment", "x", "astra")
