"""One refusal code for 'no spend limit is configured': model_spend_not_configured."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tessera_agent import model_spend_client, remote_reasoning_runtime

ROOT = Path(__file__).resolve().parents[1]
CODE = "model_spend_not_configured"
OLD_CODE = "model_spend_unconfigured"


def test_the_client_constant_is_the_documented_code() -> None:
    assert model_spend_client.NOT_CONFIGURED == CODE
    assert CODE in (ROOT / "docs/model-spend.md").read_text()


def test_the_control_plane_refuses_with_the_same_code() -> None:
    source = (ROOT / "src/tessera_control/model_spend.py").read_text()
    assert f'"{CODE}"' in source


def test_the_runtime_blocks_with_the_client_constant_everywhere() -> None:
    source = (ROOT / "src/tessera_agent/remote_reasoning_runtime.py").read_text()
    # Every block for a missing limit uses the shared constant, never a second spelling.
    assert "RuntimeBlocked(NOT_CONFIGURED)" in source
    assert remote_reasoning_runtime.NOT_CONFIGURED == CODE


@pytest.mark.parametrize("tree", ["src", "docs", "tests"])
def test_the_old_spelling_is_gone(tree: str) -> None:
    this = Path(__file__).resolve()
    offenders = [
        str(path.relative_to(ROOT))
        for path in (ROOT / tree).rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".md", ".json", ".service", ".toml"}
        and path.resolve() != this
        and OLD_CODE in path.read_text(errors="replace")
    ]
    assert offenders == []


def test_the_documented_revoke_route_exists_in_the_application(tmp_path: Path) -> None:
    from tessera_control.api import create_app
    from tessera_control.store import initialize_state

    documented = re.findall(
        r"`POST (/agent-authority-grants/\{grant_id\}/revoke)`",
        (ROOT / "docs/agent-authority-interface.md").read_text(),
    )
    assert documented == ["/agent-authority-grants/{grant_id}/revoke"]
    state = tmp_path / "state"
    initialize_state(state)
    app = create_app(state)
    # Included routers are listed lazily by newer FastAPI, so read the published schema.
    paths = app.openapi()["paths"]
    assert "post" in paths[documented[0]]
