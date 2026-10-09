import base64
import hashlib
from copy import deepcopy

import pytest
from test_kubernetes_contracts import _binding, _cluster

from tessera_control.dataset_snapshots import (
    build_dataset_capture,
    manifest_bytes,
    verify_dataset_bytes,
)
from tessera_starter.materialize import materialize
from tessera_starter.synthetic import example
from tessera_worker.dataset_inputs import (
    create_input_steps,
    create_inputs,
    delete_input,
    prepare_dataset_inputs,
    validate_descriptor,
)
from tessera_worker.kubernetes_contracts import KubernetesContractError, job_manifest, parse_job


def delivery(content=None, *, include_bundle=False):
    generated, declaration = example()
    content = generated if content is None else content
    data_hash = hashlib.sha256(content).hexdigest()
    manifest = verify_dataset_bytes(
        content, declaration, expected_sha256=data_hash, expected_size=len(content)
    )
    manifest_digest = hashlib.sha256(manifest_bytes(manifest)).hexdigest()
    capture = build_dataset_capture(
        content,
        declaration,
        expected_sha256=data_hash,
        expected_size=len(content),
        expected_manifest_digest=manifest_digest,
    )
    inventory = capture.inventory
    # Canonical existing snapshot envelope (actual bytes, synthetic authority fixture).
    import dataclasses
    import json

    envelope = dataclasses.asdict(inventory)
    envelope["entries"] = list(envelope["entries"])
    envelope["exclusions"] = list(envelope["exclusions"])
    snapshot_digest = hashlib.sha256(
        json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    bundle = {
        "receipt": {
            "lease_id": "lease-1",
            "grant_id": "grant-1",
            "epoch": 7,
            "worker_principal_id": "worker-1",
            "snapshot_digest": snapshot_digest,
            "snapshot_id": "snapshot-1",
            "delivery_confirmed": False,
        },
        "snapshot": {
            "id": "snapshot-1",
            "digest": snapshot_digest,
            "manifest": envelope,
            "files": [
                {"relative_path": name, "content_base64": base64.b64encode(value).decode()}
                for name, value in capture.files
            ],
        },
    }
    descriptor, resources = prepare_dataset_inputs(
        bundle,
        snapshot_digest=snapshot_digest,
        manifest_digest=manifest_digest,
        lease_id="lease-1",
        epoch=7,
        worker_id="worker-1",
        operation_id="1" * 32,
        namespace="tessera-fixture",
        grant_id="grant-1",
        project_id="project-1",
    )
    return (descriptor, resources, bundle) if include_bundle else (descriptor, resources)


def test_real_chunk_materialization(tmp_path):
    descriptor, resources = delivery()
    projection = tmp_path / "projection"
    projection.mkdir()
    actual = projection / "..timestamp"
    actual.mkdir()
    (projection / "..data").symlink_to("..timestamp")
    for resource in resources:
        for key, value in resource["binaryData"].items():
            (actual / key).write_bytes(base64.b64decode(value))
    destination = tmp_path / "dataset"
    destination.mkdir()
    materialize(projection.resolve(), destination.resolve(), descriptor, "1" * 32)
    assert (destination / "dataset.csv").read_bytes() == example()[0]
    assert (destination / "dataset.csv").stat().st_mode & 0o222 == 0


@pytest.mark.parametrize(
    "field,value",
    [("size", 0), ("sha256", "wrong"), ("file", "../secret"), ("name", "wrong-owner")],
)
def test_bad_descriptor(field, value):
    descriptor, _ = delivery()
    descriptor["parts"][0][field] = value
    with pytest.raises(ValueError):
        validate_descriptor(descriptor, "1" * 32)


def test_job_pins_input_and_readonly_mount():
    descriptor, _ = delivery()
    binding = _binding(
        dataset_input=descriptor,
        active_deadline_seconds=120,
        command=(
            "python",
            "-m",
            "tessera_starter",
            "--snapshot-root",
            "/dataset",
            "--manifest-digest",
            descriptor["manifest_digest"],
            "--variant",
            "linear",
            "--output-root",
            "/work",
        ),
    )
    manifest = job_manifest(binding, _cluster())
    pod = manifest["spec"]["template"]["spec"]
    assert pod["containers"][0]["volumeMounts"][-1]["readOnly"]
    assert pod["initContainers"][0]["image"] == binding.image
    assert (
        manifest["metadata"]["annotations"]["tessera.dev/input-snapshot-sha256"]
        == descriptor["snapshot_digest"]
    )
    assert manifest["spec"]["backoffLimit"] == 0


@pytest.mark.parametrize(
    "change",
    [{"network_mode": "cluster"}, {"memory_mebibytes": 513}, {"active_deadline_seconds": 121}],
)
def test_tighter_profile(change):
    descriptor, _ = delivery()
    values = {"dataset_input": descriptor, "active_deadline_seconds": 120, **change}
    with pytest.raises(ValueError):
        _binding(**values)


def test_cleanup_refuses_wrong_uid():
    _, resources = delivery()
    observed = deepcopy(resources[0])
    observed["metadata"]["uid"] = "other"
    with pytest.raises(ValueError):
        delete_input(
            {"name": observed["metadata"]["name"], "namespace": "tessera-fixture", "uid": "ours"},
            operation_id="1" * 32,
            read=lambda _: observed,
            delete_exact_uid=lambda *_: pytest.fail("unsafe delete"),
        )


def test_near_four_mib_delivery_is_complete_and_safe():
    content = b"id,x,target,split\n" + b"".join(
        f"row-{i},{'0' * 1000},1,{('train', 'validation', 'test')[i % 3]}\n".encode()
        for i in range(4000)
    )
    descriptor, resources = delivery(content)
    assert 3_900_000 < len(content) <= 4 * 1024 * 1024
    assert len(resources) <= 8
    assert sum(p["size"] for p in descriptor["parts"] if p["file"] == "dataset.csv") == len(content)
    import json

    assert all(len(json.dumps(r).encode()) < 1024 * 1024 for r in resources)


def test_each_create_has_current_authority_and_durable_uid():
    _, resources = delivery()
    events, observed = [], {}

    def create(resource):
        events.append("create")
        value = deepcopy(resource)
        value["metadata"]["uid"] = f"uid-{len(observed)}"
        observed[value["metadata"]["name"]] = value
        return value

    receipts = create_inputs(
        resources,
        check_current_authority=lambda: events.append("fence"),
        create=create,
        read=lambda name: observed[name],
        record_receipt=lambda value: events.append("durable-uid"),
    )
    assert len(receipts) == len(resources)
    assert events == ["fence", "create", "durable-uid", "fence"] * len(resources)


def test_cooperative_steps_validate_entire_set_before_first_command():
    from tessera_worker.inputs import InputIntegrityError

    _, resources = delivery()
    resources[-1]["immutable"] = False

    def no_create(_):
        pytest.fail("invalid complete set must not reach create step")
        yield

    steps = create_input_steps(
        resources,
        check_current_authority=lambda: pytest.fail("validation precedes authorization/effects"),
        create_steps=no_create,
        read=lambda _: pytest.fail("no native read"),
        record_receipt=lambda _: pytest.fail("no UID receipt"),
    )
    with pytest.raises(InputIntegrityError):
        next(steps)


def test_expired_authority_creates_nothing():
    _, resources = delivery()

    def deny():
        raise ValueError("lease expired/release revoked")

    with pytest.raises(ValueError):
        create_inputs(
            resources,
            check_current_authority=deny,
            create=lambda _: pytest.fail("unauthorized create"),
            read=lambda _: None,
            record_receipt=lambda _: pytest.fail("unexpected receipt"),
        )


def test_wrong_grant_denies_before_resources():
    with pytest.raises(ValueError, match="grant/project"):
        prepare_dataset_inputs(
            {"receipt": {"grant_id": "wrong"}},
            snapshot_digest="a" * 64,
            manifest_digest="b" * 64,
            lease_id="lease-1",
            epoch=7,
            worker_id="worker-1",
            operation_id="1" * 32,
            namespace="tessera-fixture",
            grant_id="grant-1",
            project_id="project-1",
        )


def test_init_defaults_and_image_mutation_fences():
    descriptor, _ = delivery()
    binding = _binding(
        dataset_input=descriptor,
        active_deadline_seconds=120,
        command=(
            "python",
            "-m",
            "tessera_starter",
            "--snapshot-root",
            "/dataset",
            "--manifest-digest",
            descriptor["manifest_digest"],
            "--variant",
            "linear",
            "--output-root",
            "/work",
        ),
    )
    observed = job_manifest(binding, _cluster())
    observed["metadata"]["uid"] = "job-1"
    observed["status"] = {"active": 1}
    init = observed["spec"]["template"]["spec"]["initContainers"][0]
    init.update(
        {"terminationMessagePath": "/dev/termination-log", "terminationMessagePolicy": "File"}
    )
    assert parse_job(observed, binding, _cluster()).job_uid == "job-1"
    init["image"] = "python:latest"
    with pytest.raises(KubernetesContractError):
        parse_job(observed, binding, _cluster())


@pytest.mark.parametrize("mutation", ("secret", "empty", "different-project", "too-many"))
def test_closed_custody_rejects_mutated_resource_set_before_create(mutation):
    _, resources = delivery()
    if mutation == "secret":
        resources[0]["kind"] = "Secret"
    elif mutation == "empty":
        resources[0]["binaryData"] = {"part-0": ""}
    elif mutation == "different-project":
        resources[1]["metadata"]["labels"]["tessera.dev/project"] = "other"
    else:
        resources *= 5
    with pytest.raises(ValueError):
        create_inputs(
            resources,
            check_current_authority=lambda: pytest.fail("effect before validation"),
            create=lambda _: pytest.fail("unbounded create"),
            read=lambda _: None,
            record_receipt=lambda _: pytest.fail("invalid receipt"),
        )
