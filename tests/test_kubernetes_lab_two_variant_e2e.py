"""Isolated runner contract checks; never evidence of Kubernetes execution."""

import importlib.util
import json
import sqlite3
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_kubernetes_never_submitted import _pre_manifest_fixture

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location(
    "two_variant_fixture", SCRIPTS / "check_kubernetes_lab_two_variant_e2e.py"
)
assert spec and spec.loader
sys.path.insert(0, str(SCRIPTS))
try:
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
finally:
    sys.path.remove(str(SCRIPTS))


def test_collector_bytes_are_actual_distinct_bounded_json() -> None:
    first = runner._output(b'{"iterations":10,"sum_squares":385}\n', "baseline")
    second = runner._output(b'{"iterations":20,"sum_squares":2870}\n', "intervention")
    assert first["sha256"] != second["sha256"]
    assert first["size"] != 0
    for content in (
        b'{"iterations":10,"sum_squares":385,"sum_squares":385}',
        b'{"iterations":10,"sum_squares":386}',
        b'{"iterations":20,"sum_squares":385}',
        b'{"iterations":10,"sum_squares":true}',
        b'{"iterations":10,"sum_squares":385}' * 100,
    ):
        with pytest.raises(runner.TwoVariantError):
            runner._output(content, "baseline")


def test_owner_recovery_bridge_fences_original_journal_and_uses_exact_read_selectors(tmp_path):
    _client, _state, cluster, package, journal, _grant, _worker = _pre_manifest_fixture(tmp_path)
    journal.path.chmod(0o600)

    class Reads:
        def __init__(self):
            self.calls = []
            self.namespace_uid = cluster.namespace_uid
            self.pods = []
            self.job = None

        def _get(self, kind, name, namespace):
            self.calls.append((kind, name, namespace))
            return {"metadata": {"name": name, "uid": self.namespace_uid}}

        def _maybe_get(self, kind, name, namespace):
            self.calls.append((kind, name, namespace))
            return self.job

        def _kubectl(self, *args, namespace, timeout):
            self.calls.append((args, namespace, timeout))
            return SimpleNamespace(
                stdout=json.dumps({"items": self.pods if args[1] == "pods" else []})
            )

    live = Reads()
    proof = runner._owner_pre_manifest_absence_proof(
        live, journal.path, cluster, package.operation_id, package.package_digest
    )
    assert proof["proof_kind"] == "pre_manifest"
    assert proof["journal_manifest_digest"] is None
    selector = "tessera.dev/operation=" + runner.contracts.kubernetes_resource_name(
        package.operation_id, "operation_id"
    )
    assert [
        ("get", kind, "--selector", selector, "-o", "json")
        in [item[0] for item in live.calls if len(item) == 3 and isinstance(item[0], tuple)]
        for kind in ("jobs", "pods")
    ] == [True, True]
    assert all(item[1] == cluster.namespace for item in live.calls if isinstance(item[0], tuple))
    live.pods = [{"metadata": {"uid": "unexpected"}}]
    with pytest.raises(runner.contracts.KubernetesUnresolvedError, match="may exist"):
        runner._owner_pre_manifest_absence_proof(
            live, journal.path, cluster, package.operation_id, package.package_digest
        )
    live.pods = []
    live.job = {"metadata": {"uid": "replacement-job"}}
    with pytest.raises(runner.contracts.KubernetesUnresolvedError, match="may exist"):
        runner._owner_pre_manifest_absence_proof(
            live, journal.path, cluster, package.operation_id, package.package_digest
        )
    live.job = None
    live.namespace_uid = "replacement"
    with pytest.raises(runner.contracts.KubernetesUnresolvedError, match="Namespace UID"):
        runner._owner_pre_manifest_absence_proof(
            live, journal.path, cluster, package.operation_id, package.package_digest
        )


class _Transport:
    def __init__(self):
        self.calls = []
        self.fail_after_commit = False

    def request(self, method, path, body, *, key):
        self.calls.append((method, path, body, key))
        if self.fail_after_commit:
            raise runner.TwoVariantError("lost reply")
        return {"id": "stable-from-normal-api", "revision": 1}


def test_receipt_replay_uses_durable_response_and_same_identity(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    receipt = runner.Receipt(path, "tessera-lab-two-variant-e2e-test", "nonce1234")
    transport = _Transport()
    first = receipt.call(transport, "task", "POST", "/projects/p/tasks", {"title": "cpu"})
    transport.fail_after_commit = True
    replay = runner.Receipt(path, "tessera-lab-two-variant-e2e-test", "nonce1234")
    assert replay.call(transport, "task", "POST", "/projects/p/tasks", {"title": "cpu"}) == first
    assert len(transport.calls) == 1
    assert transport.calls[0][-1] == "two-variant-nonce1234-task"
    assert path.stat().st_mode & 0o077 == 0
    with pytest.raises(runner.TwoVariantError, match="identity"):
        runner.Receipt(path, "tessera-lab-two-variant-e2e-other", "nonce1234")
    with pytest.raises(runner.TwoVariantError, match="changed"):
        replay.record("task", {"id": "replacement"})


def test_unknown_response_retries_same_idempotency_key_not_new_authority(tmp_path: Path) -> None:
    receipt = runner.Receipt(
        tmp_path / "receipt.json", "tessera-lab-two-variant-e2e-test", "nonce1234"
    )
    transport = _Transport()
    transport.fail_after_commit = True
    with pytest.raises(runner.TwoVariantError, match="lost reply"):
        receipt.call(transport, "grant", "POST", "/projects/p/execution-grants", {"task_id": "t"})
    assert "grant" not in json.loads(receipt.path.read_text())["steps"]
    transport.fail_after_commit = False
    assert (
        receipt.call(transport, "grant", "POST", "/projects/p/execution-grants", {"task_id": "t"})[
            "id"
        ]
        == "stable-from-normal-api"
    )
    assert len(transport.calls) == 2
    assert transport.calls[0] == transport.calls[1]


def _lease(now):
    return {
        "id": "lease_test",
        "epoch": 2,
        "expires_at": (now + timedelta(seconds=30)).isoformat(),
        "hard_expires_at": (now + timedelta(minutes=3)).isoformat(),
    }


class _Renewals:
    def __init__(self, lease, now):
        self.lease, self.now = lease, now
        self.calls = []
        self.responses = {}
        self.fail = False

    def request(self, method, path, body, *, key):
        assert method == "POST" and path == "/worker/leases/renew"
        assert body == {"lease_id": self.lease["id"], "expected_epoch": 2}
        self.calls.append(key)
        if self.fail:
            raise ConnectionError("control unavailable")
        if key not in self.responses:
            self.responses[key] = {
                **self.lease,
                "expires_at": (self.now[0] + timedelta(seconds=30)).isoformat(),
            }
        return self.responses[key]


def test_post_job_renewal_stays_with_original_lease_and_hard_limit(tmp_path):
    now = [datetime(2026, 10, 3, tzinfo=UTC)]
    lease = _lease(now[0])
    worker = _Renewals(lease, now)
    path = tmp_path / "post-job-lease.json"
    keeper = runner._PostJobLeaseKeeper(
        path, "owned-fixture", "nonce", lease, worker, clock=lambda: now[0]
    )
    keeper.renew()
    now[0] += timedelta(seconds=12)
    keeper.renew()
    assert len(set(worker.calls)) == 2
    assert all(key.startswith("two-variant-nonce-lease_test-renew-") for key in worker.calls)
    assert path.stat().st_mode & 0o077 == 0
    replay = runner._PostJobLeaseKeeper(
        path, "owned-fixture", "nonce", lease, worker, clock=lambda: now[0]
    )
    replay.renew()
    assert worker.calls[-1].endswith("renew-2")
    now[0] += timedelta(minutes=3)
    with pytest.raises(runner.TwoVariantError, match="expired"):
        replay.renew()
    assert len(worker.calls) == 3


def test_post_job_uncertain_renewal_replays_same_key_and_blocks_upload(tmp_path):
    now = [datetime(2026, 10, 3, tzinfo=UTC)]
    lease = _lease(now[0])
    worker = _Renewals(lease, now)
    worker.fail = True
    path = tmp_path / "post-job-lease.json"
    keeper = runner._PostJobLeaseKeeper(
        path, "owned-fixture", "nonce", lease, worker, clock=lambda: now[0]
    )
    with pytest.raises(ConnectionError):
        keeper.renew()
    assert "renew-0-intent" in json.loads(path.read_text())["steps"]
    worker.fail = False
    resumed = runner._PostJobLeaseKeeper(
        path, "owned-fixture", "nonce", lease, worker, clock=lambda: now[0]
    )
    resumed.renew()
    assert worker.calls == [worker.calls[0]] * 2
    with pytest.raises(runner.TwoVariantError, match="changed"):
        runner._PostJobLeaseKeeper(path, "owned-fixture", "nonce", {**lease, "epoch": 3}, worker)


def test_post_job_lost_renewal_reply_replays_original_committed_effect(tmp_path):
    now = [datetime(2026, 10, 3, tzinfo=UTC)]
    lease = _lease(now[0])
    worker = _Renewals(lease, now)
    original_request = worker.request
    lost = [True]

    def request(*args, **kwargs):
        result = original_request(*args, **kwargs)
        if lost[0]:
            lost[0] = False
            raise ConnectionError("reply lost after renewal committed")
        return result

    worker.request = request
    path = tmp_path / "post-job-lease.json"
    with pytest.raises(runner.TwoVariantError, match="renewal failed"):
        with runner._PostJobLeaseKeeper(
            path, "owned-fixture", "nonce", lease, worker, clock=lambda: now[0]
        ):
            pytest.fail("collection must not start without acknowledged renewal")
    with runner._PostJobLeaseKeeper(
        path, "owned-fixture", "nonce", lease, worker, clock=lambda: now[0]
    ) as resumed:
        resumed.check()
    assert worker.calls == [worker.calls[0]] * 2
    assert len(worker.responses) == 1


def test_blocking_collector_wait_is_renewed_and_failure_fences_next_upload(tmp_path):
    now = [datetime(2026, 10, 3, tzinfo=UTC)]
    lease = _lease(now[0])
    worker = _Renewals(lease, now)
    second = threading.Event()
    original_request = worker.request

    def request(*args, **kwargs):
        if worker.calls:
            worker.fail = True
            second.set()
        return original_request(*args, **kwargs)

    worker.request = request
    keeper = runner._PostJobLeaseKeeper(
        tmp_path / "post-job-lease.json",
        "owned-fixture",
        "nonce",
        lease,
        worker,
        clock=lambda: now[0],
        interval=0.01,
    )
    with keeper:
        assert second.wait(timeout=2)  # Represents a blocking collector wait.
        deadline = time.monotonic() + 2
        while keeper.failure is None and time.monotonic() < deadline:
            time.sleep(0.001)
        with pytest.raises(runner.TwoVariantError, match="do not upload"):
            keeper.check()  # Guard before artifact/placement/manifest POST.
    assert len(worker.calls) == 2


def test_worker_supervision_deadline_is_bounded_to_issued_lease(tmp_path):
    now = datetime(2026, 10, 3, tzinfo=UTC)
    lease = _lease(now)
    path = tmp_path / "kubernetes-claims.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE dispatch_supervision(dispatch_id TEXT,lease_expires_at TEXT)")
        db.execute(
            "INSERT INTO dispatch_supervision VALUES (?,?)",
            ("dispatch-1", (now + timedelta(seconds=48)).isoformat()),
        )
    assert (
        runner._worker_lease_deadline(path, "dispatch-1", lease)["expires_at"]
        == (now + timedelta(seconds=48)).isoformat()
    )
    with pytest.raises(runner.TwoVariantError, match="no original"):
        runner._worker_lease_deadline(path, "dispatch-2", lease)
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE dispatch_supervision SET lease_expires_at=?",
            ((now + timedelta(minutes=4)).isoformat(),),
        )
    with pytest.raises(runner.TwoVariantError, match="exceeds"):
        runner._worker_lease_deadline(path, "dispatch-1", lease)


def test_prebootstrap_receipt_can_retry_only_before_namespace_effects(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    receipt = runner.Receipt(path, "tessera-lab-two-variant-e2e-test", "nonce1234")
    receipt.record("context", {"name": "docker-desktop"})
    receipt.record("prebuilt-control-image", {"image_id": "sha256:" + "a" * 64})
    # A failed image build did not create a namespace; replay must keep its nonce.
    assert runner._bootstrap_nonce(path, False) == "nonce1234"
    with pytest.raises(runner.TwoVariantError, match="partial bootstrap"):
        runner._bootstrap_nonce(path, True)
    receipt.record("namespace", {"uid": "uid-previously-observed"})
    assert runner._bootstrap_nonce(path, True) == "nonce1234"
    with pytest.raises(runner.TwoVariantError, match="missing fixture namespace"):
        runner._bootstrap_nonce(path, False)


def test_prebuilt_control_image_fences_id_and_input_revision(monkeypatch, tmp_path):
    restart = runner._load_script(
        "two_variant_prebuilt_restart", "check_kubernetes_control_plane_restart.py"
    )
    calls = []

    def command(argv, **_kwargs):
        calls.append(argv)
        assert argv[:3] == ["docker", "image", "inspect"]
        return json.dumps([{"Id": "sha256:" + "b" * 64, "Os": "linux", "Architecture": "amd64"}])

    monkeypatch.setattr(restart, "_run", command)
    with pytest.raises(restart.FixtureError, match="did not load"):
        restart._load_image(tmp_path, "nonce1234", prebuilt_image_id="sha256:" + "a" * 64)
    assert len(calls) == 1  # Do not build or import an unexpected host image.
    for name in ("Dockerfile", "pyproject.toml", "uv.lock", "README.md"):
        (tmp_path / name).write_text(name)
    source = tmp_path / "src" / "control.py"
    source.parent.mkdir()
    source.write_text("original")
    first = runner._control_build_sources(tmp_path)
    source.write_text("changed")
    assert runner._control_build_sources(tmp_path) != first


def test_fixture_control_manifest_has_both_concrete_host_arguments() -> None:
    restart = runner._load_script(
        "two_variant_test_restart", "check_kubernetes_control_plane_restart.py"
    )
    documents = runner._fixture_manifest(
        restart, "tessera-lab-two-variant-e2e-test", "nonce1234", "pinned-image"
    )
    config = next(doc for doc in documents if doc["kind"] == "ConfigMap")
    deployment = next(doc for doc in documents if doc["kind"] == "Deployment")
    assert config["data"]["TESSERA_TRUSTED_HOST"] == "localhost"
    assert config["data"]["TESSERA_TLS_GATEWAY_HOST"] == "localhost"
    assert deployment["spec"]["template"]["metadata"]["annotations"][
        "tessera.io/fixture-control-config"
    ]


def test_network_proof_replay_does_not_refresh_timestamp_or_change_pod_uid(tmp_path: Path) -> None:
    receipt = runner.Receipt(
        tmp_path / "receipt.json", "tessera-lab-two-variant-e2e-test", "nonce1234"
    )
    proof = {"observed_at": "2026-10-03T18:40:00+00:00", "allowed_uid": "first"}
    assert runner._retain_network_proof(receipt, proof).isoformat() == proof["observed_at"]
    replay = runner.Receipt(receipt.path, "tessera-lab-two-variant-e2e-test", "nonce1234")
    assert (
        runner._retain_network_proof(
            replay, {**proof, "observed_at": "2026-10-03T18:55:00+00:00"}
        ).isoformat()
        == proof["observed_at"]
    )
    with pytest.raises(runner.TwoVariantError, match="changed"):
        runner._retain_network_proof(replay, {**proof, "allowed_uid": "replacement"})


def test_cancelled_dispatch_journal_refuses_repeat_grant(tmp_path: Path) -> None:
    path = tmp_path / "baseline-jobs.sqlite3"
    runner._require_no_cancelled_variant(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE kubernetes_execution_bindings (cancellation_requested INTEGER)"
        )
        connection.execute("INSERT INTO kubernetes_execution_bindings VALUES (1)")
    with pytest.raises(runner.TwoVariantError, match="do not replay"):
        runner._require_no_cancelled_variant(path)


def test_wait_uses_exact_variant_claim_uid_and_spec() -> None:
    namespace = "tessera-lab-two-variant-e2e-test"
    claim = runner.docker._pvc(namespace)
    claim["metadata"].update(name="outputs-baseline", uid="claim-uid")
    claim["status"] = {"phase": "Bound"}

    class Live:
        def _get(self, kind, name, selected_namespace):
            assert (kind, name, selected_namespace) == ("pvc", "outputs-baseline", namespace)
            return claim

    digest = runner.docker._pvc_spec_sha256(claim)
    runner._wait_for_variant_pvc(Live(), namespace, "outputs-baseline", "claim-uid", digest, 1)
    with pytest.raises(runner.TwoVariantError, match="identity changed"):
        runner._wait_for_variant_pvc(Live(), namespace, "outputs-baseline", "other", digest, 1)


def test_resumed_terminal_job_uses_intact_original_journal(tmp_path: Path) -> None:
    cluster = runner.contracts.KubernetesClusterConfig(
        kubeconfig="/private/fixture/kubeconfig",
        kubeconfig_sha256="a" * 64,
        context="docker-desktop",
        profile="development-minikube-opencode-v1",
        server="https://127.0.0.1:8443",
        server_sha256="b" * 64,
        ca_sha256="c" * 64,
        namespace="tessera-lab-two-variant-e2e-test",
        cluster_uid="cluster-uid",
        namespace_uid="namespace-uid",
    )
    binding = runner.contracts.KubernetesJobBinding(
        operation_id="a" * 32,
        attempt_id="attempt-test",
        lease_id="lease-test",
        epoch=1,
        package_sha256="d" * 64,
        image="docker.io/library/hello@sha256:" + "e" * 64,
        command=("/hello",),
        active_deadline_seconds=120,
        output_pvc="outputs-baseline",
        output_mount_path="/outputs",
        output_pvc_uid="claim-uid",
        output_pvc_spec_sha256="f" * 64,
    )
    path = tmp_path / "baseline-jobs.sqlite3"
    intent = {
        "binding": binding.__dict__,
        "package_digest": binding.package_sha256,
        "target_digest": runner.contracts.cluster_identity_digest(cluster),
    }
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE kubernetes_execution_bindings (operation_id TEXT,intent_json TEXT,"
            "namespace TEXT,namespace_uid TEXT,job_name TEXT,pvc_name TEXT,pvc_uid TEXT,"
            "pvc_spec_sha256 TEXT,job_uid TEXT,cancellation_requested INTEGER,observed_state TEXT)"
        )
        db.execute(
            "INSERT INTO kubernetes_execution_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                binding.operation_id,
                json.dumps(intent),
                cluster.namespace,
                cluster.namespace_uid,
                binding.job_name,
                binding.output_pvc,
                binding.output_pvc_uid,
                binding.output_pvc_spec_sha256,
                "job-uid",
                0,
                "succeeded",
            ),
        )
        db.execute(
            "CREATE TABLE kubernetes_jobs (operation_id TEXT,binding_json TEXT,job_uid TEXT,"
            "create_intent INTEGER,cancellation_requested INTEGER)"
        )
        db.execute(
            "INSERT INTO kubernetes_jobs VALUES (?,?,?,?,?)",
            (
                binding.operation_id,
                runner.contracts.canonical_json(binding.__dict__).decode(),
                "job-uid",
                1,
                0,
            ),
        )
    dispatch = {"operation_id": binding.operation_id, "package_digest": binding.package_sha256}
    recorded = {"name": binding.job_name, "uid": "job-uid"}
    assert runner._resume_terminal_binding(path, dispatch, recorded, cluster) == binding
    with sqlite3.connect(path) as db:
        db.execute("UPDATE kubernetes_jobs SET job_uid='replacement'")
    with pytest.raises(runner.TwoVariantError, match="conflicts"):
        runner._resume_terminal_binding(path, dispatch, recorded, cluster)


def test_serial_grants_stop_on_first_failure_and_do_not_issue_intervention(tmp_path: Path) -> None:
    receipt = runner.Receipt(
        tmp_path / "receipt.json", "tessera-lab-two-variant-e2e-test", "nonce1234"
    )
    seen = []

    def one(kind):
        seen.append(kind)
        return {"grant_status": "active", "sum_squares": {"number": 385}, "sha256": "a"}

    with pytest.raises(runner.TwoVariantError, match="unresolved grant"):
        runner._serial_variants(one, receipt)
    assert seen == ["baseline"]
    assert "intervention-expiry" not in json.loads(receipt.path.read_text())["steps"]


def test_serial_grants_reject_duplicate_or_mismatched_output(tmp_path: Path) -> None:
    receipt = runner.Receipt(
        tmp_path / "receipt.json", "tessera-lab-two-variant-e2e-test", "nonce1234"
    )
    seen = []

    def one(kind):
        seen.append(kind)
        return {
            "grant_status": "reconciled",
            "sum_squares": {"number": runner._expected(kind)},
            "sha256": "a",
        }

    with pytest.raises(runner.TwoVariantError, match="digests"):
        runner._serial_variants(one, receipt)
    assert seen == ["baseline", "intervention"]


def test_cli_fences_missing_run_and_unsafe_fixture_identity(monkeypatch, tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("synthetic-only")
    token.chmod(0o600)
    base = [
        "fixture",
        "--kubeconfig",
        str(tmp_path / "kubeconfig"),
        "--worker-token-file",
        str(token),
        "--namespace",
        "tessera-lab-two-variant-e2e-test",
    ]
    monkeypatch.setattr(sys, "argv", base)
    with pytest.raises(SystemExit, match="2"):
        runner.main()
    monkeypatch.setattr(sys, "argv", base + ["--run", "--timeout", "999"])
    with pytest.raises(SystemExit, match="2"):
        runner.main()
    token.chmod(0o644)
    monkeypatch.setattr(sys, "argv", base + ["--run"])
    with pytest.raises(SystemExit, match="2"):
        runner.main()
