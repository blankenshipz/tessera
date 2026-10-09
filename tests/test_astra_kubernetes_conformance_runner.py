"""Deterministic contracts for the opt-in Astra conformance runner."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def runner_module():
    path = Path(__file__).parents[1] / "scripts" / "check_astra_kubernetes_conformance.py"
    spec = importlib.util.spec_from_file_location("astra_kubernetes_conformance_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _arguments(tmp_path: Path) -> argparse.Namespace:
    kubeconfig = tmp_path / "config"
    kubeconfig.write_text("fixture")
    return argparse.Namespace(
        run=True,
        kubeconfig=str(kubeconfig),
        context="docker-desktop",
        probe_namespace="astra-quota-fixture",
        probe_pvc="astra-quota-pvc",
        storage_class="quota-fixture",
        pvc_storage="8Mi",
        byte_quota_bytes=1024 * 1024,
        inode_quota_files=64,
        evidence=str(tmp_path / "evidence.json"),
    )


def test_live_arguments_require_explicit_existing_absolute_target(
    tmp_path: Path, runner_module
) -> None:
    args = _arguments(tmp_path)

    kubeconfig, evidence = runner_module.validate_live_args(args)

    assert kubeconfig.is_absolute()
    assert evidence == tmp_path / "evidence.json"
    args.context = "other"
    with pytest.raises(runner_module.ConformanceError, match="docker-desktop"):
        runner_module.validate_live_args(args)


def test_live_arguments_require_explicit_new_bounds(tmp_path: Path, runner_module) -> None:
    args = _arguments(tmp_path)
    args.byte_quota_bytes = 9 * 1024 * 1024

    with pytest.raises(runner_module.ConformanceError, match="cannot exceed"):
        runner_module.validate_live_args(args)

    args = _arguments(tmp_path)
    args.inode_quota_files = 0
    with pytest.raises(runner_module.ConformanceError, match="inode-quota-files"):
        runner_module.validate_live_args(args)


def test_live_evidence_requires_a_private_non_symlink_operator_directory(
    tmp_path: Path, runner_module
) -> None:
    args = _arguments(tmp_path)
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)  # mkdir's mode is filtered by the process umask.
    args.evidence = str(shared / "evidence.json")

    with pytest.raises(runner_module.ConformanceError, match="private non-symlink"):
        runner_module.validate_live_args(args)

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(private, target_is_directory=True)
    args.evidence = str(linked / "evidence.json")
    with pytest.raises(runner_module.ConformanceError, match="private non-symlink"):
        runner_module.validate_live_args(args)


def test_evidence_write_is_complete_private_and_refuses_an_existing_path(
    tmp_path: Path, runner_module
) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    evidence = directory / "evidence.json"

    runner_module._write_evidence(evidence, {"fixture_verdict": "blocked"})

    assert evidence.stat().st_mode & 0o777 == 0o600
    assert evidence.read_text() == '{\n  "fixture_verdict": "blocked"\n}\n'
    with pytest.raises(runner_module.ConformanceError, match="could not be created safely"):
        runner_module._write_evidence(evidence, {"fixture_verdict": "blocked"})


def _storage_class(annotations: dict[str, str]) -> dict[str, object]:
    return {"kind": "StorageClass", "metadata": {"annotations": annotations}}


def test_quota_capability_requires_matching_explicit_storageclass_declaration(
    runner_module,
) -> None:
    annotations = {
        runner_module.QUOTA_CAPABILITY_ANNOTATION: runner_module.QUOTA_CAPABILITY,
        runner_module.BYTE_QUOTA_ANNOTATION: "1048576",
        runner_module.INODE_QUOTA_ANNOTATION: "64",
    }

    capability = runner_module.quota_capability(_storage_class(annotations), 1048576, 64)

    assert capability["capability"] == "filesystem-quota-v1"
    with pytest.raises(runner_module.UnsupportedQuota, match="does not match"):
        runner_module.quota_capability(_storage_class(annotations), 1048577, 64)
    with pytest.raises(runner_module.UnsupportedQuota, match="no explicit"):
        runner_module.quota_capability(_storage_class({}), 1048576, 64)


def test_quota_evidence_classifies_enforced_unsupported_and_unexpected_outcomes(
    runner_module,
) -> None:
    enforced = runner_module.classify_quota_evidence(
        "byte",
        {
            "kind": "byte",
            "limit": 1024,
            "requested_bytes": 2048,
            "written_bytes": 1024,
            "outcome": "enforced",
            "failure": "no space left on device",
            "failure_errno": 28,
        },
        1024,
    )
    unsupported = runner_module.classify_quota_evidence(
        "inode",
        {
            "kind": "inode",
            "limit": 3,
            "requested_files": 4,
            "created_files": 4,
            "outcome": "not_enforced",
        },
        3,
    )

    assert enforced["classification"] == "enforced"
    assert unsupported["classification"] == "unsupported"
    with pytest.raises(runner_module.ConformanceError, match="non-quota"):
        runner_module.classify_quota_evidence(
            "inode",
            {
                "kind": "inode",
                "limit": 3,
                "requested_files": 4,
                "created_files": 3,
                "outcome": "enforced",
                "failure": "permission denied",
                "failure_errno": 13,
            },
            3,
        )


def test_quota_evidence_rejects_unbounded_or_uncleaned_probe_data(runner_module) -> None:
    with pytest.raises(runner_module.ConformanceError, match="past the declared"):
        runner_module.classify_quota_evidence(
            "byte",
            {
                "kind": "byte",
                "limit": 1,
                "requested_bytes": 1,
                "written_bytes": 1,
                "outcome": "not_enforced",
            },
            1,
        )
    with pytest.raises(runner_module.ConformanceError, match="clean"):
        runner_module.classify_quota_evidence(
            "byte",
            {
                "kind": "byte",
                "limit": 1,
                "requested_bytes": 2,
                "written_bytes": 1,
                "outcome": "enforced",
                "failure": "quota",
                "failure_errno": 122,
                "cleanup_error": "busy",
            },
            1,
        )


def test_network_policies_only_allow_the_labelled_allowed_client(runner_module) -> None:
    _deny_all, ingress, egress = runner_module.network_manifests(
        "astra-fixture", "run", "tessera-astra-run"
    )

    role = "tessera.io/conformance-role"
    assert ingress["spec"]["podSelector"]["matchLabels"][role] == "network-server"
    source_role = ingress["spec"]["ingress"][0]["from"][0]["podSelector"]["matchLabels"][role]
    assert source_role == "network-allowed"
    assert egress["spec"]["podSelector"]["matchLabels"][role] == "network-allowed"
    destination_role = egress["spec"]["egress"][0]["to"][0]["podSelector"]["matchLabels"][role]
    assert destination_role == "network-server"


def test_network_enforcement_requires_observed_allowed_and_denied_connectivity(
    runner_module,
) -> None:
    declaration = [{"name": "deny", "uid": "policy-1"}] * 3
    server = {"uid": "server"}
    allowed = {"uid": "allowed", "expected": "allow", "log": "allowed connection observed"}
    denied = {
        "uid": "denied",
        "expected": "deny",
        "log": "denied connection observed: i/o timeout",
    }

    evidence = runner_module.classify_network_enforcement(
        declaration=declaration, server=server, allowed=allowed, denied=denied
    )

    assert evidence["declaration"]["classification"] == "declared"
    assert evidence["enforcement"]["classification"] == "enforced"
    with pytest.raises(runner_module.ConformanceError, match="denied connectivity"):
        runner_module.classify_network_enforcement(
            declaration=declaration,
            server=server,
            allowed=allowed,
            denied={**denied, "log": "unexpected connection to denied destination: HTTP 200"},
        )
    with pytest.raises(runner_module.ConformanceError, match="declaration identities"):
        runner_module.classify_network_enforcement(
            declaration=[], server=server, allowed=allowed, denied=denied
        )


def test_pvc_probe_pod_uses_only_the_new_supplied_claim(
    runner_module,
) -> None:
    manifest = runner_module.pod_manifest(
        name="fixture",
        namespace="astra-fixture",
        run_id="run",
        role="storage-writer",
        image="example@sha256:" + "a" * 64,
        command=["--write-sentinel", "/outputs/.tessera-sentinel", "value"],
        pvc="astra-quota-pvc",
    )

    assert manifest["spec"]["volumes"] == [
        {"name": "outputs", "persistentVolumeClaim": {"claimName": "astra-quota-pvc"}}
    ]
    assert manifest["spec"]["containers"][0]["imagePullPolicy"] == "Never"
    assert runner_module._owned(manifest, "run")
    assert not runner_module._owned(manifest, "other")


def test_new_pvc_manifest_has_explicit_storage_class_and_request(runner_module) -> None:
    manifest = runner_module.pvc_manifest(
        namespace="astra-quota-fixture",
        name="astra-quota-pvc",
        run_id="run",
        storage_class="quota-fixture",
        storage="8Mi",
    )

    assert manifest["spec"]["storageClassName"] == "quota-fixture"
    assert manifest["spec"]["resources"]["requests"]["storage"] == "8Mi"
    assert manifest["metadata"]["name"] != "astra-outputs"


def test_cleanup_refuses_a_same_label_replacement_uid(runner_module) -> None:
    kubectl = object.__new__(runner_module.Kubectl)
    kubectl.namespace = "astra-fixture"
    kubectl.get = lambda *_args, **_kwargs: {
        "kind": "Pod",
        "metadata": {
            "name": "fixture",
            "namespace": "astra-fixture",
            "uid": "replacement",
            "creationTimestamp": "2026-09-14T00:00:00Z",
            "labels": {
                "app.kubernetes.io/managed-by": "tessera-astra-conformance",
                "tessera.io/conformance-run": "run",
            },
        },
    }
    kubectl.run = lambda *_args, **_kwargs: pytest.fail("replacement must not be deleted")

    with pytest.raises(runner_module.ConformanceError, match="replaced"):
        kubectl.delete_owned("pod", "fixture", "run", "created")


def test_cleanup_verification_rejects_remaining_owned_resources(runner_module) -> None:
    kubectl = object.__new__(runner_module.Kubectl)
    kubectl.list_owned = lambda kind, _run_id: (
        [
            {
                "metadata": {
                    "name": "remaining-pod",
                }
            }
        ]
        if kind == "pod"
        else []
    )

    with pytest.raises(runner_module.ConformanceError, match="remain after cleanup"):
        runner_module._assert_no_owned_resources(kubectl, "run")
