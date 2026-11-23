"""main(): decision-only servers make no live scope read; the default path reads once."""

from __future__ import annotations

import pytest

from tessera_mcp import server as module

RETENTION = frozenset({"retention:read", "retention:apply"})


@pytest.fixture
def harness(tmp_path, monkeypatch):
    reference = tmp_path / "cli.token"
    reference.write_text("synthetic-token")
    reference.chmod(0o600)
    for name, value in {
        "TESSERA_CONTROL_URL": "http://127.0.0.1:8765",
        "TESSERA_AGENT_TOKEN_FILE": str(reference),
        "TESSERA_PROJECT_ID": "proj1",
        "TESSERA_AGENT_DESTINATION": "dest",
    }.items():
        monkeypatch.setenv(name, value)
    built: list = []
    reads: list = []

    class Stub:
        def run(self, transport):
            return None

    monkeypatch.setattr(module, "create_server", lambda client, **kw: built.append(kw) or Stub())
    monkeypatch.setattr(module, "_has_provider_trust_scope", lambda client: False)
    monkeypatch.setattr(module, "_has_model_spend_scope", lambda client: False)

    def set_live(actions):
        def fake(client):
            reads.append(1)
            return frozenset(actions)

        monkeypatch.setattr(module, "_live_actions", fake)

    set_live(RETENTION)
    return built, reads, set_live


@pytest.mark.parametrize(
    "flag",
    [
        "--approval-tool",
        "--review-decision-tool",
        "--person-enrollment-tool",
        "--model-spend-resolve-tool",
    ],
)
def test_each_decision_flag_alone_makes_no_live_read(harness, flag):
    built, reads, _ = harness
    assert module.main([flag]) == 0
    assert reads == []
    assert "retention_actions" not in built[0]
    assert "storage_retention_tool" not in built[0]


def test_default_server_reads_live_scopes_once_and_exposes_held_retention(harness):
    built, reads, set_live = harness
    set_live({"retention:read"})
    assert module.main([]) == 0
    assert reads == [1]
    assert built[0]["retention_actions"] == frozenset({"retention:read"})


def test_default_server_without_retention_scopes_exposes_none(harness):
    built, reads, set_live = harness
    set_live(set())
    assert module.main([]) == 0
    assert reads == [1]
    assert "retention_actions" not in built[0]


def test_explicit_campaign_flag_still_reads_once(harness):
    built, reads, _ = harness
    assert module.main(["--campaign-tools"]) == 0
    assert reads == [1]
    assert built[0]["retention_actions"] == RETENTION


def test_approval_with_storage_retention_reads_and_registers_held_scopes(harness):
    built, reads, set_live = harness
    set_live({"retention:read"})
    assert module.main(["--approval-tool", "--storage-retention-tool"]) == 0
    assert reads == [1]
    assert built[0]["approval_tool"] is True
    assert built[0]["storage_retention_tool"] is True
    assert built[0]["retention_actions"] == frozenset({"retention:read"})
