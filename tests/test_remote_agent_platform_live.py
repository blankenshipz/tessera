"""Isolated harness safety checks; no protocol/live acceptance is claimed."""

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "remote_agent_platform_live", Path(__file__).parents[1] / "dev/remote_agent_platform_live.py"
)
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)


def manifest():
    steps = []
    for stage in live.STAGES:
        step = {
            "stage": stage,
            "via": live.MODES[stage],
            "assertions": [{"path": "body.ok", "value": True}],
        }
        if step["via"] == "mcp":
            step.update(
                tool="tessera_consume_grant",
                arguments={
                    "grant_id": "grant-1",
                    "idempotency_key": "consume-once",
                    "payload": {
                        "expected_revision": 1,
                        "expected_epoch": 1,
                        "model": "model",
                        "command": {"action": "attempt:status", "attempt_id": "attempt-1"},
                    },
                },
            )
        if stage == "planner_proposal":
            step["tool"] = "tessera_propose_research"
        if stage == "authority_request":
            step["tool"] = "tessera_request_authority"
        if stage == "joined_export":
            step["tool"] = "tessera_export_notebook"
        if step["via"] == "human":
            step.update(method="GET", path="/projects/project/records")
        if stage == "critic_task":
            step["request"] = {
                "message": {"messageId": "message-1", "role": "ROLE_USER", "parts": [{"data": {}}]}
            }
        if stage == "human_issue":
            step.update(
                method="POST",
                path="/projects/project/agent-authority-grants",
                idempotency_key="issue-once",
            )
        if stage == "human_revoke":
            step.update(
                method="POST",
                path="/agent-authority-grants/grant-1/revoke",
                idempotency_key="revoke-once",
            )
        if stage in {"denied_expansion", "expiry_fence", "revocation_fence"}:
            step["assertions"] = [{"path": "http_status", "value": 403}]
        if stage == "expiry_fence":
            step["wait_until"] = "2026-01-01T00:00:00+00:00"
        if stage == "restart_observed":
            step["assertions"] = [
                {
                    "path": "body.generation",
                    "op": "ne",
                    "value": {"$ref": "restart_baseline.body.generation"},
                }
            ]
        steps.append(step)
    return {
        "environment": "local_owned_docker",
        "ownership_inspected": True,
        "writers_quiesced": True,
        "max_seconds": 30,
        "peer_paid_inference_disabled": True,
        "control_url": "http://127.0.0.1:8000",
        "critic_url": "http://127.0.0.1:8001",
        "project_id": "project",
        "critic_agent_id": "critic",
        "steps": steps,
    }


def test_receipt_closed_projection_never_leaks_payloads_or_credentials():
    m = manifest()
    m["human_token_file"] = "/private/secret-location"
    result = live.receipt(
        m,
        [
            {
                "stage": "critic_task",
                "status": "unknown",
                "code": "no_reply",
                "error": "Bearer SUPER_SECRET",
                "body": {"prompt": "PRIVATE"},
            }
        ],
    )
    encoded = json.dumps(result)
    assert all(s not in encoded for s in ("SUPER_SECRET", "PRIVATE", "secret-location", "prompt"))
    assert result["status"] == "incomplete"
    assert result["independent_host_verified"] is False


def test_passing_assertions_do_not_claim_full_integration_or_reasoning():
    result = live.receipt(
        manifest(), [{"stage": s, "status": "passed", "code": "observed"} for s in live.STAGES]
    )
    assert result["status"] == "passed"
    assert result["integrated_acceptance_verified"] is False
    assert result["reasoning_verified"] is False
    assert result["laptop_off_verified"] is False


@pytest.mark.parametrize(
    "key,value",
    [
        ("max_seconds", 601),
        ("ownership_inspected", False),
        ("writers_quiesced", False),
        ("peer_paid_inference_disabled", False),
        ("control_url", "http://remote.example"),
        ("critic_url", "https://user:password@host.example"),
        ("token", "secret"),
    ],
)
def test_fail_closed_manifest(key, value):
    m = manifest()
    m[key] = value
    with pytest.raises(live.Blocked):
        live.validate(m)


def test_valid_contract_and_fence_is_not_just_arbitrary_sdk_error():
    m = manifest()
    live.validate(m)
    m["steps"][8]["assertions"] = [{"path": "is_error", "value": True}]
    with pytest.raises(live.Blocked, match="explicit_fence"):
        live.validate(m)


def test_no_polling_mutations_or_alternate_transports():
    m = manifest()
    m["steps"][6]["poll_seconds"] = 10
    with pytest.raises(live.Blocked):
        live.validate(m)
    m = manifest()
    m["steps"][0]["via"] = "http"
    with pytest.raises(live.Blocked):
        live.validate(m)


def test_timeout_is_unknown_without_retry_or_fallback():
    class Journey:
        m = {**manifest(), "max_seconds": 0.001}
        results = []
        calls = 0

        async def run(self):
            self.calls += 1
            await asyncio.sleep(1)

    journey = Journey()
    result = asyncio.run(live.bounded_run(journey))
    assert journey.calls == 1
    assert result["status"] == "incomplete"
    assert result["checks"][0]["status"] == "unknown"


def test_private_manifest_permission_and_symlink_fences(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest()))
    path.chmod(0o600)
    assert live.private_json(path)["max_seconds"] == 30
    path.chmod(0o644)
    with pytest.raises(live.Blocked):
        live.private_json(path)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        live.private_json(link)


def test_unknown_errors_do_not_leak_exception_text():
    class Journey:
        m = manifest()
        results = []

        async def run(self):
            raise RuntimeError("SECRET_TOKEN provider raw response")

    result = asyncio.run(live.bounded_run(Journey()))
    assert "SECRET_TOKEN" not in json.dumps(result)
    assert result["checks"][0]["code"] == "blocked_or_unknown_no_fallback"


def test_references_and_failure_assertions():
    observed = {"human_issue": {"body": {"id": "grant-1"}}}
    assert live.resolve({"grant_id": {"$ref": "human_issue.body.id"}}, observed) == {
        "grant_id": "grant-1"
    }
    with pytest.raises(AssertionError):
        live.assertions(
            {"assertions": [{"path": "body.state", "value": "succeeded"}]},
            {"body": {"state": "unknown"}},
            observed,
        )


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Control API denied or rejected request (HTTP 403)", 403),
        ("network unavailable SECRET_TOKEN", None),
        ("Control API denied or rejected request (HTTP 500)", None),
    ],
)
def test_sdk_failure_unknown_is_not_permission_denial(text, expected):
    pytest.importorskip("mcp")

    class Session:
        async def call_tool(self, name, arguments):
            return SimpleNamespace(isError=True, content=[SimpleNamespace(type="text", text=text)])

    journey = live.InstalledJourney(manifest())
    step = journey.m["steps"][8]
    if expected is None:
        with pytest.raises(live.Unknown):
            asyncio.run(journey.call(Session(), None, step))
    else:
        result = asyncio.run(journey.call(Session(), None, step))
        assert result == {"is_error": True, "http_status": expected, "body": None}


def test_sdk_shaped_structured_result_consumed_without_stdout(capsys):
    pytest.importorskip("mcp")

    class Session:
        async def call_tool(self, name, arguments):
            return SimpleNamespace(isError=False, structuredContent={"id": "attempt-1"})

    journey = live.InstalledJourney(manifest())
    result = asyncio.run(journey.call(Session(), None, journey.m["steps"][8]))
    assert result["body"]["id"] == "attempt-1"
    assert capsys.readouterr().out == ""


def probe_responses():
    return [
        {"metadata": {"uid": "ns-uid"}},
        {
            "metadata": {"uid": "deploy-uid"},
            "spec": {"selector": {"matchLabels": {"app": "control"}}},
        },
        {
            "items": [
                {
                    "metadata": {
                        "uid": "rs-uid",
                        "ownerReferences": [{"controller": True, "uid": "deploy-uid"}],
                    }
                }
            ]
        },
        {
            "items": [
                {
                    "metadata": {
                        "uid": "pod-uid",
                        "ownerReferences": [{"controller": True, "uid": "rs-uid"}],
                    },
                    "status": {
                        "containerStatuses": [
                            {
                                "name": "control",
                                "ready": True,
                                "restartCount": 0,
                                "state": {"running": {"startedAt": "2026-10-04T00:00:00Z"}},
                            }
                        ]
                    },
                }
            ]
        },
    ]


def restart_probe():
    return {
        "kubeconfig": "/documented/config",
        "context": "docker-desktop",
        "namespace": "tessera-lab",
        "namespace_uid": "ns-uid",
        "deployment": "tessera-control",
        "deployment_uid": "deploy-uid",
    }


def install_probe(monkeypatch, responses):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["timeout"] == 10
        assert "create" not in argv and "delete" not in argv and "rollout" not in argv
        return SimpleNamespace(returncode=0, stdout=json.dumps(responses[len(calls) - 1]).encode())

    monkeypatch.setattr(live.subprocess, "run", run)
    return calls


def test_generation_probe_is_scoped_readonly_real_start_binding(monkeypatch):
    responses = probe_responses()
    calls = install_probe(monkeypatch, responses)
    original = live.kubernetes_generation(restart_probe())
    assert original["body"]["ready"] is True
    assert len(calls) == 4
    assert all(
        argv[:5] == ["kubectl", "--kubeconfig", "/documented/config", "--context", "docker-desktop"]
        for argv in calls
    )
    responses[3]["items"][0]["status"]["containerStatuses"][0]["restartCount"] = 1
    install_probe(monkeypatch, responses)
    restarted = live.kubernetes_generation(restart_probe())
    assert original["body"]["generation"] != restarted["body"]["generation"]


@pytest.mark.parametrize("case", ["namespace", "deployment", "foreign_pod", "not_ready"])
def test_generation_probe_fails_closed_on_owner_or_liveness_change(monkeypatch, case):
    responses = probe_responses()
    if case == "namespace":
        responses[0]["metadata"]["uid"] = "replacement"
    elif case == "deployment":
        responses[1]["metadata"]["uid"] = "replacement"
    elif case == "foreign_pod":
        responses[3]["items"][0]["metadata"]["ownerReferences"][0]["uid"] = "other-rs"
    else:
        responses[3]["items"][0]["status"]["containerStatuses"][0]["ready"] = False
    install_probe(monkeypatch, responses)
    with pytest.raises((live.Blocked, live.Unknown)):
        live.kubernetes_generation(restart_probe())


def test_implemented_typed_consume_contract_and_stage_action():
    pytest.importorskip("mcp")
    step = manifest()["steps"][8]
    args = live.typed_arguments(step, {})
    assert args["payload"]["command"]["action"] == "attempt:status"
    step["stage"] = "agent_dispatch"
    with pytest.raises(live.Blocked, match="stage_command"):
        live.typed_arguments(step, {})
    step["stage"] = "denied_expansion"
    step["arguments"]["payload"]["arbitrary_proxy_path"] = "/owner"
    with pytest.raises(ValueError):
        live.typed_arguments(step, {})


def test_kubernetes_restart_mode_requires_all_explicit_pins():
    m = manifest()
    m["restart_probe"] = restart_probe()
    m["steps"][10]["via"] = m["steps"][11]["via"] = "kubernetes"
    live.validate(m)
    del m["restart_probe"]["namespace_uid"]
    with pytest.raises(live.Blocked):
        live.validate(m)


@pytest.mark.parametrize(
    "field,value",
    [
        ("protocol_version", "0.3"),
        ("protocol_binding", "HTTP+JSON"),
        ("url", "https://unapproved.example"),
        ("url", "http://127.0.0.1:8001/secret-path"),
    ],
)
def test_critic_card_cannot_reroute_bearer_to_unapproved_interface(field, value):
    interface = SimpleNamespace(
        protocol_version="1.0", protocol_binding="JSONRPC", url="http://127.0.0.1:8001"
    )
    card = SimpleNamespace(name="critic", supported_interfaces=[interface])
    live.validate_critic_card(card, manifest())
    setattr(interface, field, value)
    with pytest.raises(live.Blocked):
        live.validate_critic_card(card, manifest())


def test_legacy_a2a_jsonrpc_guess_rejected_before_calls():
    m = manifest()
    step = m["steps"][2]
    del step["request"]
    step["body"] = {"method": "message/send", "params": {}}
    with pytest.raises(live.Blocked, match="a2a_v1"):
        live.validate(m)


def local_setup_manifest(tmp_path):
    human = tmp_path / "human-token"
    live.private_write(human, b"h" * 43)
    return {
        "control_url": "http://127.0.0.1:8765",
        "human_token_file": str(human),
        "project_id": "project-test",
        "environment": "local_owned_docker",
        "writers_quiesced": True,
        "ownership_inspected": True,
        "local_setup": {
            "human_approved": True,
            "private_dir": str(tmp_path / "setup"),
            "duration_seconds": 7200,
            "agents": {
                "planner": {
                    "destination": "approved-local",
                    "actions": [
                        "context:read",
                        "proposal:create",
                        "authority:request",
                        "authority:consume",
                        "notebook:read",
                    ],
                },
                "critic": {
                    "destination": "approved-local",
                    "actions": ["context:read", "authority:consume", "notebook:read"],
                },
            },
        },
    }


@pytest.mark.parametrize("duration", [59, 7201])
def test_local_setup_lifetime_remains_bounded(tmp_path, duration):
    manifest = local_setup_manifest(tmp_path)
    manifest["local_setup"]["duration_seconds"] = duration
    with pytest.raises(live.Blocked, match="bounded_setup_lifetime_required"):
        asyncio.run(live.setup_local(manifest, None))


def test_actual_isolated_human_api_enrollment_no_token_proxy(tmp_path, capsys):
    import httpx

    from tessera_control.api import create_app
    from tessera_control.store import initialize_state
    from tessera_mcp.config import Config

    state = tmp_path / "control-state"
    initialize_state(state)
    owner = (state / "owner.header").read_text().strip().split("Bearer ", 1)[1]
    m = local_setup_manifest(tmp_path)
    Path(m["human_token_file"]).write_text(owner)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state))
        ) as client:
            response = await client.post(
                m["control_url"] + "/projects",
                json={"name": "Isolated journey"},
                headers={"Authorization": "Bearer " + owner, "Idempotency-Key": "setup-project"},
            )
            assert response.status_code == 201
            m["project_id"] = response.json()["id"]
            prepared = await live.setup_local(m, client)
            replay = await live.setup_local(m, client)
            assert prepared == replay
            for role in ("planner", "critic"):
                token = Config(
                    m["control_url"],
                    Path(prepared[role]["token_file"]),
                    m["project_id"],
                    prepared[role]["destination"],
                ).token()
                assert token != owner
                status = await client.get(
                    m["control_url"] + "/external-agent/status",
                    headers={
                        "Authorization": "Bearer " + token,
                        "X-Agent-Destination": prepared[role]["destination"],
                    },
                )
                assert status.status_code == 200
                # Enrollment gives no right to mint its own credential/approval.
                denial = await client.post(
                    m["control_url"] + "/credentials",
                    headers={
                        "Authorization": "Bearer " + token,
                        "Idempotency-Key": "self-enroll-denied",
                    },
                    json={
                        "token": "z" * 43,
                        "kind": "agent",
                        "audience": "control",
                        "project_id": m["project_id"],
                        "expires_at": prepared[role]["expires_at"],
                    },
                )
                assert denial.status_code == 403
            return prepared

    prepared = asyncio.run(run())
    assert prepared["planner"]["credential_id"] != prepared["critic"]["credential_id"]
    assert capsys.readouterr().out == ""
    journal = (tmp_path / "setup/setup-state.json").read_text()
    assert owner not in journal
    for name in ("planner-token", "critic-token", "peer-token"):
        token = (tmp_path / "setup" / name).read_text()
        assert token not in journal
        assert (tmp_path / "setup" / name).stat().st_mode & 0o077 == 0


def test_setup_unknown_reply_no_retry_or_new_identity(tmp_path):
    import httpx

    m = local_setup_manifest(tmp_path)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, json={"token": "DO_NOT_LOG"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(live.Unknown):
                await live.setup_local(m, client)
            original = (tmp_path / "setup/planner-token").read_bytes()
            with pytest.raises(live.Unknown):
                await live.setup_local(m, client)
            assert (tmp_path / "setup/planner-token").read_bytes() == original

    asyncio.run(run())
    assert len(calls) == 1
    assert "DO_NOT_LOG" not in (tmp_path / "setup/setup-state.json").read_text()


@pytest.mark.parametrize("field", ["writers_quiesced", "ownership_inspected"])
@pytest.mark.parametrize("value", [False, "false", 1])
def test_setup_cannot_mutate_while_writers_active_or_unowned(tmp_path, field, value):
    m = local_setup_manifest(tmp_path)
    m[field] = value
    with pytest.raises(live.Blocked):
        asyncio.run(live.setup_local(m, None))
    assert not (tmp_path / "setup").exists()


def test_setup_directory_symlink_or_shared_permissions_denied(tmp_path):
    m = local_setup_manifest(tmp_path)
    (tmp_path / "setup").mkdir(mode=0o755)
    (tmp_path / "setup").chmod(0o755)
    with pytest.raises(live.Blocked):
        asyncio.run(live.setup_local(m, None))


def test_optional_worker_setup_uses_exact_fresh_target_and_human_registration(tmp_path):
    from dataclasses import asdict

    import httpx
    from test_kubernetes_remote_service import _fixture

    m = local_setup_manifest(tmp_path)
    client, output, _ = _fixture(tmp_path)
    cluster = asdict(client.cluster)
    cluster["context"] = "docker-desktop"
    cluster["profile"] = "development-minikube-opencode-v1"
    evidence = cluster.pop("network_none_evidence")
    target = tmp_path / "target.json"
    target.write_text(
        json.dumps(
            {
                "cluster": cluster,
                "network_evidence": evidence,
                "output": asdict(output),
                "collector_image": "local/installed@sha256:" + "a" * 64,
            },
            default=str,
        )
    )
    m["local_setup"]["worker"] = {
        "target_file": str(target),
        "target_file_sha256": live.hashlib.sha256(target.read_bytes()).hexdigest(),
        "namespace_uid": cluster["namespace_uid"],
        "pvc_uid": output.pvc_uid,
        "build_digest": "a" * 64,
    }
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        assert request.headers["Authorization"] == "Bearer " + "h" * 43
        calls.append((request.url.path, payload))
        if request.url.path == "/credentials":
            return httpx.Response(
                201,
                json={
                    "credential_id": "credential-" + str(len(calls)),
                    "principal_id": "principal-" + str(len(calls)),
                    "org_id": "org-test",
                    "project_id": "project-test",
                    "kind": payload["kind"],
                    "audience": payload["audience"],
                    "expires_at": payload["expires_at"],
                },
            )
        return httpx.Response(
            201, json={"id": "registration-1", "worker_principal_id": "principal-3"}
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await live.setup_local(m, http)

    prepared = asyncio.run(run())
    assert prepared["worker"]["principal_id"] == "principal-3"
    assert calls[-1][0] == "/projects/project-test/workers"
    assert calls[-1][1]["approved_build_digest"] == "a" * 64
    assert prepared["worker"]["token_file"] != m["human_token_file"]


def test_setup_changed_intent_does_not_widen_confirmed_credential(tmp_path):
    import httpx

    m = local_setup_manifest(tmp_path)
    m["local_setup"]["peer"] = {"public_url": "http://127.0.0.1:8771", "model": "model"}
    calls = []

    def handler(request):
        calls.append(request)
        payload = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "credential_id": "credential-" + str(len(calls)),
                "principal_id": "principal-" + str(len(calls)),
                "org_id": "org-test",
                "project_id": "project-test",
                "kind": "agent",
                "audience": "control",
                "expires_at": payload["expires_at"],
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            prepared = await live.setup_local(m, client)
            peer = live.private_json(prepared["peer_config_file"])
            assert peer["agent_id"] == "principal-2"
            assert peer["credential_id"] == "credential-2"
            assert peer["control_token_env"] == "TESSERA_JOURNEY_CRITIC_CONTROL_TOKEN"
            assert (tmp_path / "setup/critic-token").read_text() not in json.dumps(peer)
            m["local_setup"]["agents"]["critic"]["destination"] = "unapproved-destination"
            with pytest.raises(live.Blocked, match="intent_changed"):
                await live.setup_local(m, client)

    asyncio.run(run())
    assert len(calls) == 2


def test_status_child_setup_delegates_with_real_agent_not_human_proxy(tmp_path):
    from datetime import UTC, datetime, timedelta

    import httpx

    m = local_setup_manifest(tmp_path)
    m["local_setup"]["critic_status_bounds"] = {
        "model": "model",
        "purpose": "Bounded reference critique",
        "actions": ["attempt:status"],
        "budget_cents": 0,
        "max_duration_seconds": 60,
        "expires_at": (datetime.now(UTC) + timedelta(seconds=300)).isoformat(),
        "bindings": [
            {
                "variant_id": "variant-test",
                "expected_variant_revision": 1,
                "expected_design_revision": 1,
                "execution_grant_id": "execution-grant-test",
                "expected_execution_grant_revision": 1,
                "expected_execution_epoch": 1,
                "task_id": "task-test",
                "expected_task_revision": 1,
                "plan_digest": "a" * 64,
                "target_digest": "b" * 64,
            }
        ],
    }
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(request.url.path)
        if request.url.path == "/credentials":
            return httpx.Response(
                201,
                json={
                    "credential_id": "credential-" + str(len(calls)),
                    "principal_id": "principal-" + str(len(calls)),
                    "org_id": "org-test",
                    "project_id": "project-test",
                    "kind": "agent",
                    "audience": "control",
                    "expires_at": payload["expires_at"],
                },
            )
        if request.url.path.endswith("/delegate"):
            planner = (tmp_path / "setup/planner-token").read_text()
            assert request.headers["Authorization"] == "Bearer " + planner
            assert request.headers["X-Agent-Destination"] == "approved-local"
            assert payload["bounds"]["subject_credential_id"] == "credential-2"
            assert payload["bounds"]["actions"] == ["attempt:status"]
            assert payload["bounds"]["delegation_depth"] == 0
            return httpx.Response(201, json={"id": "status-child", "revision": 1, "epoch": 1})
        assert request.headers["Authorization"] == "Bearer " + "h" * 43
        assert payload["bounds"]["subject_credential_id"] == "credential-1"
        return httpx.Response(201, json={"id": "status-root", "revision": 1, "epoch": 1})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await live.setup_local(m, client)

    prepared = asyncio.run(run())
    assert prepared["critic_status_grant"]["id"] == "status-child"
    assert len(calls) == 4
