"""The MCP tests need the ``mcp`` extra; a missing extra is a failure, not a quiet skip.

A plain ``pytest`` on an environment without the extra used to skip every MCP test and look
green while covering none of the MCP tools. ``require_mcp()`` fails the module with the fix
instead. A run that knowingly has no extra (a minimal platform check) sets
``TESSERA_ALLOW_MISSING_MCP=1`` to skip them, and the skip reason says so.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

FIX = "install the mcp extra: uv sync --locked --extra mcp (CONTRIBUTING.md)"


def mcp_missing() -> bool:
    return importlib.util.find_spec("mcp") is None


def require_mcp() -> None:
    """Module level: fail loudly when ``mcp`` is absent unless the run says it may be."""
    if not mcp_missing():
        return
    if os.environ.get("TESSERA_ALLOW_MISSING_MCP") == "1":
        pytest.skip(
            "mcp extra missing; skipped because TESSERA_ALLOW_MISSING_MCP=1",
            allow_module_level=True,
        )
    pytest.fail(
        f"The mcp extra is not installed, so the MCP tests cannot run: {FIX}", pytrace=False
    )
