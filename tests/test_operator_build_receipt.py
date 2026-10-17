"""Synthetic Docker/node runner; local Git/SQLite-independent provenance tests.

No live image builds/imports/containers/namespace changes.
"""

import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

from tessera_operator import build_receipt as producer
from tessera_operator import upgrade
from tessera_operator import upgrade_support as activation
from tessera_operator import upgrade_support as private_import
from tessera_operator import upgrade_support as target

OP = "a" * 32

# Captured successful native v0.36.1-desktop.1 inspect output. Only runtime UUID,
# host-gateway address and activity timestamp are sanitized; grammar is retained.
INSTALLED_BUILDER_INSPECT = """Name:          desktop-linux
Driver:        docker
Last Activity: 2026-10-15 00:00:00 +0000 UTC

Nodes:
Name:             desktop-linux
Endpoint:         desktop-linux
Status:           running
BuildKit version: v0.32.2
Platforms:        linux/amd64, linux/amd64/v2, linux/amd64/v3, linux/arm64, \
linux/riscv64, linux/ppc64le, linux/s390x, linux/arm/v7, linux/arm/v6
Labels:
 org.mobyproject.buildkit.worker.containerd.namespace: moby
 org.mobyproject.buildkit.worker.containerd.uuid:      fixture-uuid
 org.mobyproject.buildkit.worker.executor:             containerd
 org.mobyproject.buildkit.worker.hostname:             docker-desktop
 org.mobyproject.buildkit.worker.moby.host-gateway-ip: 192.0.2.1
 org.mobyproject.buildkit.worker.network:              host
 org.mobyproject.buildkit.worker.selinux.enabled:      false
 org.mobyproject.buildkit.worker.snapshotter:          overlayfs
Devices:
 Name:                  docker.com/gpu=webgpu
 Automatically allowed: false
GC Policy rule#0:
 All:            false
 Filters:        type==source.local type==exec.cachemount type==source.git.checkout
 Keep Duration:  48h0m0s
 Max Used Space: 2.764GiB
GC Policy rule#1:
 All:            false
 Keep Duration:  1440h0m0s
 Reserved Space: 20GiB
GC Policy rule#2:
 All:            false
 Reserved Space: 20GiB
GC Policy rule#3:
 All:            true
 Reserved Space: 20GiB
"""


def _identity_fixture(shape="descriptor", nested=False):
    """Sanitized real export shape; all blob contents/digests are synthetic."""
    files = {}

    def blob(data, media):
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        files["blobs/sha256/" + digest[7:]] = data
        return {"digest": digest, "size": len(data), "mediaType": media}

    def encoded(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

    raw = b"synthetic uncompressed layer"
    diff = "sha256:" + hashlib.sha256(raw).hexdigest()
    layer = blob(gzip.compress(raw, mtime=0), "application/vnd.oci.image.layer.v1.tar+gzip")
    config = blob(
        encoded(
            {
                "os": "linux",
                "architecture": "amd64",
                "rootfs": {"type": "layers", "diff_ids": [diff]},
            }
        ),
        "application/vnd.oci.image.config.v1+json",
    )
    manifest = blob(
        encoded(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": config,
                "layers": [layer],
            }
        ),
        "application/vnd.oci.image.manifest.v1+json",
    )
    root = manifest
    if nested:
        root = blob(
            encoded(
                {
                    "schemaVersion": 2,
                    "mediaType": "application/vnd.oci.image.index.v1+json",
                    "manifests": [
                        manifest | {"platform": {"os": "linux", "architecture": "amd64"}}
                    ],
                }
            ),
            "application/vnd.oci.image.index.v1+json",
        )
    files["oci-layout"] = encoded({"imageLayoutVersion": "1.0.0"})
    files["index.json"] = encoded(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [root],
        }
    )
    files["manifest.json"] = encoded(
        [
            {
                "Config": "blobs/sha256/" + config["digest"][7:],
                "Layers": ["blobs/sha256/" + layer["digest"][7:]],
            }
        ]
    )
    image = {
        "Id": config["digest"] if shape == "classic" else root["digest"],
        "Os": "linux",
        "Architecture": "amd64",
        "RootFS": {"Layers": [diff]},
    }
    if shape != "classic":
        image["Descriptor"] = root | {"annotations": {"config.digest": config["digest"]}}
    return SimpleNamespace(
        files=files, image=image, root=root, config=config, manifest=manifest, layer=layer
    )


def _write_identity_archive(path, fixture):
    with tarfile.open(path, "w") as archive:
        for name, data in fixture.files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))


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
    identity_fixture = _identity_fixture("classic")
    digest = identity_fixture.root["digest"]
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
            data = json.dumps([identity_fixture.image]).encode()
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
        if label.startswith(("manifest", "pinned-manifest")):
            data = identity_fixture.files["blobs/sha256/" + args[-1][7:]]
        if label == "pins":
            data = (pinned + " type " + digest).encode()
        out.write_bytes(data)
        if label == "export-tagged":
            _write_identity_archive(directory / "tagged.tar", identity_fixture)
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
        "manifest-child-1",
        "pin",
        "pins",
        "pinned-manifest",
        "pinned-manifest-child-1",
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
    value = INSTALLED_BUILDER_INSPECT
    if drift == "driver":
        value = value.replace("Driver:        docker", "Driver:        remote")
    if drift == "endpoint":
        value = value.replace(
            "Endpoint:         desktop-linux", "Endpoint:         tcp://synthetic-remote:1234"
        )
    if drift == "multiple":
        value += "\nName: second-node\nEndpoint: other\n"
    if drift == "flags":
        value = value.replace("Labels:", "BuildkitdFlags: synthetic-custom-policy\nLabels:")
    monkeypatch.setattr(producer, "_query", lambda *args: value)
    with pytest.raises(target.AdmissionError, match="local embedded"):
        producer._builder_identity()


def test_rev006_local_builder_identity_and_queries_use_sanitized_env(monkeypatch):
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
        ]
        env = kwargs["env"]
        assert env["DOCKER_CONFIG"] == "/Users/zach/.docker"
        assert env["BUILDX_CONFIG"] == "/Users/zach/.docker/buildx"
        assert "BUILDX_BUILDER" not in env
        # Exact command assertion ensures the previously unsupported --format
        # path is never retried, even though a synthetic runner could accept it.
        return SimpleNamespace(returncode=0, stdout=INSTALLED_BUILDER_INSPECT.encode())

    monkeypatch.setattr(producer.subprocess, "run", command)
    assert producer._builder_identity() == {
        "name": "desktop-linux",
        "driver": "docker",
        "endpoint": "desktop-linux",
        "node": "desktop-linux",
        "buildkit": "v0.32.2",
    }


@pytest.mark.parametrize(
    "drift",
    [
        "truncated",
        "metadata-truncated",
        "duplicate",
        "unknown",
        "stopped",
        "version",
        "wrong-name",
        "oversized",
    ],
)
def test_native_builder_output_rejects_ambiguous_or_unrecognized(monkeypatch, drift):
    value = INSTALLED_BUILDER_INSPECT
    if drift == "truncated":
        value = value.split("BuildKit version:")[0]
    if drift == "metadata-truncated":
        value = value.split("Labels:")[0] + "Labels:"
    if drift == "duplicate":
        value = value.replace("Endpoint:", "Status: running\nEndpoint:")
    if drift == "unknown":
        value = value.replace("Labels:", "Source policy: unreviewed\nLabels:")
    if drift == "stopped":
        value = value.replace("Status:           running", "Status:           stopped")
    if drift == "version":
        value = value.replace("v0.32.2", "unknown-version")
    if drift == "wrong-name":
        value = value.replace("Name:          desktop-linux", "Name:          other")
    if drift == "oversized":
        value += " " * 65536
    monkeypatch.setattr(producer, "_query", lambda *args: value)
    with pytest.raises(target.AdmissionError, match="local embedded"):
        producer._builder_identity()


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


@pytest.mark.parametrize("shape", ["classic", "descriptor"])
@pytest.mark.parametrize("nested", [False, True])
def test_typed_archive_chain_config_or_manifest_id(tmp_path, shape, nested):
    fixture = _identity_fixture(shape, nested)
    path = tmp_path / "image.tar"
    _write_identity_archive(path, fixture)
    identity = producer._archive_identity(path, fixture.image)
    assert identity["root_digest"] == fixture.root["digest"]
    assert identity["config_digest"] == fixture.config["digest"]
    assert identity["manifest_digest"] == fixture.manifest["digest"]
    assert identity["identity_kind"] == (
        "classic-config-id" if shape == "classic" else "descriptor-id"
    )
    assert len(identity["chain"]) == (3 if nested else 2)


@pytest.mark.parametrize(
    "substitution",
    ["config", "manifest", "index", "layer", "hint", "id", "ambiguous-index", "diff-id"],
)
def test_typed_archive_substitutions_fail_closed(tmp_path, substitution):
    fixture = _identity_fixture("descriptor", nested=True)
    if substitution in {"config", "manifest", "index", "layer"}:
        desc = {
            "config": fixture.config,
            "manifest": fixture.manifest,
            "index": fixture.root,
            "layer": fixture.layer,
        }[substitution]
        key = "blobs/sha256/" + desc["digest"][7:]
        data = fixture.files[key]
        fixture.files[key] = data[:-1] + bytes([data[-1] ^ 1])
    if substitution == "hint":
        fixture.image["Descriptor"]["annotations"]["config.digest"] = "sha256:" + "f" * 64
    if substitution == "id":
        fixture.image["Id"] = fixture.config["digest"]
    if substitution == "ambiguous-index":
        index = json.loads(fixture.files["index.json"])
        index["manifests"] *= 2
        fixture.files["index.json"] = json.dumps(index).encode()
    if substitution == "diff-id":
        fixture.image["RootFS"]["Layers"] = ["sha256:" + "0" * 64]
    path = tmp_path / "image.tar"
    _write_identity_archive(path, fixture)
    with pytest.raises(target.AdmissionError, match="descriptor/config/layer"):
        producer._archive_identity(path, fixture.image)


@pytest.mark.parametrize("label", ["manifest", "pinned-manifest"])
@pytest.mark.parametrize("substitution", ["root", "config"])
def test_import_and_final_node_chain_cannot_substitute(tmp_path, monkeypatch, label, substitution):
    fixture = _identity_fixture()
    path = tmp_path / "image.tar"
    _write_identity_archive(path, fixture)
    identity = producer._archive_identity(path, fixture.image)
    changed = fixture.root if substitution == "root" else fixture.config

    def query(directory, stage, *args):
        data = fixture.files["blobs/sha256/" + args[-1][7:]]
        if args[-1] == changed["digest"]:
            data = data[:-1] + bytes([data[-1] ^ 1])
        result = directory / (stage + ".stdout")
        result.write_bytes(data)
        return result

    monkeypatch.setattr(producer, "_docker", query)
    with pytest.raises(target.AdmissionError, match="node descriptor/config"):
        producer._verify_node_identity(tmp_path, label, identity["root_digest"], identity)


def test_classic_config_anchor_requires_uncompressed_layer_digest(tmp_path):
    fixture = _identity_fixture("classic")
    # Rehash a substituted layer and all manifest/export parents coherently,
    # keeping the inspected config ID/diff IDs unchanged. Parent-only checks
    # would pass, but actual uncompressed layer content must still fail.
    data = gzip.compress(b"different uncompressed layer", mtime=0)
    changed = {
        "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "mediaType": fixture.layer["mediaType"],
    }
    fixture.files["blobs/sha256/" + changed["digest"][7:]] = data
    manifest = json.loads(fixture.files["blobs/sha256/" + fixture.root["digest"][7:]])
    manifest["layers"] = [changed]
    data = json.dumps(manifest).encode()
    parent = {
        "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "mediaType": fixture.root["mediaType"],
    }
    fixture.files["blobs/sha256/" + parent["digest"][7:]] = data
    index = json.loads(fixture.files["index.json"])
    index["manifests"] = [parent]
    fixture.files["index.json"] = json.dumps(index).encode()
    legacy = json.loads(fixture.files["manifest.json"])
    legacy[0]["Layers"] = ["blobs/sha256/" + changed["digest"][7:]]
    fixture.files["manifest.json"] = json.dumps(legacy).encode()
    path = tmp_path / "substituted.tar"
    _write_identity_archive(path, fixture)
    with pytest.raises(target.AdmissionError, match="descriptor/config/layer"):
        producer._archive_identity(path, fixture.image)
