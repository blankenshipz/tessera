"""Actual temporary ASGI HTTP/control SQLite; no real Docker or credentials."""

import io
import json
from urllib.error import HTTPError

import pytest
from test_agent_authority import post
from test_ordinary_docker_control import docker as control_fixture  # noqa: F401

from tessera_worker.control_channel import ControlChannelError
from tessera_worker.ordinary_docker_contract import DockerPackage
from tessera_worker.ordinary_docker_http import (
    DockerFence,
    DockerWireReport,
    NativeDockerHttpClient,
)


@pytest.fixture
def native(request):
    parts = request.getfixturevalue("control_fixture")
    _, client, owner, worker, _, target, _, _, _, receipt, _, _ = parts
    d = {"client": client, "owner": owner, "target": target, "state": parts[0]}
    package = DockerPackage.model_validate_json(json.dumps(receipt["package"]))

    def opener(req, timeout):
        assert 0 < timeout <= 2
        response = client.request(
            req.method, req.full_url, content=req.data, headers=dict(req.header_items())
        )
        if response.status_code >= 400:
            raise HTTPError(req.full_url, response.status_code, "rejected", {}, None)
        value = io.BytesIO(response.content)
        value.getcode = lambda: response.status_code
        value.headers = response.headers
        return value

    api = NativeDockerHttpClient(
        "http://127.0.0.1", "worker-token", allow_loopback_http=True, opener=opener
    )
    return d, package, api, opener


def test_native_poll_claim_authority_report_replay(native):
    d, package, api, _ = native
    rows = api.poll(package.target_digest).dispatches
    assert len(rows) == 1 and rows[0].package == package
    assert rows[0].state == "queued"
    first = api.claim(package, "stable-http-claim")
    assert api.claim(package, "stable-http-claim") == first
    assert api.current_authority(package).id == package.lease_id
    value = DockerWireReport(
        **DockerFence.from_package(package).model_dump(),
        container_id="c" * 64,
        state="running",
        termination_confirmed=False,
        stop_requested=False,
    )
    first_report = api.report(value, "stable-http-report")
    assert api.report(value, "stable-http-report") == first_report
    assert first_report.scientific_validity == "not_assessed"


def test_wrong_credential_denied_and_error_sanitized(native):
    _, package, _, opener = native
    bad = NativeDockerHttpClient(
        "http://127.0.0.1", "synthetic-wrong-token", allow_loopback_http=True, opener=opener
    )
    with pytest.raises(ControlChannelError) as error:
        bad.poll(package.target_digest)
    assert error.value.status in {401, 403}
    assert "synthetic-wrong-token" not in str(error.value)


def test_revoked_target_denies_authority_but_accepts_actual_stop_receipt(native):
    d, package, api, _ = native
    api.claim(package, "claim-revoke")
    response = post(
        d["client"],
        d["owner"],
        f"/docker-targets/{d['target']['id']}/revoke",
        {"expected_revision": 1},
        key="http-revoke",
        expected=200,
    )
    assert response["revoked"] is True
    with pytest.raises(ControlChannelError):
        api.current_authority(package)
    assert api.poll(package.target_digest).dispatches == ()
    value = DockerWireReport(
        **DockerFence.from_package(package).model_dump(),
        container_id="c" * 64,
        state="cancelled",
        termination_confirmed=True,
        stop_requested=True,
    )
    assert api.report(value, "stop-after-revoke").state == "cancelled"


def test_no_implicit_loopback_or_nonloopback_http():
    with pytest.raises(ControlChannelError, match="development opt-in"):
        NativeDockerHttpClient("http://127.0.0.1", "synthetic")
    with pytest.raises(ValueError):
        NativeDockerHttpClient("http://remote.example", "synthetic", allow_loopback_http=True)


def test_redirect_denied_without_second_request():
    from tessera_worker.control_channel import _RejectRedirects

    with pytest.raises(ControlChannelError, match="redirect denied"):
        _RejectRedirects().redirect_request(None, None, 302, "", {}, "https://other.example")


@pytest.fixture
def bound(native, tmp_path):
    from pathlib import Path

    from test_ordinary_docker_execution import FakeDocker

    from tessera_worker.ordinary_docker_backend import HttpDockerWorkerBackend
    from tessera_worker.ordinary_docker_contract import DockerTarget
    from tessera_worker.ordinary_docker_execution import OrdinaryDockerExecutionAdapter
    from tessera_worker.ordinary_docker_service import (
        DockerWorkerConfig,
        OrdinaryDockerWorkerService,
    )

    d, package, api, opener = native
    target = DockerTarget.model_validate(d["target"]["bounds"]["target"])
    transport = FakeDocker(target)
    workspace = tmp_path / "worker"
    config = DockerWorkerConfig(
        package.worker_principal_id,
        "https://control.example",
        Path("/private/synthetic-reference"),
        Path("/private/synthetic-ca"),
        workspace,
        target,
    )
    services = []

    def create():
        service = OrdinaryDockerWorkerService(
            config,
            None,
            adapter_factory=lambda claim, path, authority: OrdinaryDockerExecutionAdapter(
                path, target, transport, current_authority=authority
            ),
        )
        service.backend = HttpDockerWorkerBackend(
            api, workspace / "http.sqlite3", package.worker_principal_id
        )
        services.append(service)
        return service

    yield d, package, api, transport, create
    for service in services:
        service.close()


def test_actual_backend_service_submit_report_restart_no_duplicate(bound):
    d, package, api, transport, create = bound
    service = create()
    assert service.step().status == "running"
    service.close()
    result = create().step()
    assert result.status == "running" and result.container_id == "c" * 64
    assert transport.creates == transport.starts == 1
    assert api.readback(package).container_id == "c" * 64


def test_actual_backend_revocation_stop_only_no_renew(bound):
    d, package, api, transport, create = bound
    service = create()
    assert service.step().status == "running"
    post(
        d["client"],
        d["owner"],
        f"/docker-targets/{d['target']['id']}/revoke",
        {"expected_revision": 1},
        key="service-revoke",
        expected=200,
    )
    assert api.readback(package).authorization == "stop_only"
    result = service.step()
    assert result.status == "stopped" and result.termination_confirmed
    assert transport.creates == transport.starts == 1
    assert api.readback(package).latest_report.state == "cancelled"


def test_actual_backend_output_custody(bound):
    from test_ordinary_docker_execution import tar

    d, package, api, transport, create = bound
    service = create()
    assert service.step().status == "running"
    transport.output = tar([("outputs/" + package.output_paths[0], b"synthetic result\n")])
    transport.ready = True
    result = service.step()
    assert result.status == "completed" and result.control_acknowledged
    assert api.readback(package).latest_report.state == "succeeded"


def test_lost_report_ack_readback_recovers_exact_key_after_restart(bound):
    d, package, api, transport, create = bound
    service = create()
    original = api.report

    def lost(value, key):
        original(value, key)
        raise ControlChannelError("synthetic lost reply after actual commit")

    api.report = lost
    assert service.step().status == "running"
    assert service._pending(package.operation_id) == 1
    service.close()
    api.report = original
    restarted = create()
    assert restarted.step().status == "running"
    assert restarted._pending(package.operation_id) == 0
    assert transport.starts == transport.creates == 1


def test_unknown_claim_reply_restarts_original_claim_without_replacement(bound):
    d, package, api, transport, create = bound
    service = create()
    original = api.claim

    def lost(package, key):
        original(package, key)
        raise ControlChannelError("synthetic lost claim reply after actual commit")

    api.claim = lost
    assert service.step().status == "recovery_required"
    assert transport.creates == 0
    service.close()
    api.claim = original
    assert create().step().status == "running"
    assert transport.starts == transport.creates == 1


@pytest.mark.parametrize("failure", ["lease_expired", "credential_expired"])
def test_expiry_stops_owned_work_without_renewal(bound, failure):
    import sqlite3

    d, package, api, transport, create = bound
    service = create()
    assert service.step().status == "running"
    with sqlite3.connect(d["state"] / "tessera.sqlite3") as db:
        if failure == "lease_expired":
            db.execute("UPDATE execution_leases SET expires_at='2000-01-01T00:00:00Z'")
        else:
            db.execute(
                "UPDATE credentials SET expires_at='2000-01-01T00:00:00Z' WHERE kind='worker'"
            )
    renewals = []
    service.backend.renew = lambda *args, **kwargs: renewals.append(args)
    result = service.step()
    assert result.termination_confirmed
    assert transport.signals[-1] == ("c" * 64, "SIGKILL")
    assert renewals == [] and transport.starts == 1
    if failure == "credential_expired":
        with pytest.raises(ControlChannelError) as error:
            api.readback(package)
        assert error.value.status == 401
        assert not result.control_acknowledged


def test_actual_readback_container_mismatch_and_owner_denied(bound):
    d, package, api, transport, create = bound
    service = create()
    service.step()
    with pytest.raises(ControlChannelError):
        api.readback(package, container_id="d" * 64)
    response = d["client"].post(
        "/worker/docker/readback",
        headers=d["owner"],
        json=DockerFence.from_package(package).model_dump(mode="json"),
    )
    assert response.status_code == 401


def test_unknown_start_reply_is_observed_only_on_restart(bound):
    d, package, api, transport, create = bound
    transport.lose_start = True
    service = create()
    service.step()
    service.close()
    assert create().step().status == "running"
    assert transport.starts == transport.creates == 1


def test_actual_lease_renewal_maps_native_pins(bound):
    d, package, api, transport, create = bound
    service = create()
    service.step()
    claim = service.backend.claim(
        package.worker_principal_id, transport.target, "second-local-claim-key", timeout_seconds=2
    )
    lease = service.backend.renew(claim, "native-renew", timeout_seconds=2)
    assert lease.id == package.lease_id and lease.epoch == package.epoch


def test_lost_output_reply_replays_custody_without_duplicate_artifacts(bound):
    import sqlite3

    from test_ordinary_docker_execution import tar

    d, package, api, transport, create = bound
    service = create()
    service.step()
    transport.output = tar([("outputs/" + package.output_paths[0], b"synthetic result\n")])
    transport.ready = True
    real_output = service.backend.output

    def lost(*args, **kwargs):
        real_output(*args, **kwargs)
        raise ControlChannelError("synthetic lost actual custody reply")

    service.backend.output = lost
    assert service.step().status == "report_draining"
    with sqlite3.connect(d["state"] / "tessera.sqlite3") as db:
        before = db.execute("SELECT count(*) FROM artifact_manifests").fetchone()[0]
    service.close()
    assert create().step().status == "completed"
    with sqlite3.connect(d["state"] / "tessera.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM artifact_manifests").fetchone()[0] == before
    assert transport.creates == transport.starts == 1


def test_readback_payload_bound_rejected_before_transport(native):
    _, package, api, _ = native
    with pytest.raises(ControlChannelError):
        api.readback(package, delivery_key="x" * 201)


def test_cli_run_constructs_actual_binding_without_auth_file_reads(bound, monkeypatch, capsys):
    import ssl

    import tessera_worker.ordinary_docker_http as http_module
    import tessera_worker.ordinary_docker_transport as transport_module
    import tessera_worker.ordinary_docker_worker as worker_module
    from tessera_worker.ordinary_docker_service import OrdinaryDockerWorkerService

    d, package, api, transport, create = bound
    service = create()
    config = service.config
    service.close()
    normal_tls = ssl.create_default_context()
    monkeypatch.setattr(worker_module, "_configuration", lambda _: config)
    monkeypatch.setattr(worker_module, "_credential", lambda _: "worker-token")
    monkeypatch.setattr(ssl, "create_default_context", lambda **_: normal_tls)
    monkeypatch.setattr(http_module, "NativeDockerHttpClient", lambda *a, **kw: api)
    monkeypatch.setattr(transport_module, "FixedDockerTransport", lambda *a, **kw: transport)
    original_run = OrdinaryDockerWorkerService.run
    monkeypatch.setattr(
        OrdinaryDockerWorkerService, "run", lambda self, **kw: original_run(self, max_cycles=1)
    )
    from test_ordinary_docker_service import cli_args

    assert (
        worker_module.main(cli_args("run")) == 2
    )  # bounded test return, ordinary run is continuous
    assert json.loads(capsys.readouterr().out)["status"] == "running"
    assert api.readback(package).container_id == "c" * 64
    assert transport.creates == transport.starts == 1
