"""Fake-runner coverage for the namespace-only operator boundary."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tessera_provisioner.astra_live_inspection import CommandResult, LiveInspectionError
from tessera_provisioner.astra_namespace import (
    NamespaceLifecycleError,
    NamespaceOperator,
    NamespacePlan,
)


def _config(path: Path) -> None:
    path.write_text(
        """clusters:
- name: c
  cluster:
    server: https://cluster.test
    certificate-authority-data: Y2E=
contexts:
- name: team/admin@prod
  context:
    cluster: c
"""
    )


def _namespace(name: str, uid: str, version: str, labels: dict[str, str] | None = None) -> str:
    return json.dumps(
        {
            "kind": "Namespace",
            "metadata": {
                "labels": labels or {},
                "name": name,
                "resourceVersion": version,
                "uid": uid,
            },
        }
    )


class FakeRunner:
    def __init__(self, namespace: str | None = None, *, create: str = "ok") -> None:
        self.namespace = namespace
        self.create_result = create
        self.calls: list[tuple[tuple[str, ...], bytes | None]] = []

    def __call__(
        self, argv: tuple[str, ...], _timeout: float, stdin: bytes | None
    ) -> CommandResult:
        self.calls.append((argv, stdin))
        if "get" in argv:
            name = argv[argv.index("namespace") + 1]
            if name == "kube-system":
                return CommandResult(argv, 0, _namespace(name, "cluster", "1"), "")
            if self.namespace is None:
                return CommandResult(argv, 1, "", "[kubectl stderr redacted]", True)
            return CommandResult(argv, 0, self.namespace, "")
        assert "create" in argv
        if self.create_result == "timeout":
            raise LiveInspectionError("namespace command response is uncertain")
        if self.create_result == "duplicate":
            return CommandResult(argv, 1, "", "[kubectl stderr redacted]")
        assert stdin is not None
        manifest = json.loads(stdin)
        self.namespace = _namespace(
            manifest["metadata"]["name"], "created", "9", manifest["metadata"]["labels"]
        )
        return CommandResult(argv, 0, self.namespace, "")


def _plan(tmp_path: Path, runner: FakeRunner) -> tuple[NamespaceOperator, NamespacePlan]:
    config = tmp_path / "config"
    _config(config)
    operator = NamespaceOperator(config, "team/admin@prod", runner=runner)
    plan = NamespacePlan.create(
        mode="create-dedicated",
        inspection=operator.inspect("astra-system"),
        installation="astra-control",
        operation="operation-one",
        approval_expires_at="2026-09-20T00:00:00Z",
    )
    return operator, plan


def test_existing_namespace_use_is_read_only_and_never_creates_any_resource(tmp_path: Path) -> None:
    runner = FakeRunner(_namespace("astra-system", "existing", "2"))
    config = tmp_path / "config"
    _config(config)
    operator = NamespaceOperator(config, "team/admin@prod", runner=runner)
    plan = NamespacePlan.create(
        mode="use-existing",
        inspection=operator.inspect("astra-system"),
        approval_expires_at="2026-09-20T00:00:00Z",
    )

    assert plan.mode == "use-existing"
    assert all("get" in call[0] for call in runner.calls)
    assert all("secret" not in call[0] for call in runner.calls)


def test_dedicated_create_has_one_namespace_receipt_and_no_installer_resources(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    operator, plan = _plan(tmp_path, runner)

    receipt = operator.create(plan, plan.digest, datetime(2026, 9, 19, tzinfo=UTC))

    assert receipt.uid == "created"  # type: ignore[union-attr]
    creates = [call for call in runner.calls if "create" in call[0]]
    assert len(creates) == 1
    manifest = json.loads(creates[0][1] or b"{}")
    assert manifest["kind"] == "Namespace"
    assert manifest["metadata"]["labels"] == plan.labels
    assert all(
        forbidden not in " ".join(call[0]).lower()
        for call in runner.calls
        for forbidden in ("secret", "pvc", "service", "deployment", "rbac", "policy")
    )
    assert (
        operator.status(plan, receipt)["state"] == "namespace_created_control_plane_not_installed"
    )  # type: ignore[arg-type]


@pytest.mark.parametrize("outcome", ["duplicate", "timeout"])
def test_lost_or_duplicate_create_response_requires_reconciliation_not_retry(
    tmp_path: Path, outcome: str
) -> None:
    runner = FakeRunner(create=outcome)
    operator, plan = _plan(tmp_path, runner)

    result = operator.create(plan, plan.digest, datetime(2026, 9, 19, tzinfo=UTC))

    assert result == {"plan_digest": plan.digest, "state": "reconciliation_required"}
    assert len([call for call in runner.calls if "create" in call[0]]) == 1


def test_stale_approval_and_changed_namespace_precondition_do_not_write(tmp_path: Path) -> None:
    runner = FakeRunner()
    operator, plan = _plan(tmp_path, runner)
    with pytest.raises(NamespaceLifecycleError, match="exact plan digest"):
        operator.create(plan, "a" * 64, datetime(2026, 9, 19, tzinfo=UTC))
    with pytest.raises(NamespaceLifecycleError, match="expired"):
        operator.create(plan, plan.digest, datetime(2026, 9, 21, tzinfo=UTC))
    runner.namespace = _namespace("astra-system", "other", "3")
    result = operator.create(plan, plan.digest, datetime(2026, 9, 19, tzinfo=UTC))
    assert result["state"] == "reconciliation_required"
    assert not [call for call in runner.calls if "create" in call[0]]


def test_status_uid_or_ownership_replacement_is_not_adopted(tmp_path: Path) -> None:
    runner = FakeRunner()
    operator, plan = _plan(tmp_path, runner)
    receipt = operator.create(plan, plan.digest, datetime(2026, 9, 19, tzinfo=UTC))
    assert not isinstance(receipt, dict)
    runner.namespace = _namespace("astra-system", "replacement", "10", plan.labels)

    assert operator.status(plan, receipt)["state"] == "reconciliation_required"
