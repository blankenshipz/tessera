"""OpenCode-harness model-call receipts, cost basis and backoff (ADR 0083). Synthetic only.

The billed Anthropic Messages API runtime is covered in test_anthropic_runtime_integration.
"""

import hashlib
import json
import sqlite3
import urllib.error

import pytest

import tessera_agent.remote_reasoning_runtime as rr
from tessera_agent.remote_reasoning_runtime import (
    JsonEndpoint,
    OpenCodeV2Reasoning,
    ReasoningSupervisor,
    RuntimeBlocked,
    validate_profile,
)
from tessera_agent.remote_reasoning_store import ReasoningStore

PROMPT = "SENTINEL-PROMPT-TEXT-do-not-journal"
CREDENTIAL = "anthropic-api-key://SENTINEL-OWNER/SENTINEL-CREDENTIAL"
SOURCE = hashlib.sha256(b"source").hexdigest()
PERMISSIONS = [
    {"action": "*", "resource": "*", "effect": "deny"},
    {"action": "tessera_propose", "resource": "*", "effect": "allow"},
]


def make_profile(model_id="gpt-5-codex", **extra):
    return {
        "run_id": "run1",
        "account_mode": "included-chatgpt-oauth",
        "model": {"providerID": "openai", "id": model_id},
        "max_steps": 3,
        "max_tokens": 1000,
        "max_duration_seconds": 7200,
        "permissions": PERMISSIONS,
        "input_digests": {"source": SOURCE},
        "project_id": "project1",
        **extra,
    }


def unsealed(project, digest):
    return {"project_id": project, "claims": [], "truncated": False}


class Harness:
    def __init__(self):
        self.calls, self.models = [], []
        self.error = None
        self.observation = {
            "tokens": 15,
            "cost": 0,
            "outcome": "succeeded",
            "reported_model": "openai/gpt-5-codex",
            "usage": {"input": 5, "output": 4, "reasoning": 3, "cache_read": 2, "cache_write": 1},
        }

    def verify(self):
        return True

    def create(self, row):
        self.calls.append("create")

    def prompt(self, row, text):
        self.calls.append("prompt")
        if isinstance(self.error, str):
            raise RuntimeBlocked(self.error)

    def status(self, row):
        self.calls.append("status")
        if isinstance(self.error, Exception):
            raise self.error
        return dict(self.observation)

    def interrupt(self, row):
        self.calls.append("interrupt")


class Time:
    now = 100.0

    def __call__(self):
        return self.now


def build(tmp_path, profile=None):
    profile = profile or make_profile()
    store = ReasoningStore(tmp_path / "journal")
    binding = {
        "grant_id": "g1",
        "revision": 1,
        "epoch": 1,
        "subject_credential_id": "agent1",
        "destination": "lab",
        "model": profile["model"]["id"],
        "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
        "profile_sha256": hashlib.sha256(b"profile").hexdigest(),
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
            "model": profile["model"]["id"],
            "budget_cents": 20,
            "max_duration_seconds": 7200,
            "expires_at": "2100-01-01T00:00:00+00:00",
        },
    }
    clock, harness = Time(), Harness()
    supervisor = ReasoningSupervisor(
        store, harness, lambda _: grant, profile, clock=clock, holdout_lookup=unsealed
    )
    return store, supervisor, harness, clock


def test_receipt_fields_and_cost_basis(tmp_path):
    store, supervisor, harness, _ = build(tmp_path)
    assert supervisor.tick("run1", PROMPT)["state"] == "completed"
    (call,) = store.model_calls("run1")
    assert call["model_configured"] == "openai/gpt-5-codex"
    assert call["model_reported"] == "openai/gpt-5-codex"
    assert call["model_identity_basis"] == "harness_reported_unverified"
    assert call["tier"] == "unclassified" and call["seq"] == 1
    assert call["prompt_sha256"] == hashlib.sha256(PROMPT.encode()).hexdigest()
    assert call["profile_sha256"] == hashlib.sha256(b"profile").hexdigest()
    assert call["input_sha256"] == call["prompt_sha256"]
    assert call["input_digests"] == {"source": SOURCE}
    assert call["tokens"] == {
        "input": 5, "output": 4, "reasoning": 3, "cache_read": 2, "cache_write": 1, "total": 15,
    }  # fmt: skip
    assert call["cost"] == 0 and call["cost_basis"] == "included_subscription_usage"
    expected = hashlib.sha256(
        json.dumps(ReasoningSupervisor.policy(supervisor.profile), sort_keys=True,
                   separators=(",", ":")).encode()
    ).hexdigest()  # fmt: skip
    assert call["policy_sha256"] == expected
    assert "credential" not in json.dumps(ReasoningSupervisor.policy(supervisor.profile))
    assert store.latest_rate_limit("run1") is None


def test_input_class_defaults_to_private_derived(tmp_path):
    store, supervisor, _, _ = build(tmp_path)
    supervisor.tick("run1", PROMPT)
    (call,) = store.model_calls("run1")
    assert call["input_class"] == "private_derived"


@pytest.mark.parametrize("declared", ["public", "private_code_approved", "private_derived"])
def test_declared_input_class_recorded_in_receipt_and_policy(tmp_path, declared):
    profile = make_profile(input_class=declared)
    store, supervisor, _, _ = build(tmp_path, profile)
    supervisor.tick("run1", PROMPT)
    (call,) = store.model_calls("run1")
    assert call["input_class"] == declared
    assert ReasoningSupervisor.policy(profile)["input_class"] == declared


def full_profile(**extra):
    return {
        **make_profile(),
        "agent": "researcher",
        "grant_id": "grant1",
        "subject_credential_id": "agent1",
        "max_duration_seconds": 60,
        **extra,
    }


def test_valid_input_classes_pass_validation():
    for declared in ("public", "private_code_approved", "private_derived"):
        validate_profile(full_profile(input_class=declared))
    validate_profile(full_profile())


def test_input_class_changes_policy_digest():
    assert ReasoningSupervisor.policy(make_profile(input_class="public")) != (
        ReasoningSupervisor.policy(make_profile())
    )


@pytest.mark.parametrize("bad", ["secret", "", None, 1, ["public"], "PUBLIC"])
def test_invalid_input_class_refused(bad):
    with pytest.raises(ValueError, match="Invalid input data class") as caught:
        validate_profile(full_profile(input_class=bad))
    assert "secret" not in str(caught.value)


def test_reported_cost_basis_and_fence_kept(tmp_path):
    store, supervisor, harness, _ = build(tmp_path)
    harness.observation["cost"] = 0.5
    assert supervisor.tick("run1", PROMPT)["reason"] == "usage_fence"
    (call,) = store.model_calls("run1")
    assert call["cost"] == 0.5 and call["cost_basis"] == "reported_cost"


def test_policy_digest_changes_with_bounds_not_credentials(tmp_path):
    a = ReasoningSupervisor.policy(make_profile())
    assert a == ReasoningSupervisor.policy(make_profile(credential_ref="anthropic-api-key://x/y"))
    assert a != ReasoningSupervisor.policy(make_profile(max_tokens=5))


def test_sentinels_absent_from_journal_bytes(tmp_path):
    store, supervisor, _, _ = build(tmp_path)
    supervisor.tick("run1", PROMPT)
    store.db.commit()
    blob = b"".join(p.read_bytes() for p in (tmp_path / "journal").iterdir() if p.is_file())
    dump = "\n".join(store.db.iterdump()).encode()
    for secret in (PROMPT, CREDENTIAL, "SENTINEL-CREDENTIAL"):
        assert secret.encode() not in blob and secret.encode() not in dump


def test_unknown_receipt_field_refused(tmp_path):
    store, _, _, _ = build(tmp_path)
    with pytest.raises(ValueError):
        store.model_call("run1", {"prompt": PROMPT}, 1)


def test_existing_journal_directory_migrates(tmp_path):
    directory = tmp_path / "journal"
    directory.mkdir(mode=0o700)
    db = sqlite3.connect(directory / "reasoning.sqlite3")
    db.execute("CREATE TABLE runs (id TEXT PRIMARY KEY)")
    db.commit()
    db.close()
    (directory / "reasoning.sqlite3").chmod(0o600)
    store = ReasoningStore(directory)  # old state dir gains the new tables
    assert store.latest_rate_limit("none") is None
    store.close()


@pytest.mark.parametrize(
    "error,condition,wait",
    [
        ({"name": "ProviderRateLimit", "message": PROMPT}, "model_rate_limited", 60),
        ({"class": "insufficient_quota", "retry_after": 120}, "model_quota_exhausted", 120),
        ("rate_limit_error", "model_rate_limited", 60),
    ],
)
def test_session_limit_recorded_surfaced_and_backed_off(tmp_path, error, condition, wait):
    store, supervisor, harness, clock = build(tmp_path)
    found = rr._session_condition(error)
    harness.observation["rate_limit"] = {"condition": found[0], "retry_after": found[1]}
    harness.observation["outcome"] = "failed"  # a limited session is paused, not terminal
    row = supervisor.tick("run1", PROMPT)
    assert row["state"] == "running"
    assert store.latest_rate_limit("run1") == {
        "condition": condition,
        "backoff_until": 100 + wait,
    }
    calls = list(harness.calls)
    clock.now = 100 + wait - 1  # still inside backoff: no harness call at all
    assert supervisor.tick("run1", PROMPT)["state"] == "running"
    assert harness.calls == calls
    clock.now = 100 + wait  # backoff over; same session polled, never re-prompted
    del harness.observation["rate_limit"]
    harness.observation["outcome"] = "succeeded"
    assert supervisor.tick("run1")["state"] == "completed"
    assert harness.calls.count("prompt") == 1


def test_status_endpoint_limit_blocks_without_halting(tmp_path):
    store, supervisor, harness, clock = build(tmp_path)
    harness.error = rr.ModelConditionBlocked("model_quota_exhausted", 30)
    row = supervisor.tick("run1", PROMPT)
    assert row["state"] == "running" and "interrupt" not in harness.calls
    assert store.latest_rate_limit("run1") == {
        "condition": "model_quota_exhausted",
        "backoff_until": 130,
    }


def test_prompt_limit_is_recorded_and_never_replayed(tmp_path):
    store, supervisor, harness, clock = build(tmp_path)
    harness.error = "model_rate_limited"
    assert supervisor.tick("run1", PROMPT)["reason"] == "uncertain_prompt"
    assert store.latest_rate_limit("run1")["condition"] == "model_rate_limited"
    clock.now = 10_000
    harness.error = None
    supervisor.tick("run1", PROMPT)
    assert harness.calls.count("prompt") == 1


def test_repolled_unchanged_observation_appends_no_row(tmp_path):
    store, supervisor, harness, clock = build(tmp_path)
    harness.observation["outcome"] = None
    supervisor.tick("run1", PROMPT)
    supervisor.tick("run1", PROMPT)
    assert len(store.model_calls("run1")) == 1
    harness.observation.update(tokens=20, outcome="succeeded")
    supervisor.tick("run1", PROMPT)
    assert [c["seq"] for c in store.model_calls("run1")] == [1, 2]
    assert harness.calls.count("prompt") == 1


def test_declared_digests_without_project_halt_seal_unknown(tmp_path):
    profile = make_profile()
    profile.pop("project_id")
    store, supervisor, harness, _ = build(tmp_path, profile)
    assert supervisor.tick("run1", PROMPT)["reason"] == "model_input_seal_unknown"
    assert "prompt" not in harness.calls


def test_quota_failure_never_changes_model_or_tier(tmp_path):
    # A cheaper model is configured alongside; it must stay unused.
    cheaper = make_profile("gpt-5-mini")
    profile = make_profile(available_models=[cheaper["model"]])
    store, supervisor, harness, clock = build(tmp_path, profile)
    harness.observation["rate_limit"] = {"condition": "model_quota_exhausted", "retry_after": None}
    supervisor.tick("run1", PROMPT)
    clock.now = 100 + 3601
    supervisor.tick("run1", PROMPT)
    assert supervisor.profile["model"]["id"] == "gpt-5-codex"
    for call in store.model_calls("run1"):
        assert call["model_configured"] == "openai/gpt-5-codex"
    assert "gpt-5-mini" not in json.dumps(store.model_calls("run1"))
    assert json.loads(store.get("run1")["binding"])["model"] == "gpt-5-codex"


class Response:
    def __init__(self, body):
        self.body = body

    def read(self, n):
        return self.body

    def close(self):
        pass


@pytest.mark.parametrize(
    "code,headers,expected",
    [(429, {"Retry-After": "7"}, "model_rate_limited"), (402, {}, "model_quota_exhausted")],
)
def test_endpoint_maps_http_limits_without_leaking_bodies(monkeypatch, code, headers, expected):
    def opener(request, timeout):
        raise urllib.error.HTTPError(request.full_url, code, PROMPT, headers, None)

    monkeypatch.setattr(rr, "_default_open", opener)
    endpoint = JsonEndpoint("http://127.0.0.1:4096", "synthetic", model_conditions=True)
    with pytest.raises(RuntimeBlocked) as caught:
        endpoint.call("GET", "/x")
    assert str(caught.value) == expected and PROMPT not in repr(caught.value)
    assert getattr(caught.value, "retry_after", None) == (7.0 if headers else None)
    control = JsonEndpoint("http://127.0.0.1:8000", "synthetic")
    with pytest.raises(RuntimeBlocked, match="endpoint_unavailable_or_invalid"):
        control.call("GET", "/x")


def test_v2_status_reports_breakdown_model_and_limit():
    class Endpoint:
        def call(self, *args):
            return {
                "data": {
                    "id": "ses",
                    "metadata": {"binding_digest": "d"},
                    "model": {"providerID": "anthropic", "id": "claude-sonnet-4-5"},
                    "permissions": [],
                    "cost": 0,
                    "error": {"name": "RateLimitError", "message": PROMPT},
                    "tokens": {
                        "input": 1,
                        "output": 2,
                        "reasoning": 3,
                        "cache": {"read": 4, "write": 5},
                    },
                }
            }

    model = {"providerID": "anthropic", "id": "claude-sonnet-4-5"}
    seen = OpenCodeV2Reasoning(Endpoint(), {"model": model, "permissions": []}).status(
        {"session_id": "ses", "digest": "d"}
    )
    assert seen["tokens"] == 15 and seen["reported_model"] == "anthropic/claude-sonnet-4-5"
    assert seen["usage"]["cache_write"] == 5
    assert seen["rate_limit"] == {"condition": "model_rate_limited", "retry_after": None}
    assert PROMPT not in json.dumps(seen)


def test_main_prints_receipt_with_model_calls_and_rate_limit(tmp_path, monkeypatch, capsys):
    profile = {
        **make_profile(max_duration_seconds=3600),
        "run_id": "run1",
        "agent": "researcher",
        "grant_id": "g1",
        "revision": 1,
        "epoch": 1,
        "subject_credential_id": "agent1",
        "destination": "lab",
        "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
        "control_url": "http://127.0.0.1:1",
        "harness_url": "http://127.0.0.1:2",
        "agent_token_env": "T_AGENT",
        "harness_token_env": "T_HARNESS",
    }
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile))
    path.chmod(0o600)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text(PROMPT)
    prompt.chmod(0o600)
    monkeypatch.setenv("T_AGENT", "SENTINEL-AGENT-TOKEN")
    monkeypatch.setenv("T_HARNESS", "SENTINEL-HARNESS-TOKEN")
    grant = {
        "revision": 1, "epoch": 1, "status": "active", "effective_status": "active",
        "remaining_budget_cents": 20,
        "bounds": {
            "subject_credential_id": "agent1", "destination": "lab",
            "model": "gpt-5-codex", "budget_cents": 20, "max_duration_seconds": 7200,
            "expires_at": "2100-01-01T00:00:00+00:00",
        },
    }  # fmt: skip

    class Control:
        def __init__(self, *args, **kwargs):
            pass

        def call(self, method, path, body=None):
            if "/holdout-claims" in path:
                return {"claims": [], "truncated": False}
            return grant

    harness = Harness()
    monkeypatch.setattr(rr, "JsonEndpoint", Control)
    monkeypatch.setattr(rr, "OpenCodeV2Reasoning", lambda endpoint, profile: harness)
    state = tmp_path / "state"
    assert rr.main(["run", "--profile", str(path), "--state-dir", str(state),
                    "--prompt-file", str(prompt)]) == 0  # fmt: skip
    out = capsys.readouterr().out
    receipt = json.loads(out)
    (call,) = receipt["model_calls"]
    assert call["tier"] == "unclassified" and call["cost_basis"] == "included_subscription_usage"
    assert receipt["rate_limit"] is None
    store = ReasoningStore(state)
    store.rate_limit("run1", "model_rate_limited", 777.0, 1.0)
    store.close()
    rr.main(["status", "--profile", str(path), "--state-dir", str(state)])
    status = capsys.readouterr().out
    assert json.loads(status)["rate_limit"] == {
        "condition": "model_rate_limited", "backoff_until": 777.0,
    }  # fmt: skip
    blob = b"".join(p.read_bytes() for p in state.iterdir() if p.is_file())
    for secret in (PROMPT, "SENTINEL-AGENT-TOKEN", "SENTINEL-HARNESS-TOKEN", "SENTINEL-CREDENTIAL"):
        assert secret not in out + status and secret.encode() not in blob


def test_reserved_estimate_row_is_allowlisted_and_counts_toward_the_day(tmp_path):
    store, _, _, clock = build(tmp_path)
    receipt = {
        "tier": "sonnet",
        "cost": 1.53,
        "cost_basis": "reserved_worst_case_estimate",
        "outcome_class": "uncertain",
    }
    store.model_call("run1", receipt, clock(), tokens_delta=1100, cost_cents_delta=1.53)
    (row,) = store.model_calls("run1")
    assert row["outcome_class"] == "uncertain"
    day = store.daily_usage(rr.utc_day(clock()))
    assert day == {"tokens": 1100, "cost_cents": pytest.approx(1.53)}


def test_opencode_path_ignores_billed_call_attributes(tmp_path):
    # Only the billed direct API runtime receipts halting calls; OpenCode stays as it was.
    store, supervisor, harness, _ = build(tmp_path)
    error = RuntimeBlocked("model_call_uncertain")
    error.billed_call = {"tokens": 10, "cost": 1.0, "usage": {}, "cost_basis": "x"}

    def prompt(row, text):
        raise error

    harness.prompt = prompt
    row = supervisor.tick("run1", PROMPT)
    assert row["reason"] == "uncertain_prompt"
    assert store.model_calls("run1") == []
