"""Offline verification of the opt-in, create-only foundations runner."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location(
    "bootstrap_local_kubernetes_composition", SCRIPTS / "bootstrap_local_kubernetes_composition.py"
)
assert spec and spec.loader
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)
IMAGE = "docker.io/library/tessera-local-lab@sha256:" + "a" * 64


def test_documents_replace_both_control_images_and_never_pull():
    docs = bootstrap.documents(IMAGE)
    assert all(doc["kind"] != "Namespace" for doc in docs)
    pod = next(doc for doc in docs if doc["kind"] == "Deployment")["spec"]["template"]["spec"]
    for container in [*pod["initContainers"], *pod["containers"]]:
        assert container["image"] == IMAGE
        assert container["imagePullPolicy"] == "Never"
    assert {doc["metadata"]["namespace"] for doc in docs} == {bootstrap.CONTROL, bootstrap.WORKLOAD}


@pytest.mark.parametrize(
    "image",
    [
        "tessera:latest",
        "registry.example.invalid/x@sha256:" + "a" * 64,
        "docker.io/library/tessera-local-lab@sha256:" + "g" * 64,
    ],
)
def test_reject_mutable_or_wrong_image(image):
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.documents(image)


def test_occupied_namespace_has_no_effects():
    with (
        patch.object(
            bootstrap,
            "inspect_cluster",
            return_value={
                bootstrap.CONTROL: {"state": "occupied_refuse_adoption"},
                bootstrap.WORKLOAD: {"state": "absent_at_inspection_not_reserved"},
            },
        ),
        patch.object(bootstrap, "_kubectl") as command,
    ):
        with pytest.raises(bootstrap.BootstrapError, match="refuse adoption"):
            bootstrap.bootstrap(Path("/fixture/config"), IMAGE)
        command.assert_not_called()


def test_create_only_and_uid_readback_before_resources():
    calls = []

    def command(base, *args, data=None):
        calls.append((args, data))
        if args[0] == "create":
            doc = json.loads(data)
            return {"metadata": {"uid": "owned-" + doc["metadata"]["name"]}}
        if args[0] == "get":
            name = args[2]
            namespace_data = json.loads(calls[0 if name == bootstrap.CONTROL else 1][1])
            return {
                "metadata": {"uid": "owned-" + name, "labels": namespace_data["metadata"]["labels"]}
            }
        return {"metadata": {"uid": "resource"}}

    absent = {
        name: {"state": "absent_at_inspection_not_reserved"}
        for name in (bootstrap.CONTROL, bootstrap.WORKLOAD)
    }
    with (
        patch.object(bootstrap, "inspect_cluster", return_value=absent),
        patch.object(bootstrap, "verify_node_image"),
        patch.object(bootstrap, "_kubectl", side_effect=command),
    ):
        result = bootstrap.bootstrap(Path("/fixture/config"), IMAGE)
    assert result["state"] == "foundations_applied_not_ready"
    assert [args[0] for args, _ in calls[:4]] == ["create", "create", "get", "-n"]
    assert all("apply" not in args and "delete" not in args for args, _ in calls)


def test_changed_uid_refuses_to_install_resources():
    absent = {
        name: {"state": "absent_at_inspection_not_reserved"}
        for name in (bootstrap.CONTROL, bootstrap.WORKLOAD)
    }
    with (
        patch.object(bootstrap, "inspect_cluster", return_value=absent),
        patch.object(bootstrap, "verify_node_image"),
        patch.object(
            bootstrap,
            "_kubectl",
            side_effect=[
                {"metadata": {"uid": "first"}},
                {"metadata": {"uid": "second"}},
                {"metadata": {"uid": "stolen"}},
            ],
        ) as command,
    ):
        with pytest.raises(bootstrap.BootstrapError, match="identity changed"):
            bootstrap.bootstrap(Path("/fixture/config"), IMAGE)
        assert command.call_count == 3


def test_node_image_must_be_present_exactly():
    from subprocess import CompletedProcess

    with patch.object(
        bootstrap.subprocess, "run", return_value=CompletedProcess([], 0, IMAGE + "x\n")
    ):
        with pytest.raises(bootstrap.BootstrapError, match="missing"):
            bootstrap.verify_node_image(IMAGE)
