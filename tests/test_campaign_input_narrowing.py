"""A stage's own send_classes narrows provider input at ticket issue and each pull."""

from __future__ import annotations

import json
import sqlite3

from tessera_control.data_class import campaign_input_narrowing


def _db(spec_json: str | None, operation_id: str | None = "op1") -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """CREATE TABLE campaigns (id TEXT, spec_json TEXT, org_id TEXT, project_id TEXT);
        CREATE TABLE campaign_stage_attempts (campaign_id TEXT, stage_key TEXT, operation_id TEXT,
            grant_id TEXT, release_id TEXT, provider_id TEXT);
        CREATE TABLE external_operations (operation_id TEXT, grant_id TEXT);
        CREATE TABLE execution_grants (id TEXT, snapshot_release_id TEXT);
        CREATE TABLE campaign_approvals (campaign_id TEXT, trust_policy_digest TEXT,
            status TEXT, approved_at TEXT, id TEXT);"""
    )
    if spec_json is not None:
        connection.execute("INSERT INTO campaigns (id, spec_json) VALUES ('c1', ?)", (spec_json,))
        connection.execute(
            "INSERT INTO campaign_stage_attempts (campaign_id, stage_key, operation_id) "
            "VALUES ('c1','s1',?)",
            (operation_id,),
        )
        connection.execute(
            "INSERT INTO campaign_approvals VALUES ('c1','d' ,'active','2026-01-01T00:00:00Z','a1')"
        )
    return connection


def test_operation_outside_a_campaign_is_not_narrowed():
    assert campaign_input_narrowing(_db(None), "op1") == (None, None)


def test_unreadable_campaign_spec_fails_closed_to_public():
    narrowed, digest = campaign_input_narrowing(_db(json.dumps({"bad": 1})), "op1")
    assert narrowed == frozenset({"public"}) and digest == "d"


def test_stage_send_classes_and_approval_digest_are_returned(monkeypatch):
    from types import SimpleNamespace

    import tessera_control.campaign_models as models

    stage = SimpleNamespace(key="s1", send_classes=["public"])
    monkeypatch.setattr(models, "parse_spec_json", lambda text: SimpleNamespace(stages=[stage]))
    assert campaign_input_narrowing(_db("{}"), "op1") == (frozenset({"public"}), "d")
    stage.send_classes = None
    assert campaign_input_narrowing(_db("{}"), "op1") == (None, "d")
