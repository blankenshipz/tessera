"""One end-to-end call of the on-platform agent loop, recorded and debited (stub transport).

Acceptance evidence for first-call readiness: a real supervisor and journal, the real control
routes in-process and a recorded Messages API transport. No network, no real key: the only
"key" is a clearly synthetic marker inside a temporary credentials directory.

Proves that one billed call (1) is reserved on the ledger before anything is sent, (2) writes
exactly one model-call receipt, (3) settles the same cost into the control plane's ledger bound
to that receipt, and (4) leaves no key, prompt or completion text in any receipt, ledger row,
event or output. Also the operator path: a call refused before the transport because no limit
is set, then a new run that succeeds once a human sets one.
"""

# ruff: noqa: F811
from __future__ import annotations

import hashlib
import json
import math
from urllib.parse import urlsplit

import pytest
from test_anthropic_runtime_integration import (  # noqa: F401
    API_KEY,
    COMPLETION,
    MODEL,
    PROMPT,
    Transport,
    assert_no_secrets,
    creds,
    make_profile,
    ok,
    setup,
)
from test_model_spend_api import World
from test_model_spend_integration import Control

from tessera_agent.model_spend_client import SpendLedger

QUALIFIED = "anthropic/" + MODEL
MICRO = 1_000_000


@pytest.fixture
def world(tmp_path):
    (tmp_path / "control").mkdir()
    return World(tmp_path / "control")


def _dir(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    return path


def rig(world, tmp_path, creds, *replies, run_id="run1"):
    """A real supervisor + journal whose ledger talks to the in-process control plane."""
    transport = Transport(*replies)
    profile = make_profile(run_id=run_id, model_spend={"project_id": world.project})
    store, supervisor, _, _ = setup(tmp_path, creds, transport, profile=profile)
    supervisor.spend = SpendLedger(Control(world), None, "anthropic", QUALIFIED)
    return store, supervisor, transport


def stored_digest(call):
    """The digest the supervisor binds to a settlement: the receipt row as stored, without the
    settlement answer and with the final output digest still unset (annotated afterwards)."""
    stored = {
        k: v for k, v in call.items() if k not in {"spend_ledger", "seq", "observed_at", "status"}
    } | {"output_sha256": None}
    return hashlib.sha256(
        json.dumps(stored, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_one_call_writes_a_receipt_and_debits_the_ledger(world, tmp_path, creds, capsys):
    assert world.limits(per_call=5, daily=20).status_code == 200
    store, supervisor, transport = rig(world, tmp_path, creds, ok())

    assert supervisor.tick("run1", PROMPT)["state"] == "completed"

    # (a) exactly one receipt row, from the pinned prices, with the settlement on it.
    (call,) = store.model_calls("run1")
    assert call["model_configured"] == QUALIFIED and call["model_reported"] == QUALIFIED
    assert call["cost_basis"] == "computed_from_pinned_prices"
    assert call["tokens"]["input"] == 100 and call["tokens"]["output"] == 50
    # 100 input tokens at 300 and 50 output tokens at 1500 cents per million.
    assert math.isclose(call["cost"], 100 * 300 / 1e6 + 50 * 1500 / 1e6, abs_tol=1e-9)
    assert call["output_sha256"] == hashlib.sha256(COMPLETION.encode()).hexdigest()
    ledger = call["spend_ledger"]
    assert ledger["status"] == "settled" and ledger["call_id"]
    assert ledger["settled_micro_cents"] == math.ceil(call["cost"] * MICRO) > 0

    # (b) the control plane's ledger agrees: one settled call, nothing open, same cost.
    today = world.read().json()
    assert today["today"]["calls"] == 1 and today["today"]["reserved_cents"] == 0
    assert math.isclose(today["today"]["settled_cents"], call["cost"], abs_tol=1 / MICRO)
    assert today["open_reservations"] == []
    (row,) = world.rows("SELECT * FROM model_spend_calls")
    assert row["id"] == ledger["call_id"] and row["status"] == "settled"
    assert row["settled_micro"] == ledger["settled_micro_cents"]
    assert row["provider_id"] == "anthropic" and row["run_id"] == "run1"
    # The settlement names the receipt it belongs to.
    assert row["receipt_sha256"] == stored_digest(call)
    (reserved,) = world.events("model_spend.reserved")
    (settled,) = world.events("model_spend.settled")
    assert reserved["call_id"] == settled["call_id"] == ledger["call_id"]
    assert world.events("model_spend.refused") == []

    # (d) the transport saw one count and one message call, both to the allowlisted host.
    assert len(transport.messages()) == 1
    assert {urlsplit(url).netloc for url, *_ in transport.sent} == {"api.anthropic.com"}
    assert all(sent[1].get("x-api-key") == API_KEY for sent in transport.sent)  # in-transport only

    # (c) no key, prompt or completion anywhere durable or printed.
    control_dump = "\n".join(
        json.dumps(dict(r))
        for table in ("model_spend_calls", "events")
        for r in world.rows(f"SELECT * FROM {table}")
    )
    captured = capsys.readouterr()
    assert_no_secrets(store, tmp_path, extra=control_dump + captured.out + captured.err)


def test_a_call_is_refused_before_the_transport_until_a_human_sets_a_limit(
    world, tmp_path, creds, capsys
):
    # The first-call operator path: nothing is set, so the call is not made.
    store, supervisor, transport = rig(world, _dir(tmp_path, "first"), creds, ok())
    row = supervisor.tick("run1", PROMPT)
    assert row["state"] != "completed" and row["reason"] == "model_spend_not_configured"
    # No billed request: the client only counts tokens (a free, allowlisted call) to price the
    # worst case, then the ledger refuses before /v1/messages is ever sent.
    assert transport.messages() == []
    assert {urlsplit(url).path for url, *_ in transport.sent} <= {"/v1/messages/count_tokens"}
    assert store.model_calls("run1") == []
    assert world.rows("SELECT * FROM model_spend_calls") == []
    (refused,) = world.events("model_spend.refused")
    assert refused["code"] == "model_spend_not_configured"

    # A human sets the limits; a new run is evaluated afresh and completes, debiting the ledger.
    assert world.limits(per_call=5, daily=20).status_code == 200
    store2, supervisor2, transport2 = rig(
        world, _dir(tmp_path, "second"), creds, ok(), run_id="run2"
    )
    assert supervisor2.tick("run2", PROMPT)["state"] == "completed"
    assert len(transport2.messages()) == 1 and len(store2.model_calls("run2")) == 1
    (settled,) = world.rows("SELECT run_id, status FROM model_spend_calls")
    assert (settled["run_id"], settled["status"]) == ("run2", "settled")
    assert world.read().json()["today"]["calls"] == 1
