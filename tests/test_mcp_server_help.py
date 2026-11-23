"""The --approval-tool help names every tool the flag registers."""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")

from tessera_mcp import server  # noqa: E402


def test_approval_tool_help_names_both_tools(capsys):
    with pytest.raises(SystemExit) as exit_info:
        server.main(["--help"])
    assert exit_info.value.code == 0
    out = " ".join(capsys.readouterr().out.split())
    start = out.index("--approval-tool register")
    section = out[start : out.index("--provider-registration-tools", start)]
    assert "tessera_approve_campaign" in section
    assert "tessera_record_gate_decision" in section
    assert "separate operator server" in section
