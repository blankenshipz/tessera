"""Isolated synthetic harness tests; no inference, credentials or server access."""

import base64
import copy
import hashlib

import pytest

from tessera_agent.remote_reasoning_runtime import (
    JsonEndpoint,
    OpenCodeV2Reasoning,
    ReasoningSupervisor,
    RuntimeBlocked,
    validate_profile,
)
from tessera_agent.remote_reasoning_store import ReasoningStore, private_file


def test_endpoint_uses_explicit_harness_basic_without_changing_control_bearer():
    harness = JsonEndpoint("http://127.0.0.1:4096", "synthetic-password", auth_mode="basic")
    expected = base64.b64encode(b"opencode:synthetic-password").decode()
    assert harness.headers["Authorization"] == "Basic " + expected
    control = JsonEndpoint("http://127.0.0.1:8000", "synthetic-agent-token", "lab")
    assert control.headers["Authorization"] == "Bearer synthetic-agent-token"
    assert control.headers["X-Agent-Destination"] == "lab"
    with pytest.raises(ValueError, match="authentication mode"):
        JsonEndpoint("http://127.0.0.1:4096", "synthetic", auth_mode="none")


class Harness:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.observation = {"tokens": 1, "cost": 0, "outcome": None}

    def verify(self):
        return True

    def create(self, row):
        self.calls.append("create")
        if self.fail == "create":
            raise TimeoutError("private upstream detail")

    def prompt(self, row, text):
        self.calls.append("prompt")
        if self.fail == "prompt":
            raise TimeoutError("private upstream detail")

    def status(self, row):
        return self.observation

    def interrupt(self, row):
        self.calls.append("interrupt")
        if self.fail == "interrupt":
            raise TimeoutError()


@pytest.fixture
def runtime(tmp_path):
    store = ReasoningStore(tmp_path / "journal")
    binding = {
        "grant_id": "grant1",
        "revision": 1,
        "epoch": 1,
        "subject_credential_id": "agent1",
        "destination": "lab",
        "model": "gpt-model",
        "prompt_sha256": hashlib.sha256(b"bounded goal").hexdigest(),
    }
    store.bind("run1", binding, 100)
    grant = {
        "revision": 1,
        "epoch": 1,
        "status": "active",
        "effective_status": "active",
        "remaining_budget_cents": 20,
        "bounds": {
            "subject_credential_id": "agent1",
            "destination": "lab",
            "model": "gpt-model",
            "budget_cents": 20,
            "max_duration_seconds": 60,
            "expires_at": "1970-01-01T00:04:00+00:00",
        },
    }
    profile = {"max_duration_seconds": 60, "max_tokens": 100}
    harness = Harness()
    supervisor = ReasoningSupervisor(store, harness, lambda _: grant, profile, clock=lambda: 100)
    yield store, supervisor, harness, grant, binding
    store.close()


def test_restart_resumes_same_session_without_reprompt(runtime, tmp_path):
    store, supervisor, harness, grant, binding = runtime
    assert supervisor.tick("run1", "bounded goal")["state"] == "running"
    session = store.get("run1")["session_id"]
    second = ReasoningStore(tmp_path / "journal")
    resumed = ReasoningSupervisor(
        second, harness, lambda _: grant, supervisor.profile, clock=lambda: 101
    )
    assert resumed.tick("run1")["session_id"] == session
    assert harness.calls == ["create", "prompt"]
    harness.observation["outcome"] = "succeeded"
    assert resumed.tick("run1")["state"] == "completed"
    assert resumed.tick("run1")["state"] == "completed"
    second.close()


@pytest.mark.parametrize("boundary", ["create", "prompt"])
def test_uncertain_effect_is_not_retried(runtime, boundary):
    store, supervisor, harness, _, _ = runtime
    harness.fail = boundary
    row = supervisor.tick("run1", "bounded goal")
    assert row["reason"] == "uncertain_" + boundary
    assert "private upstream" not in str(row)
    supervisor.tick("run1", "bounded goal")
    assert harness.calls.count(boundary) == 1
    assert store.effect("run1", boundary) == "uncertain"


def test_crashed_pending_prompt_is_never_replayed(runtime):
    store, supervisor, harness, _, _ = runtime
    assert store.begin("run1", "create")
    store.finish("run1", "create", "acknowledged")
    assert store.begin("run1", "prompt")
    assert supervisor.tick("run1", "bounded goal")["reason"] == "uncertain_prompt"
    assert harness.calls == ["interrupt"]


@pytest.mark.parametrize(
    "fence", ["expiry", "revoke", "revision", "epoch", "budget", "scope", "duration"]
)
def test_fences_deny_before_any_harness_effect(runtime, fence):
    store, supervisor, harness, grant, _ = runtime
    if fence == "expiry":
        grant["bounds"]["expires_at"] = "1970-01-01T00:01:00+00:00"
    elif fence == "revoke":
        grant["status"] = "revoked"
    elif fence in {"revision", "epoch"}:
        grant[fence] += 1
    elif fence == "budget":
        grant["remaining_budget_cents"] = 0
    elif fence == "scope":
        grant["bounds"]["destination"] = "other"
    else:
        supervisor.clock = lambda: 200
    row = supervisor.tick("run1", "bounded goal")
    assert row["state"] == "stop_acknowledged"
    assert harness.calls == []
    assert store.effect("run1", "create") is None


def test_revocation_after_prompt_interrupts_without_new_work(runtime):
    store, supervisor, harness, grant, _ = runtime
    supervisor.tick("run1", "bounded goal")
    grant["status"] = "revoked"
    assert supervisor.tick("run1")["reason"] == "grant_revoked"
    assert harness.calls == ["create", "prompt", "interrupt"]
    assert store.get("run1")["state"] == "stop_acknowledged"


def test_ancestor_revocation_fences_active_child(runtime):
    _, supervisor, harness, grant, _ = runtime
    grant["effective_status"] = "fenced"
    assert supervisor.tick("run1", "bounded goal")["reason"] == "grant_revoked"
    assert harness.calls == []


def test_authority_rechecked_after_harness_inspection(runtime):
    _, supervisor, harness, grant, _ = runtime

    def revoke_during_inspection():
        grant["effective_status"] = "fenced"

    harness.verify = revoke_during_inspection
    assert supervisor.tick("run1", "bounded goal")["reason"] == "grant_revoked"
    assert harness.calls == []


def test_failed_reasoning_is_not_success(runtime):
    _, supervisor, harness, _, _ = runtime
    harness.observation["outcome"] = "failed"
    assert supervisor.tick("run1", "bounded goal")["state"] == "failed"


def test_stop_and_unknown_termination_are_distinct(runtime):
    store, supervisor, harness, _, _ = runtime
    supervisor.tick("run1", "bounded goal")
    store.stop("run1")
    harness.fail = "interrupt"
    assert supervisor.tick("run1")["state"] == "stop_unknown"
    supervisor.tick("run1")
    assert harness.calls.count("interrupt") == 1


@pytest.mark.parametrize("tokens,cost", [(100, 0), (1, 0.01)])
def test_usage_fence_records_usage_and_stops(runtime, tokens, cost):
    store, supervisor, harness, _, _ = runtime
    harness.observation.update(tokens=tokens, cost=cost)
    assert supervisor.tick("run1", "bounded goal")["reason"] == "usage_fence"
    assert store.get("run1")["tokens"] == tokens
    assert harness.calls[-1] == "interrupt"


def test_unknown_authority_fails_closed(runtime):
    _, supervisor, harness, _, _ = runtime

    def offline(_):
        raise TimeoutError("not logged")

    supervisor.authority = offline
    assert supervisor.tick("run1", "bounded goal")["reason"] == "invalid_authority_or_usage"
    assert harness.calls == []


def test_changed_prompt_or_binding_cannot_replay(runtime):
    store, supervisor, harness, _, binding = runtime
    with pytest.raises(ValueError, match="rebound"):
        store.bind("run1", {**binding, "epoch": 2}, 101)
    assert supervisor.tick("run1", "different goal")["reason"] == "prompt_required_or_changed"
    assert "prompt" not in harness.calls
    other = store.bind("run2", binding, 100)
    assert other["session_id"] != store.get("run1")["session_id"]


def test_usage_regression_stops(runtime):
    _, supervisor, harness, _, _ = runtime
    supervisor.tick("run1", "bounded goal")
    harness.observation["tokens"] = 0
    assert supervisor.tick("run1")["reason"] == "invalid_authority_or_usage"


def test_private_files_reject_symlink_and_public_permissions(tmp_path):
    path = tmp_path / "credential"
    path.write_text("synthetic-only")
    path.chmod(0o644)
    with pytest.raises(ValueError):
        private_file(path)
    path.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        private_file(link)


def test_profile_rejects_shell_and_paid_fallback():
    profile = {
        "run_id": "run1",
        "agent": "researcher",
        "grant_id": "grant1",
        "subject_credential_id": "agent1",
        "destination": "lab",
        "account_mode": "included-chatgpt-oauth",
        "model": {"providerID": "openai", "id": "gpt-model"},
        "max_steps": 3,
        "max_tokens": 100,
        "max_duration_seconds": 60,
        "permissions": [
            {"action": "*", "resource": "*", "effect": "deny"},
            {"action": "tessera_propose", "resource": "*", "effect": "allow"},
        ],
    }
    validate_profile(profile)
    bad = copy.deepcopy(profile)
    bad["permissions"].append({"action": "shell", "resource": "*", "effect": "allow"})
    with pytest.raises(ValueError):
        validate_profile(bad)
    bad = {**profile, "account_mode": "api-key"}
    with pytest.raises(ValueError):
        validate_profile(bad)


def test_v2_adapter_uses_real_routes_no_message_readback():
    class Endpoint:
        def __init__(self):
            self.calls = []

        def call(self, method, path, body=None):
            self.calls.append((method, path, body))
            return {"data": {"id": "ses_fixed"}}

    endpoint = Endpoint()
    adapter = OpenCodeV2Reasoning(
        endpoint,
        {
            "agent": "researcher",
            "model": {"providerID": "openai", "id": "gpt-model"},
            "workspace": "/private/research",
            "permissions": [],
        },
    )
    row = {"session_id": "ses_fixed", "id": "run1", "digest": "digest", "message_id": "msg_fixed"}
    adapter.create(row)
    adapter.prompt(row, "bounded goal")
    adapter.interrupt(row)
    assert [call[1] for call in endpoint.calls] == [
        "/api/session",
        "/api/session/ses_fixed/prompt",
        "/api/session/ses_fixed/interrupt?resume=false",
    ]
    assert endpoint.calls[0][2]["model"]["providerID"] == "openai"
    assert endpoint.calls[1][2]["id"] == "msg_fixed"


def test_v2_rejects_unknown_usage():
    class Endpoint:
        def call(self, *args):
            return {
                "data": {
                    "id": "ses_fixed",
                    "metadata": {"binding_digest": "digest"},
                    "model": {},
                    "permissions": [],
                    "cost": float("nan"),
                    "tokens": {
                        "input": 0,
                        "output": 0,
                        "reasoning": 0,
                        "cache": {"read": 0, "write": 0},
                    },
                }
            }

    adapter = OpenCodeV2Reasoning(Endpoint(), {"model": {}, "permissions": []})
    with pytest.raises(RuntimeBlocked, match="usage_unknown"):
        adapter.status({"session_id": "ses_fixed", "digest": "digest"})
