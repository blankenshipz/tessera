from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from tessera_control import cli
from tessera_provisioner.minikube import (
    NAMESPACE,
    NETWORK_POLICY,
    PROFILE,
    PVC,
    CommandResult,
    MinikubePlan,
    MinikubeProvisioner,
    MinikubeProvisioningError,
    record_result,
)


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.profile_exists = False
        self.namespaces: dict[str, dict[str, object]] = {
            "kube-system": {
                "kind": "Namespace",
                "metadata": {"name": "kube-system", "uid": "cluster-1"},
            }
        }
        self.pvc: dict[str, object] | None = None
        self.policy: dict[str, object] | None = None
        self.failure: str | None = None
        self.wrong_profile = False
        self.replace_namespace_during_bootstrap = False

    def __call__(self, argv: tuple[str, ...], **kwargs: object) -> CommandResult:
        self.calls.append(argv)
        if self.failure is not None:
            return CommandResult(argv, 1, "", self.failure)
        if argv[:3] == ("minikube", "profile", "list"):
            valid: list[dict[str, object]] = []
            if self.profile_exists:
                valid.append(
                    {
                        "Name": PROFILE,
                        "Config": {
                            "VMDriver": "docker" if not self.wrong_profile else "qemu",
                            "CPUs": 2,
                            "Memory": 4096,
                            "DiskSize": 20480,
                        },
                    }
                )
            return self._json(argv, {"valid": valid})
        if argv[:2] == ("minikube", "start"):
            self.profile_exists = True
            return CommandResult(argv, 0, "", "")
        if argv[:2] == ("minikube", "status"):
            return self._json(argv, {"Host": "Running", "APIServer": "Running"})
        if "get" in argv:
            resource = argv[argv.index("get") + 1]
            name = argv[argv.index(resource) + 1]
            if resource == "namespace":
                value = self.namespaces.get(name)
            elif resource == "networkpolicy":
                value = self.policy if name == NETWORK_POLICY else None
            else:
                value = self.pvc if name == PVC else None
                if self.replace_namespace_during_bootstrap:
                    self.namespaces[NAMESPACE] = {
                        "kind": "Namespace",
                        "metadata": {"name": NAMESPACE, "uid": "namespace-replaced"},
                    }
            if value is None:
                return CommandResult(argv, 1, "", "Error from server (NotFound): missing")
            return self._json(argv, value)
        if "create" in argv:
            manifest = json.loads(str(kwargs["input"]))
            if manifest["kind"] == "Namespace":
                self.namespaces[NAMESPACE] = {
                    "kind": "Namespace",
                    "metadata": {"name": NAMESPACE, "uid": "namespace-1"},
                }
            else:
                if manifest["kind"] == "PersistentVolumeClaim":
                    self.pvc = {
                        "kind": "PersistentVolumeClaim",
                        "metadata": {"name": PVC, "namespace": NAMESPACE, "uid": "pvc-1"},
                        "spec": manifest["spec"],
                    }
                else:
                    self.policy = {
                        "apiVersion": "networking.k8s.io/v1",
                        "kind": "NetworkPolicy",
                        "metadata": {
                            "name": NETWORK_POLICY,
                            "namespace": NAMESPACE,
                            "uid": "policy-1",
                        },
                        "spec": manifest["spec"],
                    }
            return CommandResult(argv, 0, "", "")
        raise AssertionError(argv)

    @staticmethod
    def _json(argv: tuple[str, ...], value: object) -> CommandResult:
        return CommandResult(argv, 0, json.dumps(value), "")


def test_apply_sequences_profile_and_named_resources_then_replays_without_create() -> None:
    runner = FakeRunner()
    plan = MinikubePlan.default()
    provisioner = MinikubeProvisioner(runner)

    first = provisioner.apply(plan, plan.digest)
    create_indexes = [index for index, call in enumerate(runner.calls) if "create" in call]
    assert first["state"] == "reconciled"
    assert first["retained"] is True
    assert len(create_indexes) == 3
    assert runner.calls[0] == ("minikube", "profile", "list", "--output=json")
    kubectl_calls = [call for call in runner.calls if "kubectl" in call]
    assert all(call[:4] == ("minikube", "--profile", PROFILE, "kubectl") for call in kubectl_calls)

    before_replay = len(runner.calls)
    replay = provisioner.apply(plan, plan.digest)
    assert replay["profile_created"] is False
    assert not any("create" in call for call in runner.calls[before_replay:])


def test_apply_fails_closed_when_namespace_identity_is_replaced_during_bootstrap() -> None:
    runner = FakeRunner()
    runner.replace_namespace_during_bootstrap = True
    plan = MinikubePlan.default()

    with pytest.raises(MinikubeProvisioningError, match="namespace identity changed"):
        MinikubeProvisioner(runner).apply(plan, plan.digest)

    assert not any("networkpolicy" in call and "create" in call for call in runner.calls)


def test_wrong_existing_profile_fails_closed_before_start_or_kubernetes_calls() -> None:
    runner = FakeRunner()
    runner.profile_exists = True
    runner.wrong_profile = True

    with pytest.raises(MinikubeProvisioningError, match="does not match"):
        MinikubeProvisioner(runner).apply(MinikubePlan.default(), MinikubePlan.default().digest)

    assert runner.calls == [("minikube", "profile", "list", "--output=json")]


def test_changed_plan_or_approval_digest_never_calls_minikube() -> None:
    runner = FakeRunner()
    plan = MinikubePlan.default()
    changed = replace(plan, cpus=3)

    with pytest.raises(MinikubeProvisioningError, match="immutable"):
        MinikubeProvisioner(runner).apply(changed, changed.digest)
    with pytest.raises(MinikubeProvisioningError, match="Human approval"):
        MinikubeProvisioner(runner).apply(plan, "0" * 64)

    assert runner.calls == []


@pytest.mark.parametrize(
    "failure", ["Forbidden: private detail", "Unable to connect: private detail"]
)
def test_auth_or_connectivity_failure_is_sanitized_and_never_mutates(failure: str) -> None:
    runner = FakeRunner()
    runner.profile_exists = True
    runner.failure = failure

    with pytest.raises(MinikubeProvisioningError) as error:
        MinikubeProvisioner(runner).apply(MinikubePlan.default(), MinikubePlan.default().digest)

    assert "private detail" not in str(error.value)
    assert not any("start" in call or "create" in call for call in runner.calls)


def test_dry_run_never_constructs_or_calls_a_live_provisioner(monkeypatch, capsys) -> None:
    class MustNotConstruct:
        def __init__(self) -> None:
            pytest.fail("dry run must not invoke Minikube")

    monkeypatch.setattr("tessera_provisioner.minikube.MinikubeProvisioner", MustNotConstruct)
    arguments = cli.parser().parse_args(["minikube-provision"])

    assert cli._run_minikube_provisioning(arguments) == 0
    output = capsys.readouterr().out
    assert "dry run" in output
    assert "--apply" in output


def test_apply_requires_digest_and_journal_before_constructing_live_provisioner(
    monkeypatch, tmp_path, capsys
) -> None:
    class MustNotConstruct:
        def __init__(self) -> None:
            pytest.fail("invalid apply must not invoke Minikube")

    monkeypatch.setattr("tessera_provisioner.minikube.MinikubeProvisioner", MustNotConstruct)
    arguments = cli.parser().parse_args(
        ["minikube-provision", "--apply", "--journal", str(tmp_path / "op.json")]
    )

    assert cli._run_minikube_provisioning(arguments) == 2
    assert "requires" in capsys.readouterr().err


def test_status_and_teardown_are_explicit_and_teardown_never_invokes_minikube(
    monkeypatch, tmp_path, capsys
) -> None:
    class MustNotConstruct:
        def __init__(self) -> None:
            pytest.fail("teardown must retain resources without calling Minikube")

    journal = tmp_path / "result.json"
    monkeypatch.setattr("tessera_provisioner.minikube.MinikubeProvisioner", MustNotConstruct)
    arguments = cli.parser().parse_args(
        ["minikube-provision", "--teardown", "--journal", str(journal)]
    )

    assert cli._run_minikube_provisioning(arguments) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "retained"
    assert json.loads(journal.read_text())["state"] == "retained"


def test_sanitized_result_is_bounded_and_private(tmp_path: Path) -> None:
    parent = _private_directory(tmp_path / "nested")
    journal = parent / "result.json"
    record_result(journal, {"state": "reconciled", "plan_digest": MinikubePlan.default().digest})

    assert json.loads(journal.read_text())["state"] == "reconciled"
    assert journal.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("entry_type", ["symlink", "hardlink", "fifo"])
def test_preexisting_unsafe_temporary_is_never_opened_or_clobbered(
    monkeypatch, tmp_path: Path, entry_type: str
) -> None:
    parent = _private_directory(tmp_path / "private")
    journal = parent / "result.json"
    temporary = parent / ".result.json.collision.tmp"
    victim = tmp_path / "victim"
    victim.write_text("preserve me")
    if entry_type == "symlink":
        temporary.symlink_to(victim)
    elif entry_type == "hardlink":
        os.link(victim, temporary)
    else:
        os.mkfifo(temporary)
    monkeypatch.setattr("tessera_provisioner.minikube.secrets.token_hex", lambda _: "collision")

    with pytest.raises(MinikubeProvisioningError, match="could not be recorded"):
        record_result(journal, {"state": "reconciled"})

    assert victim.read_text() == "preserve me"
    assert not journal.exists()


@pytest.mark.parametrize("parent_type", ["nonprivate", "symlink"])
def test_journal_parent_must_be_existing_private_directory(
    tmp_path: Path, parent_type: str
) -> None:
    private = _private_directory(tmp_path / "private")
    if parent_type == "nonprivate":
        parent = tmp_path / "nonprivate"
        parent.mkdir(mode=0o700)
        parent.chmod(0o755)
    else:
        parent = tmp_path / "parent-link"
        parent.symlink_to(private, target_is_directory=True)

    with pytest.raises(MinikubeProvisioningError, match="parent is not private"):
        record_result(parent / "result.json", {"state": "reconciled"})


def test_concurrent_result_writers_publish_one_unchanged_record(tmp_path: Path) -> None:
    journal = _private_directory(tmp_path / "private") / "result.json"
    start = threading.Barrier(4)
    successes: list[int] = []
    failures: list[Exception] = []

    def writer(index: int) -> None:
        start.wait()
        try:
            record_result(journal, {"state": "reconciled", "writer": index})
        except MinikubeProvisioningError as error:
            failures.append(error)
        else:
            successes.append(index)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(successes) == 1
    assert len(failures) == 3
    assert json.loads(journal.read_text())["writer"] == successes[0]


def test_apply_rejects_unsafe_journal_before_constructing_live_provisioner(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    class MustNotConstruct:
        def __init__(self) -> None:
            pytest.fail("unsafe journal must block Minikube before construction")

    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o700)
    unsafe.chmod(0o755)
    plan = MinikubePlan.default()
    monkeypatch.setattr("tessera_provisioner.minikube.MinikubeProvisioner", MustNotConstruct)
    arguments = cli.parser().parse_args(
        [
            "minikube-provision",
            "--apply",
            "--approved-plan",
            plan.digest,
            "--journal",
            str(unsafe / "result.json"),
        ]
    )

    assert cli._run_minikube_provisioning(arguments) == 1
    assert "parent is not private" in capsys.readouterr().err


def test_apply_records_failure_after_infrastructure_mutation(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    mutations: list[str] = []

    class FailsAfterMutation:
        def apply(self, plan: MinikubePlan, approved_plan_digest: str) -> dict[str, object]:
            assert approved_plan_digest == plan.digest
            mutations.append("profile-created")
            raise MinikubeProvisioningError(
                "Local command failed: subprocess_failure; effects unknown."
            )

    journal = _private_directory(tmp_path / "private") / "result.json"
    plan = MinikubePlan.default()
    monkeypatch.setattr("tessera_provisioner.minikube.MinikubeProvisioner", FailsAfterMutation)
    arguments = cli.parser().parse_args(
        [
            "minikube-provision",
            "--apply",
            "--approved-plan",
            plan.digest,
            "--journal",
            str(journal),
        ]
    )

    assert cli._run_minikube_provisioning(arguments) == 1
    assert mutations == ["profile-created"]
    assert json.loads(journal.read_text())["state"] == "failed"
    assert "subprocess_failure" in capsys.readouterr().err


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path
