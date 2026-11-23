"""First-call gaps: rate limits never downshift the model, caps refuse before the network, and
a missing key fails closed through the supervisor.

Same rig as ``test_first_call_e2e_stub``: a real supervisor and journal, the real control routes
in-process and a recorded Messages API transport. No network and no real key; the only "key" is
a synthetic marker in a temporary credentials directory, and the environment variable set in
one test is a decoy that must never be read.
"""

# ruff: noqa: F811
from __future__ import annotations

import hashlib
import json
import os
from urllib.parse import urlsplit

import pytest
from test_anthropic_runtime_integration import (  # noqa: F401
    API_KEY,
    COMPLETION,
    DAY1,
    MODEL,
    PROMPT,
    Transport,
    assert_no_secrets,
    creds,
    journal_bytes,
    make_profile,
    ok,
    private,
    setup,
)
from test_first_call_e2e_stub import QUALIFIED, world  # noqa: F401
from test_model_spend_integration import Control

import tessera_agent.remote_reasoning_runtime as rr
from tessera_agent.anthropic_messages import COUNT_URL, AnthropicMessagesReasoning
from tessera_agent.model_spend_client import SpendLedger

DECOY = "sk-ant-DECOY-environment-key-never-used"


def rig(world, tmp_path, creds, transport, *, run_id="run1", clock=None, limits=None):
    """The e2e rig with a caller-owned transport and clock, so runs can share a state dir."""
    extra = {"limits": limits} if limits is not None else {}
    profile = make_profile(run_id=run_id, model_spend={"project_id": world.project}, **extra)
    store, supervisor, clock, _ = setup(tmp_path, creds, transport, profile=profile, clock=clock)
    supervisor.spend = SpendLedger(Control(world), None, "anthropic", QUALIFIED)
    return store, supervisor, clock


class PinnedModelTransport(Transport):
    """Refuses (and remembers) any request naming a model other than the profile's."""

    def __init__(self, *replies):
        super().__init__(*replies)
        self.models = []

    def post_json(self, url, headers, body, *, timeout=None):
        self.models.append(body.get("model"))
        if body.get("model") != MODEL:
            raise AssertionError("model changed")  # never answered: no downshift can succeed
        return super().post_json(url, headers, body, timeout=timeout)


def control_dump(world):
    return "\n".join(
        json.dumps(dict(r))
        for table in ("model_spend_calls", "events")
        for r in world.rows(f"SELECT * FROM {table}")
    )


# -- (R5) a 429 backs off and retries the same model; it never downshifts ---------------------


def test_rate_limit_backs_off_and_retries_the_same_model(world, tmp_path, creds, capsys):
    assert world.limits(per_call=5, daily=20).status_code == 200
    rejected = (429, {"retry-after": "30"}, {"error": {"type": "rate_limit_error"}})
    transport = PinnedModelTransport(rejected, ok())
    store, supervisor, clock = rig(world, tmp_path, creds, transport)

    row = supervisor.tick("run1", PROMPT)
    assert (row["state"], row["reason"]) == ("ready", None)
    assert store.latest_rate_limit("run1") == {
        "condition": "model_rate_limited",
        "backoff_until": DAY1 + 30,
    }
    assert store.model_calls("run1") == []
    assert len(transport.messages()) == 1

    # Before the wait is over nothing at all is sent, not even a token count.
    sent = len(transport.sent)
    clock.now = DAY1 + 29
    supervisor.tick("run1", PROMPT)
    assert len(transport.sent) == sent

    clock.now = DAY1 + 30
    assert supervisor.tick("run1", PROMPT)["state"] == "completed"
    first, second = transport.messages()
    assert json.loads(second[2])["model"] == MODEL
    assert first[2] == second[2]  # the same request, byte for byte
    # Every request, count_tokens included, named the configured model and nothing else.
    assert set(transport.models) == {MODEL}
    (call,) = store.model_calls("run1")
    assert call["tier"] == "sonnet"
    assert call["model_configured"] == call["model_reported"] == QUALIFIED
    assert supervisor.profile["model"]["id"] == MODEL
    # The rejected attempt gave its reservation back; only the accepted call settled.
    statuses = sorted(r["status"] for r in world.rows("SELECT * FROM model_spend_calls"))
    assert statuses == ["released", "settled"]
    captured = capsys.readouterr()
    assert_no_secrets(store, tmp_path, extra=control_dump(world) + captured.out + captured.err)


# -- (R3) caps hold before the network -------------------------------------------------------


def test_request_max_tokens_is_the_profile_output_cap(world, tmp_path, creds):
    assert world.limits(per_call=5, daily=20).status_code == 200
    transport = Transport(ok())
    store, supervisor, _ = rig(world, tmp_path, creds, transport)
    assert supervisor.tick("run1", PROMPT)["state"] == "completed"
    profile = make_profile()
    cap = profile["limits"]["max_output_tokens_per_call"]
    assert cap != profile["max_tokens"]  # the per-call cap, not the run's token budget
    ((_, _, body),) = transport.messages()
    assert json.loads(body)["max_tokens"] == cap
    counted = [json.loads(b) for url, _, b in transport.sent if url == COUNT_URL]
    assert counted and all("max_tokens" not in b for b in counted)


# Call 1 uses its whole output cap, so it settles at the worst case: 1.53 cents. Call 2's worst
# case (another 1.53) then crosses a 2-cent day, though either call alone fits.
FULL = ok(output_tokens=1000)
FULL_CENTS = (100 * 300 + 1000 * 1500) / 1e6


def _two_calls_one_day(world, tmp_path, creds, limits):
    transport = Transport(FULL, ok())
    store, first, clock = rig(world, tmp_path, creds, transport, limits=limits)
    assert first.tick("run1", PROMPT)["state"] == "completed"
    (call,) = store.model_calls("run1")
    assert call["cost"] == pytest.approx(FULL_CENTS) and call["spend_ledger"]["status"] == "settled"
    store.close()
    store, second, _ = rig(
        world, tmp_path, creds, transport, run_id="run2", clock=clock, limits=limits
    )
    row = second.tick("run2", PROMPT)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "model_call_cap_exceeded")
    # Call 2 counted its tokens (free) and was refused before /v1/messages.
    assert len(transport.messages()) == 1
    assert [urlsplit(u).path for u, *_ in transport.sent[2:]] == ["/v1/messages/count_tokens"]
    assert store.effect("run2", "prompt") == "not_sent"
    assert store.model_calls("run2") == []
    # Only call 1 is on the control plane's ledger, settled; nothing is left reserved.
    (settled,) = world.rows("SELECT run_id, status FROM model_spend_calls")
    assert (settled["run_id"], settled["status"]) == ("run1", "settled")
    assert world.read().json()["open_reservations"] == []
    assert_no_secrets(store, tmp_path, run_id="run2", extra=control_dump(world))
    return store


def test_profile_daily_cost_cap_refuses_the_next_call_before_the_network(world, tmp_path, creds):
    assert world.limits(per_call=5, daily=20).status_code == 200
    limits = dict(make_profile()["limits"], daily_token_cap=10**6, daily_cost_cap_cents=2)
    store = _two_calls_one_day(world, tmp_path, creds, limits)
    # Refused by the profile's own daily cap: the control plane was never asked.
    assert store.spend_rows("run2") == []
    assert world.events("model_spend.refused") == []
    assert store.daily_usage("2026-11-22")["cost_cents"] == pytest.approx(FULL_CENTS)


def test_control_plane_daily_cap_refuses_the_next_call_before_the_network(world, tmp_path, creds):
    assert world.limits(per_call=2, daily=2).status_code == 200
    limits = dict(make_profile()["limits"], daily_token_cap=10**6)  # profile day: 100 cents
    store = _two_calls_one_day(world, tmp_path, creds, limits)
    # Refused by the control plane's ledger: a refusal event, a denied local row, nothing held.
    (refused,) = world.events("model_spend.refused")
    assert refused["code"] == "model_spend_cap_exceeded" and refused["run_id"] == "run2"
    assert [r["state"] for r in store.spend_rows("run2")] == ["denied"]


# -- (R6) a missing key fails closed through the supervisor ---------------------------------


def test_missing_key_fails_closed_before_any_request(world, tmp_path, creds, monkeypatch, capsys):
    assert world.limits(per_call=5, daily=20).status_code == 200
    transport = Transport(ok())
    store, supervisor, _ = rig(world, tmp_path, creds, transport)
    (creds / "anthropic-api-key").unlink()
    monkeypatch.setenv("ANTHROPIC_API_KEY", DECOY)  # must never stand in for the file

    row = supervisor.tick("run1", PROMPT)
    assert (row["state"], row["reason"]) == ("stop_acknowledged", "model_credential_unavailable")
    assert transport.sent == []  # no network at all, not even a token count
    assert store.model_calls("run1") == []
    assert store.spend_rows("run1") == []
    assert store.daily_usage("2026-11-22") == {"tokens": 0, "cost_cents": 0.0}
    assert world.rows("SELECT * FROM model_spend_calls") == []
    assert world.events("model_spend.reserved") == world.events("model_spend.refused") == []
    # A later tick does not retry with anything else.
    assert supervisor.tick("run1", PROMPT)["reason"] == "model_credential_unavailable"
    assert transport.sent == []
    captured = capsys.readouterr()
    durable = journal_bytes(tmp_path) + "\n".join(store.db.iterdump()).encode()
    assert DECOY.encode() not in durable
    for text in (json.dumps(row), control_dump(world), captured.out, captured.err):
        assert DECOY not in text
    assert_no_secrets(store, tmp_path, extra=control_dump(world) + captured.out + captured.err)


# -- (R1) rr.main against the real model-spend routes ---------------------------------------


def test_main_settles_on_the_real_control_ledger(world, tmp_path, creds, monkeypatch, capsys):
    """``rr.main`` end to end; the model-spend routes are the real in-process control plane.
    Only the grant and holdout reads are answered locally (World has no reasoning grant)."""
    from test_anthropic_runtime_integration import grant

    assert world.limits(per_call=5, daily=20).status_code == 200
    profile = make_profile(
        prompt_sha256=hashlib.sha256(PROMPT.encode()).hexdigest(),
        control_url="http://127.0.0.1:1",
        agent_token_env="T_AGENT",
        model_spend={"project_id": world.project},
    )
    path = private(tmp_path / "profile.json", json.dumps(profile))
    prompt = private(tmp_path / "prompt.txt", PROMPT)
    monkeypatch.setenv("T_AGENT", "SENTINEL-AGENT-TOKEN")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))
    transport = Transport(ok())
    real = Control(world)

    class Endpoint:
        def __init__(self, *args, **kwargs):
            pass

        def call(self, method, path, body=None, key=None):
            if path.startswith("/model-spend/"):
                return real.call(method, path, body, key)
            return {**grant(), "project_id": world.project}

    monkeypatch.setattr(rr, "JsonEndpoint", Endpoint)
    monkeypatch.setattr(
        rr,
        "AnthropicMessagesReasoning",
        lambda p, *, credentials_dir: AnthropicMessagesReasoning(
            p, credentials_dir=credentials_dir, transport=transport
        ),
    )
    code = rr.main(
        ["run", "--profile", str(path), "--state-dir", str(tmp_path / "state"),
         "--prompt-file", str(prompt)]
    )  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0
    receipt = json.loads(out)
    (call,) = receipt["model_calls"]
    (row,) = world.rows("SELECT * FROM model_spend_calls")
    assert row["status"] == "settled" and row["id"] == call["spend_ledger"]["call_id"]
    assert row["settled_micro"] == call["spend_ledger"]["settled_micro_cents"]
    assert world.read().json()["today"]["calls"] == 1
    for secret in (API_KEY, PROMPT, COMPLETION, "SENTINEL-AGENT-TOKEN"):
        assert secret not in out + control_dump(world)
        assert secret.encode() not in journal_bytes(tmp_path)
    assert API_KEY not in json.dumps(dict(os.environ))
