"""Synthetic server admission and custody only; no Salad/credential/network effects."""

import base64
import hashlib
import json
import pickle
import socket
import sqlite3
import time
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from test_remote_execution_routing import command, dispatch, setup_route

from tessera_control.artifact_manifests import ArtifactManifestStore
from tessera_control.artifacts import ArtifactStore
from tessera_control.capabilities import CapabilityStore
from tessera_control.capability_models import CapabilityDeclaration
from tessera_control.remote_execution_models import RemoteProposal, SaladExecution
from tessera_control.salad_proxy import FakeSaladProxy, FakeSaladTransport, SaladProxy
from tessera_control.salad_proxy_cli import main
from tessera_control.salad_proxy_contract import (
    ManagementCredential,
    ProxyPolicy,
    ProxyRequest,
    safety_projection,
)
from tessera_control.salad_secrets import FakeSecretResolver, SecretBuffer
from tessera_control.store import Conflict, canonical_digest
from tessera_worker.remote_dispatch import RemoteDispatcher
from tessera_worker.salad_remote import SaladProxyFakeAdapter


def declaration():
    return CapabilityDeclaration(environment_id="fixture", image_digest="sha256:" + "a" * 64)


def profile():
    return dict(
        platform="linux/amd64",
        command="/tessera/bin/run",
        argv=["fixture"],
        env={},
        max_output_bytes=1024,
        max_output_files=1,
        capability_digest=canonical_digest(declaration().model_dump(mode="json")),
        network_policy="provider_broad_egress_explicitly_accepted",
        data_policy="public_released_only",
        artifact_destination="artifact-ref:quarantine",
        checkpoint_destination="checkpoint-ref:external",
        monitor_profile="a" * 64,
        watchdog_profile="b" * 64,
        max_cost_cents=0,
        credit_exposure="unknown_blocks_new_work_credit_not_hard_cap",
    )


def fixture(tmp_path, *, approve=True, capability=True, execution=None):
    h = setup_route(tmp_path, approve=approve, salad_execution=execution or profile())
    if capability:
        store = CapabilityStore(h.journey.state)
        task = store.get_task(h.journey.actor, h.route["task_id"])
        _, cap = store.request(
            h.journey.actor,
            task["id"],
            "proxy-cap",
            dict(
                expected_plan_revision=task["revision"] - 1,
                expected_plan_digest=h.body["plan_digest"],
                declaration=declaration().model_dump(mode="json"),
            ),
        )
        store.approve(
            h.journey.actor,
            cap["id"],
            "proxy-cap-approve",
            dict(
                expected_revision=1,
                declaration_digest=cap["declaration_digest"],
                expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            ),
        )
    h.proxy_policy = ProxyPolicy(
        org_id=h.worker["org_id"],
        project_id=h.route["project_id"],
        target=h.policy,
        management=ManagementCredential(reference="credential-ref:fixture", version=1),
    )
    h.resolver = FakeSecretResolver("credential-ref:fixture", now=time.time)
    h.transport = FakeSaladTransport()
    h.proxy = FakeSaladProxy(
        store=h.store,
        worker=h.worker,
        policy=h.proxy_policy,
        resolver=h.resolver,
        transport=h.transport,
    )
    h.adapter = SaladProxyFakeAdapter(h.proxy, h.route["id"])
    h.dispatcher = RemoteDispatcher(
        h.store, h.worker, {h.policy.selection.target_digest: h.adapter}
    )
    return h


def request(h, operation="create_stopped", *, action="create"):
    proposal = RemoteProposal.model_validate(h.route["proposal"])
    preflight = h.proxy.preflight(proposal, h.route["id"])
    intent = h.store.begin(
        h.worker, h.route["id"], action, preflight_digest=canonical_digest(preflight)
    )
    return ProxyRequest(
        route_id=h.route["id"], effect_id=intent["effects"][-1]["id"], operation=operation
    ), preflight


def response(h, **updates):
    return (
        dict(
            status=200,
            organization="fixture-org",
            project="fixture-project",
            group=h.policy.selection.salad.group,
            provider_id=h.body["operation_id"],
            allocation_id=None,
            state="stopped",
            logs=[],
            events=[],
            selection_digest=canonical_digest(h.policy.selection.model_dump(mode="json")),
        )
        | updates
    )


def test_live_gate_precedes_all_resolution_transport_and_parsing(monkeypatch):
    class Forbidden:
        def __getattr__(self, name):
            pytest.fail("disabled entry point accessed dependency")

    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network"))
    proxy = SaladProxy(resolver=Forbidden(), transport=Forbidden())
    for request_value in (
        None,
        {},
        {"api_key": "synthetic-not-a-key", "endpoint": "https://evil.invalid"},
    ):
        result = proxy.request(request_value)
        assert result.status == "blocked" and result.reason == "salad_live_transport_disabled"
        assert "synthetic-not-a-key" not in result.model_dump_json()


@pytest.mark.parametrize(
    "field,value",
    [
        ("endpoint", "https://evil.invalid"),
        ("live_enabled", True),
        ("trust_env", True),
        ("follow_redirects", True),
        ("retries", 1),
        ("proxy", "http://evil.invalid"),
        ("api_key", "synthetic-key"),
        ("gateway_key", "synthetic-gateway"),
    ],
)
def test_server_config_cannot_enable_or_override_endpoints(tmp_path, field, value):
    h = fixture(tmp_path)
    raw = h.proxy_policy.model_dump(mode="json") | {
        "management": h.proxy_policy.management.model_dump()
    }
    with pytest.raises(ValidationError):
        ProxyPolicy.model_validate(raw | {field: value})


@pytest.mark.parametrize(
    "value", ["env:SALAD_API_KEY", "prompt:secret", "request:key", "raw-key", "https://key.invalid"]
)
def test_credential_references_only(value):
    with pytest.raises(ValidationError):
        ManagementCredential(reference=value, version=1)
    with pytest.raises(ValidationError):
        ManagementCredential(reference="credential-ref:gateway", version=1, purpose="ai_gateway")


@pytest.mark.parametrize(
    "field,value",
    [
        ("command", "/bin/sh"),
        ("command", "ssh"),
        ("argv", ["-c", "curl https://evil.invalid"]),
        ("env", {"SALAD_API_KEY": "synthetic"}),
        ("env", {"HTTP_PROXY": "https://evil.invalid"}),
        ("env", {"PYTHONPATH": "/tmp"}),
        ("gateway", True),
        ("exec", True),
        ("ssh", True),
        ("platform", "linux/arm64"),
        ("max_output_bytes", 0),
        ("max_output_files", 129),
        ("artifact_destination", "https://evil.invalid"),
        ("data_policy", "private"),
        ("network_policy", "unrestricted"),
        ("monitor_profile", "unknown"),
    ],
)
def test_forbidden_workload_profile(field, value):
    with pytest.raises(ValidationError):
        SaladExecution.model_validate(profile() | {field: value})


@pytest.mark.parametrize(
    "operation",
    ["PATCH", "GET /", "scale", "autoscale", "ssh", "exec", "gateway", "queue", "create_project"],
)
def test_closed_operations(operation):
    with pytest.raises(ValidationError):
        ProxyRequest(route_id="route", effect_id="a" * 32, operation=operation)


@pytest.mark.parametrize(
    "field", ["path", "method", "url", "headers", "api_key", "account_key", "proxy", "env"]
)
def test_no_request_override_surface(field):
    with pytest.raises(ValidationError):
        ProxyRequest.model_validate(
            dict(route_id="route", effect_id="a" * 32, operation="read_group")
            | {field: "synthetic"}
        )


def test_fake_lifecycle_intent_nonce_audit_and_202_never_termination(tmp_path):
    h = fixture(tmp_path)
    assert dispatch(h, "create")["state"] == "stopped"
    assert dispatch(h, "start")["state"] == "running"
    command(h, "cancel")
    state = dispatch(h, "stop")
    assert state["cancellation"] == "pending" and not state["termination_confirmed"]
    assert state["cost_cents"] is None
    h.transport.state = "stopped"
    assert dispatch(h, "observe")["termination_confirmed"]
    assert dispatch(h, "delete")["cancellation"] == "pending"
    assert not h.route["termination_confirmed"]
    assert all(r["usage_status"] == "unknown" for r in h.route["receipts"])
    assert all(r["provenance"] == "synthetic" for r in h.route["receipts"])
    with sqlite3.connect(h.store.database_path) as db:
        claims = db.execute(
            "SELECT payload_json FROM events WHERE event_type='salad_proxy.claim'"
        ).fetchall()
    assert len(claims) == 5
    assert len({json.loads(c[0])["nonce"] for c in claims}) == 5
    assert all(json.loads(c[0])["package_digest"] == h.body["package_digest"] for c in claims)
    text = (
        json.dumps(h.store.get(h.journey.actor, h.route["id"]))
        + repr(h.transport.calls)
        + repr(h.resolver.buffers)
    )
    assert "synthetic-management-marker" not in text and "credential-ref" not in text
    assert all(not any(b._buffer) for b in h.resolver.buffers)


@pytest.mark.parametrize(
    "change",
    ["org", "project", "salad_org", "salad_project", "country", "image", "resources", "argv"],
)
def test_exact_allowlist_mismatch_precedes_resolver(tmp_path, change):
    h = fixture(tmp_path)
    req, preflight = request(h)
    raw = h.proxy_policy.model_dump(mode="json") | {
        "management": h.proxy_policy.management.model_dump()
    }
    if change in {"org", "project"}:
        raw[change + "_id"] = "other"
    elif change in {"salad_org", "salad_project", "country"}:
        raw["target"]["selection"]["salad"][
            {"salad_org": "organization", "salad_project": "project", "country": "country"}[change]
        ] = "CA" if change == "country" else "other"
    elif change == "image":
        raw["target"]["selection"]["manifests"]["image"] = "other@sha256:" + "0" * 64
    elif change == "resources":
        raw["target"]["selection"]["resources"]["disk_mb"] += 1
    else:
        raw["target"]["selection"]["salad"]["execution"]["argv"] = ["other"]
    proxy = FakeSaladProxy(
        store=h.store,
        worker=h.worker,
        policy=ProxyPolicy.model_validate(raw),
        resolver=h.resolver,
        transport=h.transport,
    )
    assert proxy.request_fake(req, preflight=preflight).reason == "admission_denied"
    assert h.resolver.calls == h.transport.calls == []


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE execution_leases SET expires_at='2000-01-01T00:00:00Z'",
        "UPDATE execution_grants SET epoch=epoch+1",
        "UPDATE capability_policies SET status='revoked'",
        "UPDATE worker_registry SET lifecycle_state='retired'",
    ],
)
def test_use_time_fencing_after_intent(tmp_path, sql):
    h = fixture(tmp_path)
    req, preflight = request(h)
    with sqlite3.connect(h.store.database_path) as db:
        db.execute(sql)
    assert h.proxy.request_fake(req, preflight=preflight).reason == "admission_denied"
    assert h.resolver.calls == h.transport.calls == []


def test_missing_approval_and_capability(tmp_path):
    h = fixture(tmp_path, approve=False, capability=False)
    with pytest.raises(Conflict):
        dispatch(h, "create")
    h.route = command(h, "approve")
    h.route = command(h, "start")
    assert dispatch(h, "create")["receipts"][-1]["proxy_reason"] == "admission_denied"
    assert h.resolver.calls == h.transport.calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("observed_at", "2000-01-01T00:00:00Z"),
        ("quota_ok", False),
        ("available", False),
        ("group_absent", False),
        ("route_digest", "0" * 64),
    ],
)
def test_preflight_stale_outage_quota_name_collision(tmp_path, field, value):
    h = fixture(tmp_path)
    req, preflight = request(h)
    assert (
        h.proxy.request_fake(req, preflight=preflight | {field: value}).reason == "admission_denied"
    )
    assert h.resolver.calls == h.transport.calls == []


@pytest.mark.parametrize("change", ["rotate", "revoke", "stale", "expired"])
def test_credential_version_revocation_freshness(tmp_path, change):
    h = fixture(tmp_path)
    req, preflight = request(h)
    if change in {"rotate", "revoke"}:
        getattr(h.resolver, change)()
    elif change == "stale":
        h.resolver.checked_at = time.time() - 10
    else:
        h.resolver.expires_at = time.time() - 1
    assert h.proxy.request_fake(req, preflight=preflight).reason == "credential_unavailable"
    assert h.resolver.calls == ["metadata"] and h.transport.calls == []


@pytest.mark.parametrize(
    "reply,reason",
    [
        (TimeoutError("synthetic-private-error"), "provider_unavailable"),
        (OSError("synthetic-private-error"), "provider_unavailable"),
        ({}, "malformed_response"),
        ({"status": 302}, "redirect_denied"),
        ({"status": 503}, "provider_unavailable"),
        ({"provider_id": "other"}, "identity_mismatch"),
        ({"organization": "other"}, "identity_mismatch"),
        ({"state": "invented"}, "malformed_response"),
        ({"logs": ["synthetic-management-marker-not-a-credential"]}, "malformed_response"),
    ],
)
def test_response_failures_zeroize_and_never_retry(tmp_path, reply, reason):
    h = fixture(tmp_path)
    h.transport.responses.append(
        reply if isinstance(reply, Exception) or not reply else response(h, **reply)
    )
    state = dispatch(h, "create")
    assert state["receipts"][-1]["proxy_reason"] == reason
    assert state["state"] == "unknown" and state["cost_cents"] is None
    assert not state["termination_confirmed"]
    assert all(not any(b._buffer) for b in h.resolver.buffers)
    with pytest.raises(Conflict):
        dispatch(h, "create")
    assert len(h.transport.calls) == 1
    assert "synthetic-private-error" not in json.dumps(state)


def test_same_group_reconciliation_after_lost_create_and_nonce_replay(tmp_path):
    h = fixture(tmp_path)
    req, preflight = request(h)
    h.transport.responses.append(TimeoutError())
    result = h.proxy.request_fake(req, preflight=preflight)
    assert result.status == "unknown"
    restarted = FakeSaladProxy(
        store=h.store,
        worker=h.worker,
        policy=h.proxy_policy,
        resolver=h.resolver,
        transport=h.transport,
    )
    assert restarted.request_fake(req, preflight=preflight).reason == "admission_denied"
    h.store.record(h.worker, h.route["id"], req.effect_id, result.observation)
    h.transport.responses.append(response(h))
    assert dispatch(h, "observe")["provider_id"] == h.body["operation_id"]
    assert [c.operation for c in h.transport.calls] == ["create_stopped", "read_group"]


def test_reallocation_and_log_event_hashes_are_not_custody(tmp_path):
    h = fixture(tmp_path)
    dispatch(h, "create")
    dispatch(h, "start")
    h.transport.responses.append(
        response(h, allocation_id="allocation-2", logs=["untrusted log"], events=["reallocated"])
    )
    state = dispatch(h, "observe")
    assert (
        state["reallocated"] and state["state"] == "unknown" and not state["termination_confirmed"]
    )
    receipt = state["receipts"][-1]
    assert receipt["proxy_reason"] == "reallocation_uncertain"
    assert [e["kind"] for e in receipt["evidence"]] == ["log", "event"]
    assert all(
        e["custody"] == "unverified" and e["manifest_id"] is None for e in receipt["evidence"]
    )
    assert "untrusted log" not in json.dumps(state)


def test_secret_buffer_serialization_and_exception_wipe():
    raw = bytearray(b"synthetic-marker")
    secret = SecretBuffer(raw)
    assert "synthetic" not in repr(secret)
    with pytest.raises(TypeError):
        pickle.dumps(secret)
    with pytest.raises(RuntimeError), secret:
        raise RuntimeError("fixture")
    assert raw == bytearray(len(raw))


def test_cli_no_network_no_resolver_and_safe_errors(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("network"))
    monkeypatch.setattr(FakeSecretResolver, "metadata", lambda *a: pytest.fail("resolution"))
    assert main([]) == 0
    assert json.loads(capsys.readouterr().out)["configuration"] == "not_read"
    path = tmp_path / "bad.json"
    path.write_text('{"api_key":"synthetic-private-marker"}')
    assert main(["--validate-config", str(path)]) == 2
    assert "synthetic-private-marker" not in capsys.readouterr().out


def test_api_projection_has_safety_not_credential_custody(tmp_path):
    h = fixture(tmp_path)
    result = h.journey.client.get(
        f"/remote-executions/{h.route['id']}", headers={"Authorization": h.journey.owner}
    )
    assert result.status_code == 200
    assert result.json()["proxy_safety"] == safety_projection()
    assert "credential-ref" not in result.text
    assert not result.json()["proxy_safety"]["isolation_witness"]


def test_source_review_supersession_and_cross_tenant_after_intent(tmp_path):
    h = fixture(tmp_path)
    req, preflight = request(h)
    wrong = FakeSaladProxy(
        store=h.store,
        worker=h.worker | {"org_id": "foreign"},
        policy=h.proxy_policy,
        resolver=h.resolver,
        transport=h.transport,
    )
    assert wrong.request_fake(req, preflight=preflight).reason == "admission_denied"
    h.journey.propose(h.journey.scope("source_use"))
    assert h.proxy.request_fake(req, preflight=preflight).reason == "admission_denied"
    assert h.resolver.calls == h.transport.calls == []


def test_revoked_execution_retains_stop_only_despite_quota_loss(tmp_path):
    h = fixture(tmp_path)
    dispatch(h, "create")
    dispatch(h, "start")
    command(h, "cancel")
    h.transport.quota_ok = False
    assert dispatch(h, "stop")["cancellation"] == "pending"
    assert h.transport.calls[-1].operation == "stop"


def test_rotation_requires_explicit_new_server_version_and_zeroizes_buffers(tmp_path):
    h = fixture(tmp_path)
    old = h.resolver.resolve("credential-ref:fixture", 1)
    h.resolver.rotate()
    assert not any(old._buffer)
    raw = h.proxy_policy.model_dump(mode="json") | {
        "management": {"reference": "credential-ref:fixture", "version": 2},
    }
    h.proxy = FakeSaladProxy(
        store=h.store,
        worker=h.worker,
        policy=ProxyPolicy.model_validate(raw),
        resolver=h.resolver,
        transport=h.transport,
    )
    req, preflight = request(h)
    assert h.proxy.request_fake(req, preflight=preflight).status == "observed"
    assert all(not any(b._buffer) for b in h.resolver.buffers)
    assert h.proxy.request(req).status == "blocked"


@pytest.mark.parametrize(
    "execution", [profile() | {"max_output_bytes": 2048}, profile() | {"max_cost_cents": 101}]
)
def test_limits_cannot_exceed_parent_package(tmp_path, execution):
    h = fixture(tmp_path, execution=execution)
    assert dispatch(h, "create")["receipts"][-1]["proxy_reason"] == "admission_denied"
    assert h.resolver.calls == h.transport.calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("replicas", 2),
        ("queues", True),
        ("autoscaling", True),
        ("interactive_shell", True),
        ("runtime_install", True),
    ],
)
def test_forbidden_salad_options(tmp_path, field, value):
    h = fixture(tmp_path)
    body = json.loads(json.dumps(h.body))
    body["selection"]["salad"][field] = value
    with pytest.raises(ValidationError):
        RemoteProposal.model_validate(body)
    body["selection"]["manifests"]["image"] = "python:latest"
    with pytest.raises(ValidationError):
        RemoteProposal.model_validate(body)


def test_cli_valid_configuration_does_not_print_reference(tmp_path, capsys):
    h = fixture(tmp_path)
    path = tmp_path / "proxy.json"
    path.write_text(
        json.dumps(
            h.proxy_policy.model_dump(mode="json")
            | {
                "management": h.proxy_policy.management.model_dump(),
            }
        )
    )
    assert main(["--validate-config", str(path)]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["configuration"] == "valid_offline_only"
    assert "credential-ref" not in output and h.resolver.calls == []


def test_api_rejects_secret_values_without_echoing_them(tmp_path):
    h = fixture(tmp_path)
    body = json.loads(json.dumps(h.body))
    body["selection"]["salad"]["execution"]["env"] = {"SALAD_API_KEY": "synthetic-private-marker"}
    result = h.journey.client.post(
        f"/tasks/{h.route['task_id']}/remote-executions",
        json=body,
        headers={"Authorization": h.journey.owner, "Idempotency-Key": "bad-key"},
    )
    assert result.status_code == 422 and "synthetic-private-marker" not in result.text
    assert h.resolver.calls == h.transport.calls == []


def test_artifact_custody_milestone_links_retained_bytes_not_provider_claim(tmp_path):
    h = fixture(tmp_path)
    dispatch(h, "create")
    content = b"synthetic proxy output"
    body = dict(
        expected_epoch=h.body["epoch"],
        operation_id=h.body["operation_id"],
        relative_path="result.txt",
        content_base64=base64.b64encode(content).decode(),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    _, artifact = ArtifactStore(h.journey.state).upload_artifact(
        h.worker,
        h.body["lease_id"],
        "proxy-output",
        canonical_digest(body),
        body,
    )
    body = dict(
        expected_epoch=h.body["epoch"],
        operation_id=h.body["operation_id"],
        artifacts=[{k: artifact[k] for k in ("id", "relative_path", "sha256", "size")}],
    )
    _, manifest = ArtifactManifestStore(h.journey.state).create_manifest(
        h.worker,
        h.body["lease_id"],
        "proxy-manifest",
        canonical_digest(body),
        body,
    )
    req, preflight = request(h, "read_group", action="observe")
    result = h.proxy.request_fake(req, preflight=preflight)
    observation = result.observation | {
        "evidence": [
            dict(
                kind="artifact",
                sha256=manifest["manifest_digest"],
                manifest_id=manifest["id"],
                custody="quarantined",
            )
        ]
    }
    state = h.store.record(h.worker, h.route["id"], req.effect_id, observation)
    assert state["receipts"][-1]["evidence"][0]["custody"] == "quarantined"
    assert state["receipts"][-1]["cost_cents"] is None
    assert h.store.record(h.worker, h.route["id"], req.effect_id, observation) == state


@pytest.mark.parametrize(
    "operation",
    [
        "read_project",
        "read_quota",
        "read_availability",
        "read_instance",
        "read_events",
        "read_logs",
    ],
)
def test_explicit_read_operations_cannot_confirm_group_termination(tmp_path, operation):
    h = fixture(tmp_path)
    dispatch(h, "create")
    req, preflight = request(h, operation, action="observe")
    result = h.proxy.request_fake(req, preflight=preflight)
    assert result.observation["state"] == "unknown"
    assert not result.observation["termination_confirmed"]
    assert h.transport.calls[-1].operation == operation
    assert all(not any(b._buffer) for b in h.resolver.buffers)


@pytest.mark.parametrize("change", ["lease", "version", "revocation", "preflight"])
def test_resolution_delay_cannot_outlive_admission(tmp_path, monkeypatch, change):
    h = fixture(tmp_path)
    req, preflight = request(h)
    resolve = h.resolver.resolve

    def delayed(*args):
        buffer = resolve(*args)
        if change == "lease":
            with sqlite3.connect(h.store.database_path) as db:
                db.execute("UPDATE execution_leases SET expires_at='2000-01-01T00:00:00Z'")
        elif change == "version":
            h.resolver.rotate()
        elif change == "revocation":
            h.resolver.revoke()
        else:
            preflight["observed_at"] = "2000-01-01T00:00:00Z"
        return buffer

    monkeypatch.setattr(h.resolver, "resolve", delayed)
    assert h.proxy.request_fake(req, preflight=preflight).status == "blocked"
    assert h.transport.calls == []
    assert all(not any(b._buffer) for b in h.resolver.buffers)
