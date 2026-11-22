"""A missing mcp extra fails the MCP tests loudly unless the run says it may skip them."""

from __future__ import annotations

import pytest
from mcp_required import FIX, require_mcp


def test_a_missing_extra_fails_loudly_and_an_explicit_allowance_skips(monkeypatch):
    monkeypatch.setattr("mcp_required.mcp_missing", lambda: True)
    monkeypatch.delenv("TESSERA_ALLOW_MISSING_MCP", raising=False)
    with pytest.raises(pytest.fail.Exception) as failure:
        require_mcp()
    assert "uv sync --locked --extra mcp" in str(failure.value) and FIX in str(failure.value)
    monkeypatch.setenv("TESSERA_ALLOW_MISSING_MCP", "1")
    with pytest.raises(pytest.skip.Exception) as skipped:
        require_mcp()
    assert "TESSERA_ALLOW_MISSING_MCP" in str(skipped.value)
    monkeypatch.setenv("TESSERA_ALLOW_MISSING_MCP", "0")
    with pytest.raises(pytest.fail.Exception):
        require_mcp()


def test_an_installed_extra_passes_silently(monkeypatch):
    monkeypatch.setattr("mcp_required.mcp_missing", lambda: False)
    require_mcp()
