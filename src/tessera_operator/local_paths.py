"""Operator-local locations, taken from the environment with portable defaults.

Nothing here names a particular user or machine. Override per deployment with
``KUBECONFIG``, ``TESSERA_REPOSITORY``, ``TESSERA_STATE_DIR`` and ``DOCKER_CONFIG``.
"""

from __future__ import annotations

import os
from pathlib import Path


def _path(variable: str, default: Path) -> Path:
    value = os.environ.get(variable)
    if value and variable == "KUBECONFIG":
        value = value.split(os.pathsep)[0]  # a kubeconfig list is not one fixed target: first entry
    return Path(value).expanduser() if value else default


HOME = Path.home()
KUBECONFIG = _path("KUBECONFIG", HOME / ".kube/config")
# The operators act on one fixed target. Without an explicit repository there is no target:
# the working directory of whatever process spawned the operator is never a substitute.
REPOSITORY_CONFIGURED = bool(os.environ.get("TESSERA_REPOSITORY"))
REPOSITORY = _path("TESSERA_REPOSITORY", Path.cwd())
STATE_DIR = _path("TESSERA_STATE_DIR", REPOSITORY / ".tessera")
DOCKER_CONFIG = _path("DOCKER_CONFIG", HOME / ".docker")
DOCKER_DESKTOP_SOCKET = DOCKER_CONFIG / "run/docker.sock"


def require_repository() -> None:
    """Fail closed unless the operator's fixed target was configured explicitly."""
    if not REPOSITORY_CONFIGURED:
        from .upgrade_support import AdmissionError

        raise AdmissionError("TESSERA_REPOSITORY must name the fixed target repository")
