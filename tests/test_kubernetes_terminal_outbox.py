"""Isolated ordinary-service terminal/report recovery, not live acceptance."""

import json
import sqlite3
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from test_kubernetes_dispatch import _ApiChannel, _dispatch, _service
from test_kubernetes_execution import _admitted_package, _cluster, _FakeKubernetes

from tessera_control.api import create_app
from tessera_worker.control_channel import ControlChannelError
from tessera_worker.kubernetes_contracts import KubernetesProviderIdentity, cluster_identity_digest
from tessera_worker.kubernetes_dispatch_service import KubernetesClaimIntentJournal


@pytest.mark.parametrize("denial", [401, 409, None])
@pytest.mark.parametrize("retained_custody", [False, True])
def test_resolved_legacy_running_outbox_recovers_and_queues_terminal_before_replay(
    tmp_path, denial, retained_custody
):
    response, _, package = _admitted_package(tmp_path)
    state, cluster = tmp_path / "state", _cluster()
    client, fake = TestClient(create_app(state)), _FakeKubernetes(package, cluster)
    dispatch = _dispatch(client, state, response, cluster_identity_digest(cluster))
    service = _service(tmp_path, client, dispatch, fake, cluster, next_dispatch=True)
    service.run(max_cycles=1)
    journal = KubernetesClaimIntentJournal(tmp_path / "worker", dispatch["id"])
    running = journal.report(
        package.operation_id,
        {
            "lease_id": package.package["lease_id"],
            "expected_epoch": package.binding.epoch,
            "operation_id": package.operation_id,
            "observed_state": "running",
            "termination_confirmed": False,
            "observed_at": service._clock_now().isoformat(),
            "provider_id": KubernetesProviderIdentity(
                cluster.cluster_uid, cluster.namespace_uid, "job-fixture"
            ).provider_id,
        },
    )
    with sqlite3.connect(journal.path) as db:
        db.execute("UPDATE dispatch_descriptors SET resolved=1")
    assert dispatch["id"] in journal.unfinished_dispatch_ids()
    fake.jobs[package.binding.job_name]["status"] = {"succeeded": 1}
    template = fake.jobs[package.binding.job_name]["spec"]["template"]
    pod = fake.pod("Succeeded")
    pod["metadata"] = deepcopy(template["metadata"]) | {
        "namespace": cluster.namespace,
        "uid": "pod-fixture",
        "ownerReferences": [{"kind": "Job", "uid": "job-fixture", "controller": True}],
    }
    pod["spec"] = deepcopy(template["spec"])
    fake.pods = [pod]

    class Denied(_ApiChannel):
        def post(self, path, body, key):
            if path == "/worker/observations":
                if denial is None:
                    raise ConnectionError("isolated delivery interruption")
                raise ControlChannelError("isolated denial", denial)
            return super().post(path, body, key)

    recovering = _service(
        tmp_path, client, dispatch, fake, cluster, channel=Denied(client), next_dispatch=True
    )
    if retained_custody:
        # Synthetic completed custody; isolated provider/API lifecycle remains real code.
        recovering.deliveries.pending = lambda _: True
        result = recovering._result
        recovering._result = lambda dispatch, package, status, *args, **kwargs: (
            {"output_delivery": "completed"}
            if status == "succeeded"
            else result(dispatch, package, status, *args, **kwargs)
        )
    recovering.run(max_cycles=1)
    terminal = journal.pending_terminal_report(package.operation_id)
    assert terminal is not None and terminal[0]["termination_confirmed"] is True
    assert journal.pending_report(package.operation_id) == running
    assert journal.resolve_descriptor() is False
    with sqlite3.connect(journal.path) as db:
        before = db.execute(
            "SELECT sequence,payload_json,key,completed FROM dispatch_reports ORDER BY sequence"
        ).fetchall()
    again = _service(
        tmp_path, client, dispatch, fake, cluster, channel=Denied(client), next_dispatch=True
    ).run(max_cycles=1)
    assert again["status"] == "report_draining"
    with sqlite3.connect(journal.path) as db:
        assert (
            db.execute(
                "SELECT sequence,payload_json,key,completed FROM dispatch_reports ORDER BY sequence"
            ).fetchall()
            == before
        )
    recovered = _service(tmp_path, client, dispatch, fake, cluster, next_dispatch=True).run(
        max_cycles=1
    )
    assert recovered["status"] == "report_delivered"
    assert journal.pending_report(package.operation_id) is None
    assert dispatch["id"] not in journal.unfinished_dispatch_ids()
    assert fake.creates == 1


def test_custody_alone_or_unconfirmed_exit_cannot_resolve_descriptor(tmp_path):
    journal = KubernetesClaimIntentJournal(tmp_path, "dispatch-fixture")
    with sqlite3.connect(journal.path) as db:
        db.execute(
            "INSERT INTO dispatch_descriptors VALUES (?,?,0)",
            (journal.dispatch_id, json.dumps({"fixture": "retained custody"})),
        )
    payload, _, seq = journal.report(
        "operation-fixture",
        {
            "observed_state": "exited",
            "termination_confirmed": False,
        },
    )
    journal.complete_report("operation-fixture", seq)
    assert journal.resolve_descriptor() is False
    assert journal.unfinished_dispatch_ids() == (journal.dispatch_id,)
    terminal = journal.report("operation-fixture", payload | {"termination_confirmed": True})
    assert journal.resolve_descriptor() is False
    journal.complete_report("operation-fixture", terminal[2])
    assert journal.resolve_descriptor() is True


def test_installed_report_draining_loop_is_paced(tmp_path, monkeypatch, capsys):
    from tessera_worker import kubernetes_remote_service as remote

    sleeps = []
    monkeypatch.setattr(remote, "configure", lambda *_: (object(), object(), object()))
    monkeypatch.setattr(remote.ssl, "create_default_context", lambda **_: object())
    monkeypatch.setattr(remote, "read_private_credential", lambda _: "fixture-credential")
    monkeypatch.setattr(remote.time, "sleep", sleeps.append)

    class Service:
        calls = 0

        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, **_kwargs):
            self.calls += 1
            if self.calls > 1:
                raise ValueError("bounded fixture stop")
            return {"status": "report_draining", "reconciliation_required": True}

    monkeypatch.setattr(remote, "KubernetesDispatchWorkerService", Service)
    flags = [
        "--target-file",
        "--ca-file",
        "--kubernetes-token-file",
        "--credential-file",
        "--control-ca-file",
    ]
    argv = [arg for flag in flags for arg in (flag, str(tmp_path / flag[2:]))]
    argv += [
        "--control-url",
        "https://control.example.test",
        "--workspace",
        str(tmp_path / "worker"),
    ]
    assert remote.main(argv) == 1
    assert sleeps == [5]
    assert json.loads(capsys.readouterr().out)["status"] == "report_draining"
