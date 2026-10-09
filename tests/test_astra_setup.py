"""Deterministic contracts for Astra's retained Docker Desktop setup boundary."""
# ruff: noqa: E501

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from tessera_control import cli
from tessera_provisioner.astra_setup import (
    AstraDockerDesktopSetup,
    AstraSetupError,
    CommandResult,
    SetupJournal,
    SetupPlan,
    SetupProfile,
    plan_from_json,
    plan_json,
)

IMAGE = "docker.io/library/tessera-e2e-hello@sha256:" + "a" * 64


class FakeKubectl:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.resources: dict[tuple[str, str, str | None], dict[str, object]] = {
            ("namespace", "kube-system", None): {
                "kind": "Namespace",
                "metadata": {"name": "kube-system", "uid": "cluster-uid"},
            }
        }

    def __call__(self, argv: tuple[str, ...], **kwargs: object) -> CommandResult:
        self.calls.append(argv)
        if "config" in argv:
            return self._result(
                argv,
                {
                    "contexts": [{"context": {"cluster": "docker"}}],
                    "clusters": [
                        {
                            "name": "docker",
                            "cluster": {
                                "server": "https://127.0.0.1:6443",
                                "certificate-authority-data": base64.b64encode(b"ca").decode(),
                            },
                        }
                    ],
                },
            )
        if "get" in argv:
            kind = argv[argv.index("get") + 1]
            name = argv[argv.index(kind) + 1]
            namespace = argv[argv.index("--namespace") + 1] if "--namespace" in argv else None
            value = self.resources.get((kind, name, namespace))
            if value is None:
                return CommandResult(argv, 1, "", "Error from server (NotFound): missing")
            return self._result(argv, value)
        if "create" in argv:
            manifest = json.loads(str(kwargs["input"]))
            kind = manifest["kind"]
            singular = {
                "Namespace": "namespace",
                "NetworkPolicy": "networkpolicy",
                "PersistentVolumeClaim": "pvc",
                "ResourceQuota": "resourcequota",
            }[kind]
            metadata = manifest["metadata"]
            namespace = metadata.get("namespace")
            value = {
                "kind": kind,
                "metadata": {
                    "name": metadata["name"],
                    "namespace": namespace,
                    "uid": f"{singular}-uid",
                    "labels": metadata.get("labels", {}),
                },
            }
            if "spec" in manifest:
                value["spec"] = manifest["spec"]
            self.resources[(singular, metadata["name"], namespace)] = value
            return self._result(argv, value)
        raise AssertionError(argv)

    @staticmethod
    def _result(argv: tuple[str, ...], value: object) -> CommandResult:
        return CommandResult(argv, 0, json.dumps(value), "")


def _operator(tmp_path: Path) -> tuple[AstraDockerDesktopSetup, SetupProfile, FakeKubectl]:
    kubeconfig = tmp_path / "config"
    kubeconfig.write_text("fixture")
    runner = FakeKubectl()
    return (
        AstraDockerDesktopSetup(kubeconfig, "docker-desktop", runner),
        SetupProfile.docker_desktop("astra-fixture", IMAGE, "provider-disabled-kubernetes-v1"),
        runner,
    )


def test_discovery_and_plan_are_deterministic_and_explicit(tmp_path: Path) -> None:
    operator, profile, runner = _operator(tmp_path)

    discovery = operator.discover(profile)
    first = SetupPlan.create(profile, discovery)
    second = SetupPlan.create(profile, discovery)

    assert first == second
    assert discovery.context == "docker-desktop"
    assert all("--kubeconfig" in call and "--context" in call for call in runner.calls)
    assert not any("current-context" in call for call in runner.calls)


def test_apply_records_created_and_replays_preexisting_resources(tmp_path: Path) -> None:
    operator, profile, runner = _operator(tmp_path)
    plan = SetupPlan.create(profile, operator.discover(profile))
    journal = SetupJournal(tmp_path / "setup.journal")

    first = operator.apply(plan, plan.digest, journal)
    replay = operator.status(plan, journal)

    assert first["state"] == "ready"
    assert [resource["created"] for resource in first["resources"]] == [True, True, True, True]
    assert replay["state"] == "ready"
    assert "not_proven" in str(replay["network_enforcement"])
    assert replay["storage_enforcement"] == "admission_enforced_claim_count_and_request_only"
    assert first["output_storage_contract"]["runtime_byte_bound"] == "unknown"
    assert first["output_storage_contract"]["runtime_inode_bound"] == "unknown"
    assert first["provider_billing_state"] == "operator_attested_unknown"
    assert first["cost_semantics"] == "internal_grants_are_not_provider_billing_caps"
    assert not any(
        any(word in call for word in ("apply", "delete", "helm", "ssh")) for call in runner.calls
    )
    assert len((tmp_path / "setup.journal").read_text().splitlines()) == 6


def test_output_pvc_is_dedicated_and_admission_bound_but_not_a_runtime_quota(
    tmp_path: Path,
) -> None:
    operator, profile, _runner = _operator(tmp_path)
    plan = SetupPlan.create(profile, operator.discover(profile))
    manifests = operator._manifests(profile)
    pvc = next(
        manifest
        for kind, _name, _namespace, manifest in manifests
        if kind == "PersistentVolumeClaim"
    )
    quota = next(
        manifest for kind, _name, _namespace, manifest in manifests if kind == "ResourceQuota"
    )

    assert pvc["metadata"]["labels"]["tessera.io/output-role"] == "dedicated-pilot-output"
    assert quota["spec"]["hard"] == {"persistentvolumeclaims": "1", "requests.storage": "64Mi"}
    assert plan.profile.pvc_storage == "64Mi"


def test_bound_pvc_server_volume_name_and_defaults_do_not_conflict(tmp_path: Path) -> None:
    operator, profile, runner = _operator(tmp_path)
    plan = SetupPlan.create(profile, operator.discover(profile))
    journal = SetupJournal(tmp_path / "setup.journal")
    operator.apply(plan, plan.digest, journal)
    pvc = runner.resources[("pvc", profile.pvc, profile.namespace)]
    pvc["spec"] = {
        **pvc["spec"],  # type: ignore[arg-type]
        "volumeName": "pvc-server-assigned",
    }
    pvc["status"] = {"phase": "Bound", "capacity": {"storage": "64Mi"}}

    status = operator.status(plan, journal)

    assert status["state"] == "ready"
    assert status["resources"][-1]["matches_plan"] is True


def test_pvc_approved_storage_conflict_requires_reconciliation(tmp_path: Path) -> None:
    operator, profile, runner = _operator(tmp_path)
    plan = SetupPlan.create(profile, operator.discover(profile))
    journal = SetupJournal(tmp_path / "setup.journal")
    operator.apply(plan, plan.digest, journal)
    pvc = runner.resources[("pvc", profile.pvc, profile.namespace)]
    pvc["spec"] = {**pvc["spec"], "storageClassName": "foreign"}  # type: ignore[arg-type]

    status = operator.status(plan, journal)

    assert status["state"] == "reconciliation_required"
    assert (
        next(item for item in status["resources"] if item["kind"] == "PersistentVolumeClaim")[
            "matches_plan"
        ]
        is False
    )


def test_status_distinguishes_same_named_replacement_from_custody(tmp_path: Path) -> None:
    operator, profile, runner = _operator(tmp_path)
    plan = SetupPlan.create(profile, operator.discover(profile))
    journal = SetupJournal(tmp_path / "setup.journal")
    operator.apply(plan, plan.digest, journal)
    namespace = runner.resources[("namespace", profile.namespace, None)]
    namespace["metadata"] = {"name": profile.namespace, "namespace": None, "uid": "replacement-uid"}

    status = operator.status(plan, journal)

    assert status["state"] == "reconciliation_required"
    assert status["resources"][0]["state"] == "replaced"
    assert status["resources"][0]["expected_uid"] == "namespace-uid"


def test_status_without_apply_time_custody_fails_closed(tmp_path: Path) -> None:
    operator, profile, _ = _operator(tmp_path)
    plan = SetupPlan.create(profile, operator.discover(profile))

    with pytest.raises(AstraSetupError, match="apply-time resource identities"):
        operator.status(plan)


def test_changed_target_or_foreign_resource_fails_before_create(tmp_path: Path) -> None:
    operator, profile, runner = _operator(tmp_path)
    plan = SetupPlan.create(profile, operator.discover(profile))
    runner.resources[("namespace", "kube-system", None)]["metadata"] = {
        "name": "kube-system",
        "uid": "replaced-cluster",
    }

    with pytest.raises(AstraSetupError, match="discovery changed"):
        operator.apply(plan, plan.digest, SetupJournal(tmp_path / "setup.journal"))

    assert not any("create" in call for call in runner.calls)


def test_plan_round_trip_rejects_tampering_and_invalid_image(tmp_path: Path) -> None:
    operator, profile, _ = _operator(tmp_path)
    plan = SetupPlan.create(profile, operator.discover(profile))
    path = tmp_path / "plan.json"
    path.write_text(plan_json(plan))

    assert plan_from_json(path) == plan
    value = json.loads(path.read_text())
    value["profile"]["namespace"] = "foreign"
    path.write_text(json.dumps(value))
    with pytest.raises(AstraSetupError, match="digest"):
        plan_from_json(path)
    with pytest.raises(AstraSetupError, match="exact repository"):
        SetupProfile.docker_desktop("astra-fixture", "ubuntu:latest", "runtime")


def test_journal_requires_private_parent(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    unsafe.chmod(0o755)  # A restrictive process umask can otherwise make this private.

    with pytest.raises(AstraSetupError, match="private directory"):
        SetupJournal(unsafe / "journal")


def test_journal_rejects_an_existing_nonprivate_file(tmp_path: Path) -> None:
    journal = tmp_path / "journal"
    journal.touch()
    journal.chmod(0o644)

    with pytest.raises(AstraSetupError, match="operator-owned regular file"):
        SetupJournal(journal)


def test_profiles_cli_is_read_only_and_requires_no_kubernetes(monkeypatch, capsys) -> None:
    arguments = cli.parser().parse_args(["astra-setup", "profiles"])

    assert cli._run_astra_setup(arguments) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["profiles"][0]["version"] == "astra-operator-setup-v3"
