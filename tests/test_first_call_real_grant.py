"""``rr.main`` against the real in-process control plane, grant and holdout reads included.

Every control read the reasoning runtime makes (``/agent-authority-grants/{id}``, the project's
holdout claims, ``/model-spend/*`` and provenance) is answered by the real routes, for a real
agent authority grant a human issued; only the Messages API is a recorded stub transport. No
network and no real key: the only "key" is the synthetic marker in the temporary credentials
directory, and a decoy ``ANTHROPIC_API_KEY`` that must never be used.
"""

# ruff: noqa: F811
from __future__ import annotations

import hashlib
import json
import math

import pytest
from reasoning_grant_world import ReasoningGrant
from test_agent_authority import prepared  # noqa: F401
from test_anthropic_runtime_integration import (  # noqa: F401
    API_KEY,
    COMPLETION,
    MODEL,
    PROMPT,
    Transport,
    creds,
    make_profile,
    ok,
    private,
)

import tessera_agent.remote_reasoning_runtime as rr
from tessera_agent.anthropic_messages import AnthropicMessagesReasoning
from tessera_agent.model_spend_client import NOT_CONFIGURED

MICRO = 1_000_000
DECOY_ENV_KEY = "sk-ant-DECOY-ENVIRONMENT-KEY-never-used"
GRANT = "/agent-authority-grants/"


@pytest.fixture
def grant(prepared):
    return ReasoningGrant(prepared, MODEL)


def run(tmp_path, grant, creds, monkeypatch, capsys, transport, run_id="run1", **over):
    """``rr.main run`` with a profile carrying the real grant's values (``over`` replaces some)."""
    profile = make_profile(
        run_id=run_id,
        prompt_sha256=hashlib.sha256(PROMPT.encode()).hexdigest(),
        control_url="http://127.0.0.1:1",
        agent_token_env="T_AGENT",
        **(grant.profile_fields() | over),
    )
    root = tmp_path / run_id
    root.mkdir()
    path = private(root / "profile.json", json.dumps(profile))
    prompt = private(root / "prompt.txt", PROMPT)
    seen = []
    monkeypatch.setenv("T_AGENT", grant.token)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))
    monkeypatch.setenv("ANTHROPIC_API_KEY", DECOY_ENV_KEY)
    monkeypatch.setattr(rr, "JsonEndpoint", grant.endpoint(seen))
    monkeypatch.setattr(rr, "ProvenanceClient", grant.provenance())
    monkeypatch.setattr(
        rr,
        "AnthropicMessagesReasoning",
        lambda p, *, credentials_dir: AnthropicMessagesReasoning(
            p, credentials_dir=credentials_dir, transport=transport
        ),
    )
    monkeypatch.setattr(rr, "OpenCodeV2Reasoning", lambda *a, **k: pytest.fail("OpenCode used"))
    monkeypatch.setattr(rr.signal, "signal", lambda *a: None)  # no handler outlives the test
    state = root / "state"
    code = rr.main(
        ["run", "--profile", str(path), "--state-dir", str(state), "--prompt-file", str(prompt)]
    )
    return code, json.loads(capsys.readouterr().out), seen


def test_profile_carries_exactly_the_issued_grant(grant):
    read = grant.read()
    fields = grant.profile_fields()
    assert (read["id"], read["revision"], read["epoch"]) == (
        fields["grant_id"],
        fields["revision"],
        fields["epoch"],
    )
    assert read["bounds"]["subject_credential_id"] == fields["subject_credential_id"]
    assert read["project_id"] == fields["project_id"] == fields["model_spend"]["project_id"]
    assert read["bounds"]["model"] == MODEL
    assert (read["status"], read["effective_status"]) == ("active", "active")
    assert read["remaining_budget_cents"] == 100


def test_first_call_reads_the_real_grant_and_holdout_and_settles_on_the_ledger(
    grant, tmp_path, creds, monkeypatch, capsys
):
    grant.limits(per_call=5, daily=20)
    transport = Transport(ok())
    code, receipt, seen = run(tmp_path, grant, creds, monkeypatch, capsys, transport)
    assert code == 0 and receipt["state"] == "completed"

    # The fence read the real grant, the guard read the real holdout claims: all answered 200.
    assert ("GET", GRANT + grant.issued["id"], 200) in seen
    holdout = f"/projects/{grant.project}/holdout-claims"
    assert ("GET", holdout, 200) in seen
    assert ("POST", "/model-spend/reservations", 200) in seen
    assert all(status < 400 for _, _, status in seen)

    # One receipt, settled on the real ledger under the same call id and cost.
    (call,) = receipt["model_calls"]
    assert call["cost_basis"] == "computed_from_pinned_prices"
    assert math.isclose(call["cost"], (100 * 300 + 50 * 1500) / 1e6, abs_tol=1e-9)
    assert call["output_sha256"] == hashlib.sha256(COMPLETION.encode()).hexdigest()
    ledger = call["spend_ledger"]
    assert ledger["status"] == "settled"
    (row,) = grant.rows("SELECT * FROM model_spend_calls")
    assert (row["id"], row["status"], row["run_id"]) == (ledger["call_id"], "settled", "run1")
    assert row["project_id"] == grant.project
    assert row["settled_micro"] == ledger["settled_micro_cents"] == math.ceil(call["cost"] * MICRO)
    assert [r["state"] for r in receipt["spend_ledger"]] == ["settled"]

    after = grant.read()
    assert (after["status"], after["revision"], after["epoch"]) == ("active", 1, 1)
    assert receipt["cost"] == pytest.approx(call["cost"])
    # The grant paid for the call: its remaining budget fell by exactly the settled amount.
    assert after["remaining_budget_cents"] == pytest.approx(100 - row["settled_micro"] / MICRO)

    # The key from the credentials directory went only to the stub; the decoy never did.
    assert len(transport.messages()) == 1
    for _, headers, body in transport.sent:
        assert headers["x-api-key"] == API_KEY
        assert DECOY_ENV_KEY not in json.dumps(headers) + body
    out = json.dumps(receipt)
    for secret in (API_KEY, PROMPT, COMPLETION, grant.token, DECOY_ENV_KEY):
        assert secret not in out


def test_grant_budget_is_consumed_by_the_receipted_cost(
    grant, tmp_path, creds, monkeypatch, capsys
):
    grant.limits(per_call=5, daily=20)
    before = grant.read()["remaining_budget_cents"]
    code, receipt, _ = run(tmp_path, grant, creds, monkeypatch, capsys, Transport(ok()))
    assert code == 0
    (call,) = receipt["model_calls"]
    assert grant.read()["remaining_budget_cents"] == pytest.approx(before - call["cost"])


def test_a_grant_revoked_by_the_human_refuses_the_next_run_before_the_transport(
    grant, tmp_path, creds, monkeypatch, capsys
):
    grant.limits(per_call=5, daily=20)
    first = Transport(ok())
    code, receipt, _ = run(tmp_path, grant, creds, monkeypatch, capsys, first)
    assert code == 0 and len(first.messages()) == 1

    revoked = grant.revoke()
    assert (revoked["status"], revoked["revision"], revoked["epoch"]) == ("revoked", 2, 2)

    # A run still holding the issued revision/epoch: revoking advanced both, so the fence stops
    # on the changed revision before it reaches the status check.
    second = Transport(ok())
    code, receipt, seen = run(tmp_path, grant, creds, monkeypatch, capsys, second, run_id="run2")
    assert code == 2 and receipt["reason"] == "grant_revision_changed"
    assert ("GET", GRANT + grant.issued["id"], 200) in seen
    assert second.sent == [] and receipt["model_calls"] == []

    # A run whose profile names the post-revocation revision/epoch is refused as revoked.
    third = Transport(ok())
    code, receipt, _ = run(
        tmp_path, grant, creds, monkeypatch, capsys, third, run_id="run3", revision=2, epoch=2
    )
    assert code == 2 and receipt["reason"] == "grant_revoked"
    assert third.sent == [] and receipt["model_calls"] == []
    # Only the first run reached the ledger.
    assert [r["run_id"] for r in grant.rows("SELECT run_id FROM model_spend_calls")] == ["run1"]


def test_a_grant_for_another_project_than_the_spend_project_is_refused(
    grant, tmp_path, creds, monkeypatch, capsys
):
    grant.limits(per_call=5, daily=20)
    transport = Transport(ok())
    code, receipt, _ = run(
        tmp_path,
        grant,
        creds,
        monkeypatch,
        capsys,
        transport,
        model_spend={"project_id": "project_elsewhere"},
    )
    assert code == 2
    assert receipt["reason"] == NOT_CONFIGURED
    assert transport.sent == [] and receipt["model_calls"] == []
    assert grant.rows("SELECT * FROM model_spend_calls") == []


def test_an_exhausted_grant_budget_is_refused_before_the_network(
    grant, tmp_path, creds, monkeypatch, capsys
):
    grant.limits(per_call=5, daily=20)
    grant.delegate_remaining()  # the agent hands the whole budget to a child grant
    read = grant.read()
    assert read["remaining_budget_cents"] == 0 and read["status"] == "active"
    transport = Transport(ok())
    code, receipt, seen = run(tmp_path, grant, creds, monkeypatch, capsys, transport)
    assert code == 2 and receipt["reason"] == "budget_exhausted"
    assert ("GET", GRANT + grant.issued["id"], 200) in seen
    assert transport.sent == [] and receipt["model_calls"] == []
    assert grant.rows("SELECT * FROM model_spend_calls") == []


# -- the grant's budget is spent by the ledger (ADR 0142) -------------------------------------
# One run's worst case on the default profile is 1.53 cents (1_530_000 micro-cents).

WORST = 1.53


def reserve(grant, worst, *, seq=1, run_id="direct", grant_id="issued", headers=None):
    body = {
        "project_id": grant.project,
        "run_id": run_id,
        "seq": seq,
        "provider_id": "anthropic",
        "model": "m",
        "worst_case_cents": worst,
    }
    if grant_id is not None:
        body["grant_id"] = grant.issued["id"] if grant_id == "issued" else grant_id
    response = grant.client.post(
        "/model-spend/reservations",
        json=body,
        headers={**(headers or grant.agent), **grant._key()},
    )
    return response


def test_a_budget_below_the_worst_case_is_refused_with_no_call_and_no_ledger_row(
    prepared, tmp_path, creds, monkeypatch, capsys
):
    grant = ReasoningGrant(prepared, MODEL, budget_cents=1)
    grant.limits(per_call=5, daily=20)
    transport = Transport(ok())
    code, receipt, _ = run(tmp_path, grant, creds, monkeypatch, capsys, transport)
    # No billed message was sent (a token-count request is free and precedes the reservation).
    assert code != 0 and transport.messages() == [] and receipt["model_calls"] == []
    assert grant.rows("SELECT * FROM model_spend_calls") == []
    assert grant.read()["remaining_budget_cents"] == 1


def test_the_ledger_refuses_a_reservation_the_grant_cannot_cover(prepared):
    grant = ReasoningGrant(prepared, MODEL, budget_cents=1)
    grant.limits(per_call=5, daily=20)
    refused = reserve(grant, WORST)
    assert refused.status_code == 200
    answer = refused.json()
    assert answer["allowed"] is False and answer["code"] == "grant_budget_exceeded"
    assert answer["remaining_cents"] == 1
    assert grant.rows("SELECT * FROM model_spend_calls") == []
    assert grant.rows("SELECT * FROM model_spend_grant_links") == []
    # A smaller call fits and is debited by its reservation.
    ok_answer = reserve(grant, 0.4, seq=2).json()
    assert ok_answer["allowed"] is True
    assert grant.read()["remaining_budget_cents"] == pytest.approx(0.6)
    # The next reservation sees the live remaining budget, not the issued one.
    assert reserve(grant, 0.7, seq=3).json()["code"] == "grant_budget_exceeded"


def test_a_budget_just_large_enough_is_admitted_and_debited(
    prepared, tmp_path, creds, monkeypatch, capsys
):
    grant = ReasoningGrant(prepared, MODEL, budget_cents=2)
    grant.limits(per_call=5, daily=20)
    transport = Transport(ok())
    code, receipt, _ = run(tmp_path, grant, creds, monkeypatch, capsys, transport)
    assert code == 0 and len(transport.messages()) == 1
    (row,) = grant.rows("SELECT * FROM model_spend_calls")
    assert row["status"] == "settled"
    assert grant.read()["remaining_budget_cents"] == pytest.approx(2 - row["settled_micro"] / MICRO)
    assert len(grant.rows("SELECT * FROM model_spend_grant_links")) == 1


def test_sequential_runs_cannot_bill_past_the_grant(prepared, tmp_path, creds, monkeypatch, capsys):
    grant = ReasoningGrant(prepared, MODEL, budget_cents=2)
    grant.limits(per_call=5, daily=20)
    refused = None
    for n in range(1, 40):
        transport = Transport(ok())
        code, receipt, _ = run(
            tmp_path, grant, creds, monkeypatch, capsys, transport, run_id=f"seq{n}"
        )
        if code != 0:
            refused = (transport, receipt)
            break
    assert refused is not None, "the grant's budget never ran out"
    transport, receipt = refused
    assert transport.messages() == [] and receipt["model_calls"] == []
    rows = grant.rows("SELECT * FROM model_spend_calls")
    assert all(r["status"] == "settled" for r in rows) and len(rows) == n - 1
    assert sum(r["settled_micro"] for r in rows) <= 2 * MICRO
    assert not any(r["run_id"] == f"seq{n}" for r in rows)


def test_a_released_reservation_gives_the_budget_back(prepared):
    grant = ReasoningGrant(prepared, MODEL, budget_cents=2)
    grant.limits(per_call=5, daily=20)
    held = reserve(grant, 1.5).json()
    assert grant.read()["remaining_budget_cents"] == pytest.approx(0.5)
    released = grant.client.post(
        f"/model-spend/reservations/{held['call_id']}/release",
        json={"reason": "not_sent"},
        headers={**grant.agent, **grant._key()},
    )
    assert released.status_code == 200, released.text
    assert grant.read()["remaining_budget_cents"] == 2


def test_a_grant_that_is_not_the_callers_or_is_revoked_cannot_pay(prepared):
    grant = ReasoningGrant(prepared, MODEL, budget_cents=2)
    grant.limits(per_call=5, daily=20)
    assert reserve(grant, 0.1, grant_id="agent_authority_grant_nope").status_code == 404
    assert grant.rows("SELECT * FROM model_spend_calls") == []
    grant.revoke()
    answer = reserve(grant, 0.1, seq=2).json()
    assert answer["allowed"] is False and answer["code"] == "grant_not_active"
    assert grant.rows("SELECT * FROM model_spend_calls") == []


def test_model_spend_is_taken_out_of_what_the_grant_may_delegate(prepared):
    grant = ReasoningGrant(prepared, MODEL, budget_cents=100)
    grant.limits(per_call=5, daily=20)
    assert reserve(grant, 2).json()["allowed"] is True
    child = {**grant.bounds, "delegation_depth": 0, "budget_cents": 99}
    response = grant.client.post(
        f"/agent-authority-grants/{grant.issued['id']}/delegate",
        json={"expected_revision": 1, "expected_epoch": 1, "bounds": child},
        headers={**grant.agent, **grant._key()},
    )
    assert response.status_code == 403
