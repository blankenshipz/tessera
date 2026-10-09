import hashlib
import json
from pathlib import Path

import pytest

from tessera_control.dataset_snapshots import manifest_bytes, verify_dataset_bytes
from tessera_starter.synthetic import example
from tessera_starter.workload import VARIANTS, run


@pytest.fixture
def snapshot(tmp_path):
    content, declaration = example()
    manifest = verify_dataset_bytes(
        content,
        declaration,
        expected_sha256=hashlib.sha256(content).hexdigest(),
        expected_size=len(content),
    )
    encoded = manifest_bytes(manifest)
    (tmp_path / "dataset.csv").write_bytes(content)
    (tmp_path / "dataset-manifest.json").write_bytes(encoded)
    return tmp_path.resolve(), hashlib.sha256(encoded).hexdigest()


@pytest.mark.parametrize("variant", VARIANTS)
def test_real_four_variant_math_preserves_splits(snapshot, variant):
    root, digest = snapshot
    result = run(root, digest, variant)
    assert result == run(root, digest, variant)
    assert [s["row_count"] for s in result["provenance"]["splits"]] == [80, 20, 20]
    assert result["disposition"] == "quarantine-human-review-required"
    assert result["scientific_validation"] == "not-established"
    if variant == "linear":
        assert result["metrics"]["test"]["mse"] < 0.2


def test_cancel_before_compute(snapshot):
    with pytest.raises(InterruptedError):
        run(*snapshot, "constant", cancelled=lambda: True)


def test_unregistered_variant(snapshot):
    with pytest.raises(ValueError):
        run(*snapshot, "fifth")


def test_deadline_bound(snapshot):
    with pytest.raises(ValueError):
        run(*snapshot, "constant", seconds=121)


def test_changed_bytes(snapshot):
    root, digest = snapshot
    (root / "dataset.csv").write_bytes(b"changed")
    with pytest.raises(ValueError):
        run(root, digest, "constant")


def test_symlink(snapshot, tmp_path):
    root, digest = snapshot
    content = (root / "dataset.csv").read_bytes()
    (root / "dataset.csv").unlink()
    other = tmp_path / "other.csv"
    other.write_bytes(content)
    (root / "dataset.csv").symlink_to(other)
    with pytest.raises(ValueError):
        run(root, digest, "constant")


def test_cli_failure_has_no_metrics(snapshot, tmp_path):
    import os
    import subprocess
    import sys

    root, digest = snapshot
    output = tmp_path / "output"
    output.mkdir()
    command = [
        sys.executable,
        "-m",
        "tessera_starter",
        "--snapshot-root",
        str(root),
        "--manifest-digest",
        "0" * 64,
        "--variant",
        "linear",
        "--output-root",
        str(output),
    ]
    done = subprocess.run(
        command,
        env={**os.environ, "PYTHONPATH": str(Path("src").resolve())},
        capture_output=True,
        timeout=15,
    )
    assert done.returncode == 2
    result = json.loads((output / "result.json").read_text())
    assert result["status"] == "inconclusive" and "metrics" not in result


def test_synthetic_bytes_reproducible():
    assert example() == example()


def test_portable_verifier_uses_actual_pure_definitions(snapshot):
    import importlib.util
    import types

    helper = Path("examples/tabular-starter/portable_verifier.py")
    spec = importlib.util.spec_from_file_location("portable_builder", helper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = module.slice_verifier(Path("src/tessera_control/dataset_snapshots.py"))
    portable = types.ModuleType("tessera_control.portable_verifier")
    portable.__package__ = "tessera_control"
    exec(compile(source, "portable-verifier", "exec"), portable.__dict__)
    root, digest = snapshot
    observed, content = portable.load_dataset_snapshot(root, expected_manifest_digest=digest)
    assert content == example()[0]
    assert observed.data_sha256 == hashlib.sha256(content).hexdigest()
    assert b"class DatasetSnapshotStore" not in source


def test_deadline_during_real_read(snapshot, monkeypatch):
    import tessera_starter.workload as workload

    moments = iter((0, 0, 121))
    monkeypatch.setattr(workload.time, "monotonic", lambda: next(moments))
    with pytest.raises(TimeoutError):
        run(*snapshot, "linear")


def test_singular_training_fallback(snapshot):
    root, _ = snapshot
    _, declaration = example()
    content = b"id,x,target,split\na,1,2,train\nb,1,4,validation\nc,1,6,test\n"
    manifest = verify_dataset_bytes(
        content,
        declaration,
        expected_sha256=hashlib.sha256(content).hexdigest(),
        expected_size=len(content),
    )
    encoded = manifest_bytes(manifest)
    (root / "dataset.csv").write_bytes(content)
    (root / "dataset-manifest.json").write_bytes(encoded)
    result = run(root, hashlib.sha256(encoded).hexdigest(), "linear")
    assert result["model"]["fallback"] == "singular-training-feature:constant-train-mean"


def test_numeric_magnitude_fails_closed(snapshot):
    root, _ = snapshot
    _, declaration = example()
    content = b"id,x,target,split\na,1e200,2,train\nb,1,4,validation\nc,1,6,test\n"
    manifest = verify_dataset_bytes(
        content,
        declaration,
        expected_sha256=hashlib.sha256(content).hexdigest(),
        expected_size=len(content),
    )
    encoded = manifest_bytes(manifest)
    (root / "dataset.csv").write_bytes(content)
    (root / "dataset-manifest.json").write_bytes(encoded)
    with pytest.raises(ValueError, match="magnitude"):
        run(root, hashlib.sha256(encoded).hexdigest(), "linear")


def test_normal_api_four_preregistered_trials_review_and_export(tmp_path):
    """Actual ASGI/SQLite/CLI evidence, not native-Job/provider acceptance.

    Opt-in TESSERA_TABULAR_TEST_IMAGE runs actual already-built local Docker image.
    No inference or credential copying; temporary synthetic human/worker identities.
    """
    import base64
    import os
    import subprocess
    import sys
    from datetime import UTC, datetime, timedelta

    from fastapi.testclient import TestClient
    from test_dataset_snapshot_api import command, upload_body
    from test_execution import _setup

    from tessera_control.api import create_app
    from tessera_worker.inputs import materialize_snapshot

    state, _, _, worker, project, _ = _setup(tmp_path)
    client = TestClient(create_app(state))
    human = {"Authorization": (state / "owner.header").read_text().strip().split(": ", 1)[1]}
    worker_header = {"Authorization": "Bearer worker-token"}
    body = upload_body(*example())
    snapshot = command(
        client, human, f"/projects/{project['id']}/tabular-dataset-snapshots", body, "starter-adopt"
    )
    command(
        client,
        human,
        f"/snapshots/{snapshot['id']}/releases",
        {
            "expected_digest": snapshot["digest"],
            "worker_principal_id": worker["id"],
            "classification": "public",
            "permits_worker_transfer": True,
            "rationale": "Reviewed public synthetic four-trial example",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=20)).isoformat(),
        },
        "starter-release",
    )
    configured = client.put(
        f"/projects/{project['id']}/execution-limits",
        headers={**human, "Idempotency-Key": "starter-limits"},
        json={
            "expected_revision": 1,
            "currency": "USD",
            "monetary_limit_cents": 240,
            "max_concurrency": 1,
        },
    )
    assert configured.status_code == 200, configured.text
    tasks = []
    for variant in VARIANTS:
        task = command(
            client,
            human,
            f"/projects/{project['id']}/tasks",
            {
                "title": f"Preregistered {variant}",
                "purpose": "Bounded synthetic engineering comparison, not scientific validation",
                "budget_cents": 60,
                "max_duration_seconds": 120,
            },
            f"task-{variant}",
        )
        argv = [
            "python",
            "-m",
            "tessera_starter",
            "--snapshot-root",
            "/dataset",
            "--manifest-digest",
            snapshot["dataset_manifest_digest"],
            "--variant",
            variant,
            "--output-root",
            "/output",
        ]
        response = client.put(
            f"/tasks/{task['id']}/plan",
            headers={**human, "Idempotency-Key": f"plan-{variant}"},
            json={
                "expected_revision": task["revision"],
                "title": task["title"],
                "purpose": task["purpose"],
                "budget_cents": 60,
                "max_duration_seconds": 120,
                "execution_spec": {
                    "profile": "local-cpu",
                    "argv": argv,
                    "input_snapshot_digest": snapshot["digest"],
                    "pinned_skill_digests": [],
                    "output_paths": ["result.json"],
                    "network_enabled": False,
                    "max_output_bytes": 1024 * 1024,
                },
            },
        )
        assert response.status_code == 200, response.text
        tasks.append(response.json())
    # All four immutable plans exist BEFORE any approval or grant.
    approved = [
        command(
            client,
            human,
            f"/tasks/{t['id']}/approve",
            {
                "expected_revision": t["revision"],
                "expected_plan_digest": t["current_plan_digest"],
            },
            f"approval-{i}",
            expected=200,
        )
        for i, t in enumerate(tasks)
    ]
    for variant, task in zip(VARIANTS, approved, strict=True):
        grant = command(
            client,
            human,
            f"/projects/{project['id']}/execution-grants",
            {
                "task_id": task["id"],
                "expected_task_revision": task["revision"],
                "expected_plan_digest": task["current_plan_digest"],
                "worker_principal_id": worker["id"],
                "snapshot_id": snapshot["id"],
                "pinned_image": "tessera-tabular-starter@"
                + os.environ.get(
                    "TESSERA_TABULAR_TEST_IMAGE",
                    json.loads(Path("examples/tabular-starter/IMAGE.lock.json").read_text())[
                        "workload_manifest_digest"
                    ],
                ),
                "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                "max_exposure_cents": 60,
                "currency": "USD",
                "acknowledges_unmeasured_infrastructure": True,
            },
            f"grant-{variant}",
        )
        lease = command(
            client, worker_header, "/worker/leases", {"grant_id": grant["id"]}, f"lease-{variant}"
        )
        bundle = command(
            client,
            worker_header,
            f"/worker/leases/{lease['id']}/inputs",
            {"expected_epoch": lease["epoch"]},
            f"inputs-{variant}",
        )
        staged = materialize_snapshot(
            bundle,
            tmp_path,
            expected_snapshot_digest=snapshot["digest"],
            expected_lease_id=lease["id"],
            expected_epoch=lease["epoch"],
            expected_worker_id=worker["id"],
        )
        output = tmp_path / variant
        output.mkdir()
        image = os.environ.get("TESSERA_TABULAR_TEST_IMAGE")
        if image:
            output.chmod(0o777)
            # This explicit public release is mounted into a distinct non-root
            # UID; permissions are read-only, not an authority substitute.
            staged.directory.chmod(0o555)
            for file in staged.directory.iterdir():
                file.chmod(0o444)
            argv = [
                "docker",
                "run",
                "--rm",
                "--platform",
                "linux/amd64",
                "--network",
                "none",
                "--read-only",
                "--user",
                "65532:65532",
                "--cpus",
                "1",
                "--memory",
                "512m",
                "--pids-limit",
                "32",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--label",
                "tessera.owner=tabular-v8-leaf",
                "--cidfile",
                str(tmp_path / f"{variant}.cid"),
                "--mount",
                f"type=bind,source={staged.directory},target=/dataset,readonly",
                "--mount",
                f"type=bind,source={output},target=/output",
                image,
                "--snapshot-root",
                "/dataset",
            ]
        else:
            argv = [
                sys.executable,
                "-m",
                "tessera_starter",
                "--snapshot-root",
                str(staged.directory),
            ]
        argv += [
            "--manifest-digest",
            snapshot["dataset_manifest_digest"],
            "--variant",
            variant,
            "--output-root",
            "/output" if image else str(output),
        ]
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(Path("src").resolve())},
        )
        stdout, stderr = process.communicate(timeout=120)
        done = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
        assert done.returncode == 0, done.stderr.decode()
        provider_id = (
            (tmp_path / f"{variant}.cid").read_text().strip()
            if image
            else f"local-process-{process.pid}"
        )
        content = (output / "result.json").read_bytes()
        assert json.loads(content)["status"] == "succeeded"
        operation = hashlib.sha256(f"tessera-cpu-v1:{lease['id']}".encode()).hexdigest()[:32]
        binding = {"expected_epoch": lease["epoch"], "operation_id": operation}
        artifact = command(
            client,
            worker_header,
            f"/worker/leases/{lease['id']}/artifacts",
            binding
            | {
                "relative_path": "result.json",
                "sha256": hashlib.sha256(content).hexdigest(),
                "content_base64": base64.b64encode(content).decode(),
            },
            f"artifact-{variant}",
        )
        command(
            client,
            worker_header,
            f"/worker/leases/{lease['id']}/project-artifacts",
            binding
            | {
                "artifacts": [{k: artifact[k] for k in ("id", "sha256", "size")}],
            },
            f"placement-{variant}",
        )
        manifest = command(
            client,
            worker_header,
            f"/worker/leases/{lease['id']}/artifact-manifest",
            binding
            | {
                "artifacts": [{k: artifact[k] for k in ("id", "relative_path", "sha256", "size")}],
            },
            f"manifest-{variant}",
        )
        assert manifest["status"] == "quarantined"
        path = f"/artifact-manifests/{manifest['id']}/artifacts/{artifact['id']}/content"
        assert client.get(path, headers=human).status_code == 403
        command(
            client,
            human,
            f"/artifact-manifests/{manifest['id']}/dispositions",
            {
                "expected_manifest_digest": manifest["manifest_digest"],
                "expected_revision": 0,
                "disposition": "retain_quarantined",
                "rationale": "Human-reviewed synthetic result; no scientific validation",
            },
            f"review-{variant}",
        )
        command(
            client,
            human,
            f"/artifact-manifests/{manifest['id']}/custody-releases",
            {
                "expected_manifest_digest": manifest["manifest_digest"],
                "expected_revision": 0,
                "action": "release",
                "rationale": "Explicit private reviewed result export",
            },
            f"export-{variant}",
        )
        exported = client.get(path, headers=human)
        assert exported.status_code == 200 and exported.content == content
        command(
            client,
            worker_header,
            "/worker/observations",
            {
                "lease_id": lease["id"],
                "expected_epoch": lease["epoch"],
                "observed_state": "exited",
                "provider_id": provider_id,
                "observed_at": datetime.now(UTC).isoformat(),
                "termination_confirmed": True,
            },
            f"exited-{variant}",
        )
        current = client.get(f"/execution-grants/{grant['id']}", headers=human).json()
        command(
            client,
            human,
            f"/execution-grants/{grant['id']}/reconcile",
            {
                "expected_revision": current["revision"],
                "settlement_cents": 60,
                "provider_terminated": True,
            },
            f"settle-{variant}",
            expected=200,
        )
