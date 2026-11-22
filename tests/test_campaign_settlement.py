"""The single campaign settlement routine (ADR 0041 section 3 idempotency contract)."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from tessera_control.campaign_settlement import (
    BASES,
    SettlementRefused,
    apply_late_charge,
    confirm_settlement,
    settle_attempt,
)
from tessera_control.store import Conflict, canonical_digest

NOW = "2026-01-01T00:00:00Z"
SRC = Path(__file__).resolve().parents[1] / "src" / "tessera_control"


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """CREATE TABLE campaigns (id TEXT PRIMARY KEY, held_cents INTEGER, settled_cents INTEGER);
        CREATE TABLE campaign_stage_attempts (id TEXT PRIMARY KEY, campaign_id TEXT,
        hold_cents INTEGER, settled_cents INTEGER, settlement_basis TEXT,
        settlement_request_digest TEXT, revision INTEGER, updated_at TEXT);
        INSERT INTO campaigns VALUES ('c1', 50, 0);
        INSERT INTO campaign_stage_attempts VALUES ('a1','c1',50,NULL,NULL,NULL,1,'t');"""
    )
    yield connection
    connection.close()


def _digest(cents, basis):
    return canonical_digest(["a1", cents, basis])


def _settle(connection, cents=30, basis="human_charge", digest=None, on_settled=None):
    return settle_attempt(
        connection,
        "a1",
        cents=cents,
        basis=basis,
        request_digest=digest or _digest(cents, basis),
        now=NOW,
        on_settled=on_settled,
    )


def _state(connection):
    attempt = dict(connection.execute("SELECT * FROM campaign_stage_attempts").fetchone())
    campaign = dict(connection.execute("SELECT * FROM campaigns").fetchone())
    return attempt, campaign


def test_settle_moves_the_hold_and_runs_the_hook_in_the_transaction(db):
    seen = []
    result = _settle(db, on_settled=lambda conn, aid, cents, stamp: seen.append((aid, cents)))
    attempt, campaign = _state(db)
    assert result["replayed"] is False and result["settled_cents"] == 30
    assert (attempt["settled_cents"], attempt["settlement_basis"]) == (30, "human_charge")
    assert attempt["settlement_request_digest"] == _digest(30, "human_charge")
    assert (campaign["held_cents"], campaign["settled_cents"]) == (0, 30)
    assert seen == [("a1", 30)]


def test_the_same_digest_returns_the_stored_settlement_and_writes_nothing(db):
    _settle(db)
    before = _state(db)
    hooks = []
    again = _settle(db, on_settled=lambda *a: hooks.append(a))
    assert again["replayed"] is True and again["settled_cents"] == 30
    assert _state(db) == before and hooks == []


def test_a_different_digest_conflicts_and_writes_nothing(db):
    _settle(db)
    before = _state(db)
    with pytest.raises(Conflict):
        _settle(db, cents=40)
    with pytest.raises(Conflict):
        _settle(db, digest="0" * 64)
    assert _state(db) == before


def test_zero_only_bases_refuse_a_nonzero_amount(db):
    for basis in ("local_zero", "unclaimed_zero"):
        with pytest.raises(SettlementRefused):
            _settle(db, cents=1, basis=basis)
    assert _state(db)[0]["settled_cents"] is None
    assert _settle(db, cents=0, basis="unclaimed_zero")["settled_cents"] == 0


def test_an_unknown_basis_is_refused_and_not_stored(db):
    assert "made_up" not in BASES
    with pytest.raises(SettlementRefused):
        _settle(db, basis="made_up")
    assert _state(db)[0]["settlement_basis"] is None


def test_a_missing_attempt_is_not_found(db):
    from tessera_control.store import NotFound

    with pytest.raises(NotFound):
        settle_attempt(db, "nope", cents=0, basis="unclaimed_zero", request_digest="d", now=NOW)


def test_a_late_charge_attaches_without_reopening(db):
    _settle(db, cents=50, basis="guardian_bounded_hold")
    digest = _state(db)[0]["settlement_request_digest"]
    apply_late_charge(db, "a1", delta_cents=20, charged_total=70, now=NOW)
    attempt, campaign = _state(db)
    assert (attempt["settled_cents"], attempt["settlement_basis"]) == (70, "human_charge")
    assert attempt["settlement_request_digest"] == digest  # never reopened
    assert (campaign["held_cents"], campaign["settled_cents"]) == (0, 70)
    # The original request still replays as itself and still moves no money.
    again = _settle(db, cents=50, basis="guardian_bounded_hold")
    assert again["replayed"] is True and _state(db) == (attempt, campaign)
    with pytest.raises(SettlementRefused):
        apply_late_charge(db, "a1", delta_cents=0, charged_total=70, now=NOW)


def test_a_late_charge_needs_a_settlement(db):
    with pytest.raises(Conflict):
        apply_late_charge(db, "a1", delta_cents=5, charged_total=5, now=NOW)


def test_confirm_relabels_only_a_bounded_hold(db):
    _settle(db, cents=50, basis="guardian_bounded_hold")
    assert confirm_settlement(db, "a1", charged_total=50, now=NOW) is True
    attempt, campaign = _state(db)
    assert (attempt["settled_cents"], attempt["settlement_basis"]) == (50, "human_charge")
    assert campaign["settled_cents"] == 50
    assert confirm_settlement(db, "a1", charged_total=50, now=NOW) is False


# -- single writer ---------------------------------------------------------------------

_MONEY = re.compile(r"\b(settled_cents|held_cents|settlement_basis)\b")
_UPDATE = re.compile(
    r"UPDATE\s+(campaigns|campaign_stage_attempts)\b(.*?)(?:WHERE|\"\"\"|\"\))", re.S
)
_KWARG = re.compile(r"\b(settled_cents|held_cents|settlement_basis)\s*=[^=]")

# Precisely what may remain outside the settlement module: the hold reservation, which
# adds a new attempt's hold before any settlement exists.
ALLOWED = {("campaign_runner.py", "campaigns", "held_cents")}


def test_only_the_settlement_module_moves_campaign_money():
    found = set()
    for path in sorted(SRC.glob("*.py")):
        if path.name == "campaign_settlement.py":
            continue
        text = path.read_text()
        for match in _UPDATE.finditer(text):
            for column in set(_MONEY.findall(match.group(2))):
                found.add((path.name, match.group(1), column))
        # No generic field write (``_set_attempt``/``_touch``) may carry a money column.
        if path.name.startswith("campaign_"):
            for call in re.finditer(r"_(?:set_attempt|touch)\((.*?)\)\n", text, re.S):
                assert not _KWARG.search(call.group(1)), (path.name, call.group(0)[:80])
    assert found == ALLOWED, found ^ ALLOWED
