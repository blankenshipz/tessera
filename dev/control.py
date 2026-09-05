"""Compose-only startup; host access is restricted by compose.yaml's port mapping."""

from pathlib import Path

import uvicorn

from tessera_control.api import create_app
from tessera_control.store import initialize_state

if __name__ == "__main__":
    state = Path("/state/control")
    if not state.exists():
        initialize_state(state)
    uvicorn.run(create_app(state), host="0.0.0.0", port=8765)
