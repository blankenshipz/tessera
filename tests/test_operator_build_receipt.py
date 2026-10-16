"""Synthetic Docker/node runner; local Git/SQLite-independent provenance tests.

No live image builds/imports/containers/namespace changes.
"""

import hashlib
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tessera_operator import build_receipt as producer
from tessera_operator import upgrade
from tessera_operator import upgrade_support as activation
from tessera_operator import upgrade_support as private_import
from tessera_operator import upgrade_support as target

OP = "a" * 32


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(upgrade, "ROOT", root / "upgrade")
    repository = tmp_path / "repo"
    repository.mkdir()
    monkeypatch.setattr(target, "REPOSITORY", repository)
    for name, data in {
        "README.md": "published only",
        "src/tessera_control/store.py": "fenced store",
        "deploy/kubernetes/local-persistent.Dockerfile": "FROM reviewed-base",
        "deploy/kubernetes/local-persistent.Dockerfile.dockerignore": "**\n!src/**\n",
    }.items():
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)

    def git(*args):
        return (
            subprocess.run(["git", "-C", str(repository), *args], check=True, capture_output=True)
            .stdout.decode()
            .strip()
        )

    git("init", "-q")
    git("add", ".")
    git(
        "-c",
        "user.name=Isolated Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    commit = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", commit)
    # Necessary dirty overlay only; unrelated dirty/untracked inputs must not enter context.
    (repository / "src/tessera_control/store.py").write_text("exact existing fence")
    (repository / "README.md").write_text("unrelated dirty README")
    (repository / "private.secret").write_text("synthetic-not-a-credential")
    inventory = upgrade.candidate_inventory()
    runtime = {
        "schema": 56,
        "store_sha256": inventory["overlays"]["src/tessera_control/store.py"],
        "node_id": "node-1",
        "incarnation": "backend-1",
    }
    state = {"commands": [], "fail": None, "crash": None, "drift": None}
    monkeypatch.setattr(producer, "_runtime", lambda: dict(runtime))
    monkeypatch.setattr(producer, "_capacity", lambda: None)
    config = "sha256:" + "c" * 64
    manifest = json.dumps({"config": {"digest": config}}, separators=(",", ":")).encode()
    digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
    pinned = "docker.io/library/tessera-local-persistent@" + digest

    def docker(directory, label, *args, **kwargs):
        assert not (upgrade.ROOT / "candidates").exists()  # No early success publication.
        state["commands"].append(label)
        activation._write_once(
            directory / (label + ".json"), {"synthetic": True, "argv": list(args)}
        )
        if label == state["crash"]:
            raise KeyboardInterrupt("synthetic process loss")
        if label == state["fail"]:
            raise target.AdmissionError("synthetic build command failure")
        if label == state["drift"]:
            runtime["node_id"] = "node-foreign"
        out = directory / (label + ".stdout")
        data = b""
        if label == "inspect":
            data = json.dumps([{"Id": config, "Os": "linux", "Architecture": "amd64"}]).encode()
        if label == "contents":
            files = producer._file_inventory(directory / "context")
            packaged = {
                n: d
                for n, d in files.items()
                if n.endswith(".py")
                and n.startswith(("src/tessera_control/", "src/tessera_operator/"))
            }
            data = json.dumps(
                {
                    "schema": 56,
                    "store_sha256": runtime["store_sha256"],
                    "package_sha256": upgrade._digest(packaged),
                }
            ).encode()
        if label == "node-list":
            data = (
                "docker.io/library/tessera-local-persistent:upgrade-" + OP + " type " + digest
            ).encode()
        if label in {"manifest", "pinned-manifest"}:
            data = manifest
        if label == "pins":
            data = (pinned + " type " + digest).encode()
        out.write_bytes(data)
        if label == "export-tagged":
            (directory / "tagged.tar").write_bytes(b"synthetic-image")
        return out

    def import_image(directory, archive):
        assert archive.is_file()
        state["commands"].append("import")
        activation._write_once(directory / "import.json", {"synthetic": True})
        if state["fail"] == "import":
            raise target.AdmissionError("synthetic partial import")

    monkeypatch.setattr(producer, "_docker", docker)
    monkeypatch.setattr(producer, "_import", import_image)
    return SimpleNamespace(
        root=root,
        repo=repository,
        inventory=inventory,
        runtime=runtime,
        state=state,
        pinned=pinned,
        digest=digest,
    )


def run_build():
    return producer.build(OP, producer.plan()["build_plan_sha256"])


def test_freeze_build_inspect_import_then_publish(candidate):
    result = run_build()
    assert result["image"] == candidate.pinned
    directory = upgrade.ROOT / "builds" / OP
    assert (directory / "context/README.md").read_text() == "published only"
    assert not (directory / "context/private.secret").exists()
    assert (
        directory / "context/src/tessera_control/store.py"
    ).read_text() == "exact existing fence"
    assert candidate.state["commands"] == [
        "build",
        "inspect",
        "contents",
        "export-tagged",
        "import",
        "node-list",
        "manifest",
        "pin",
        "pins",
        "pinned-manifest",
    ]
    receipt = private_import._receipt(
        upgrade.ROOT / "candidates" / (candidate.digest[7:] + ".json")
    )
    producer.verify_receipt(receipt, candidate.inventory)
    assert upgrade._candidate(candidate.pinned)[0] == receipt
    assert producer.status(OP)["state"] == "verified"
    assert (directory / "frozen.json").stat().st_mode & 0o077 == 0
    assert (directory / "context").stat().st_mode & 0o222 == 0


@pytest.mark.parametrize("stage", ["build", "contents", "import", "manifest", "pin"])
def test_failed_stage_never_publishes_or_replays(candidate, stage):
    candidate.state["fail"] = stage
    with pytest.raises(target.AdmissionError):
        run_build()
    assert not (upgrade.ROOT / "candidates").exists()
    assert producer.status(OP)["state"] == "failed_or_uncertain_no_replay"
    count = len(candidate.state["commands"])
    with pytest.raises(target.AdmissionError, match="never replay"):
        run_build()
    assert len(candidate.state["commands"]) == count


def test_interrupted_build_reconnect_reports_unknown_not_success(candidate):
    candidate.state["crash"] = "build"
    with pytest.raises(KeyboardInterrupt):
        run_build()
    assert producer.status(OP)["state"] == "interrupted_or_running_unknown_no_replay"
    with pytest.raises(target.AdmissionError, match="never replay"):
        run_build()
    assert not (upgrade.ROOT / "candidates").exists()


def test_target_drift_after_build_blocks_import(candidate):
    candidate.state["drift"] = "contents"
    with pytest.raises(target.AdmissionError, match="target changed"):
        run_build()
    assert "import" not in candidate.state["commands"]
    assert not (upgrade.ROOT / "candidates").exists()


def test_stale_plan_blocks_build(candidate):
    saved = producer.plan()
    (candidate.repo / "src/tessera_control/store.py").write_text("changed fence")
    with pytest.raises(target.AdmissionError):
        producer.build(OP, saved["build_plan_sha256"])
    assert not candidate.state["commands"]


@pytest.mark.parametrize("mismatch", ["package", "manifest", "context"])
def test_mismatched_built_image_or_frozen_context_never_publishes(candidate, monkeypatch, mismatch):
    original = producer._docker

    def altered(directory, label, *args, **kwargs):
        path = original(directory, label, *args, **kwargs)
        if mismatch == "package" and label == "contents":
            value = json.loads(path.read_text())
            value["package_sha256"] = "0" * 64
            path.write_text(json.dumps(value))
        if mismatch == "manifest" and label == "manifest":
            path.write_text('{"config":{"digest":"different-image"}}')
        if mismatch == "context" and label == "contents":
            source = directory / "context/README.md"
            source.chmod(0o600)
            source.write_text("unexpected postfreeze mutation")
        return path

    monkeypatch.setattr(producer, "_docker", altered)
    with pytest.raises(target.AdmissionError):
        run_build()
    assert not (upgrade.ROOT / "candidates").exists()


def test_insufficient_capacity_rejects_before_freeze_or_build(candidate, monkeypatch):
    def insufficient():
        raise target.AdmissionError("insufficient capacity")

    monkeypatch.setattr(producer, "_capacity", insufficient)
    with pytest.raises(target.AdmissionError, match="capacity"):
        run_build()
    assert not candidate.state["commands"]
    assert not (upgrade.ROOT / "builds").exists()


def test_forged_or_stale_target_receipt_rejected(candidate):
    run_build()
    receipt = private_import._receipt(
        upgrade.ROOT / "candidates" / (candidate.digest[7:] + ".json")
    )
    forged = receipt | {"image_config": "sha256:" + "f" * 64}
    with pytest.raises(target.AdmissionError, match="provenance"):
        producer.verify_receipt(forged, candidate.inventory)
    candidate.runtime["node_id"] = "different-node"
    with pytest.raises(target.AdmissionError, match="runtime"):
        upgrade._candidate(candidate.pinned)


def test_real_local_command_failure_retains_stdout_stderr_and_exit(tmp_path):
    with pytest.raises(target.AdmissionError, match="failed/uncertain"):
        producer._run(
            tmp_path,
            "failure",
            [
                sys.executable,
                "-c",
                "import sys; print('synthetic output'); "
                "print('synthetic error',file=sys.stderr); sys.exit(7)",
            ],
        )
    receipt = private_import._receipt(tmp_path / "failure.json")
    assert receipt["exit_code"] == 7
    assert "synthetic output" in (tmp_path / "failure.stdout").read_text()
    assert "synthetic error" in (tmp_path / "failure.stderr").read_text()


@pytest.mark.parametrize(
    "override",
    [
        "BUILDX_BUILDER",
        "EXPERIMENTAL_BUILDKIT_SOURCE_POLICY",
        "BUILDKIT_HOST",
        "DOCKER_HOST",
        "DOCKER_TLS_VERIFY",
    ],
)
def test_rev006_ambient_override_not_forwarded(tmp_path, monkeypatch, override):
    monkeypatch.setenv(override, "synthetic-unreviewed-override")
    observed = []

    def command(argv, **kwargs):
        observed.append(dict(kwargs.get("env", os.environ)))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(producer.subprocess, "run", command)
    producer._run(tmp_path, "command", ["synthetic-only"])
    archive = tmp_path / "image.tar"
    archive.write_bytes(b"synthetic")
    producer._import(tmp_path, archive)
    assert all(override not in env for env in observed)


def test_rev006_build_explicitly_selects_local_builder(candidate):
    run_build()
    command = private_import._receipt(upgrade.ROOT / "builds" / OP / "build.json")["argv"]
    assert "--builder" in command
    assert command[command.index("--builder") + 1] == "desktop-linux"
    assert command[:2] == ["buildx", "build"]


@pytest.mark.parametrize("drift", ["driver", "endpoint", "multiple", "flags"])
def test_rev006_remote_or_custom_builder_rejected(monkeypatch, drift):
    value = {
        "Name": "desktop-linux",
        "Driver": "docker",
        "Nodes": [
            {
                "Name": "desktop-linux",
                "Endpoint": "desktop-linux",
                "Status": "running",
                "Buildkit": "v0.25.1",
                "DriverOpts": None,
                "BuildkitdFlags": None,
            }
        ],
    }
    if drift == "driver":
        value["Driver"] = "remote"
    if drift == "endpoint":
        value["Nodes"][0]["Endpoint"] = "tcp://synthetic-remote:1234"
    if drift == "multiple":
        value["Nodes"] *= 2
    if drift == "flags":
        value["Nodes"][0]["BuildkitdFlags"] = ["synthetic-custom-policy"]
    monkeypatch.setattr(producer, "_query", lambda *args: json.dumps(value))
    with pytest.raises(target.AdmissionError, match="local embedded"):
        producer._builder_identity()


def test_rev006_local_builder_identity_and_queries_use_sanitized_env(monkeypatch):
    value = {
        "Name": "desktop-linux",
        "Driver": "docker",
        "Nodes": [
            {
                "Name": "desktop-linux",
                "Endpoint": "desktop-linux",
                "Status": "running",
                "Buildkit": "v0.25.1",
            }
        ],
    }
    monkeypatch.setenv("DOCKER_CONFIG", "/synthetic/unreviewed-config")
    monkeypatch.setenv("BUILDX_CONFIG", "/synthetic/unreviewed-builder-config")
    monkeypatch.setenv("BUILDX_BUILDER", "synthetic-remote")

    def command(argv, **kwargs):
        assert argv == [
            "docker",
            "--context",
            "desktop-linux",
            "buildx",
            "inspect",
            "desktop-linux",
            "--format",
            "{{json .}}",
        ]
        env = kwargs["env"]
        assert env["DOCKER_CONFIG"] == "/Users/zach/.docker"
        assert env["BUILDX_CONFIG"] == "/Users/zach/.docker/buildx"
        assert "BUILDX_BUILDER" not in env
        return SimpleNamespace(returncode=0, stdout=json.dumps(value).encode())

    monkeypatch.setattr(producer.subprocess, "run", command)
    assert producer._builder_identity() == {
        "name": "desktop-linux",
        "driver": "docker",
        "endpoint": "desktop-linux",
        "node": "desktop-linux",
        "buildkit": "v0.25.1",
    }


def test_rev007_pin_name_with_replaced_descriptor_fails_closed(candidate, monkeypatch):
    original = producer._docker

    def replaced(directory, label, *args, **kwargs):
        path = original(directory, label, *args, **kwargs)
        if label == "pins":
            # Simulate mutable source tag changing after its descriptor inspection;
            # final pin name exists but points at another descriptor/config.
            if "--quiet" in args:
                path.write_text(candidate.pinned)
            else:
                path.write_text(candidate.pinned + " type sha256:" + "f" * 64)
        return path

    monkeypatch.setattr(producer, "_docker", replaced)
    with pytest.raises(target.AdmissionError, match="pin.*(digest|descriptor|identity)"):
        run_build()
    assert not (upgrade.ROOT / "candidates").exists()
    assert producer.status(OP)["state"] == "failed_or_uncertain_no_replay"
