"""Dispatch profiles the MCP adapter offers: exactly those the control API admits.

The control contract (`DispatchAgentAttempt.profile_version`) is the single source;
adding a profile there opens it here with no adapter edit. The CPU profile is
`ordinary-docker-v1` (worker adapter `docker-local-cpu-v1` is a package adapter
version, not a dispatchable profile, and would be refused by the control API).
"""

from typing import Literal, get_args

from tessera_control.agent_authority_models import DispatchAgentAttempt

DISPATCH_PROFILES: tuple[str, ...] = get_args(
    DispatchAgentAttempt.model_fields["profile_version"].annotation
)
if not DISPATCH_PROFILES or any(type(p) is not str for p in DISPATCH_PROFILES):
    raise RuntimeError("Control dispatch profile contract is not a closed string set")

DispatchProfile = Literal[DISPATCH_PROFILES]  # type: ignore[valid-type]
