"""Control API: the control-side deploy report (ADR 0098).

``GET /admin/deploy-check`` returns the part of ``tessera deploy-check`` the running control
plane can see for itself: its database schema and the providers file it was started with. It is
read-only, has the same audience as the providers read (a human or an agent with
``provider_trust:read``, never a worker) and changes nothing. Config files, backups and the
smoke are local to the operator's host and stay with the CLI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from . import deploy_check, deploy_state
from .deploy_check import Finding
from .external_agent_api import Actor as ExternalActor
from .provider_registration_api import ProviderRegistrationStore, registration_store

router = APIRouter(prefix="/admin/deploy-check")
Registration = Annotated[ProviderRegistrationStore, Depends(registration_store)]


def control_side_findings(state_dir: Path, providers_path: Path | None) -> list[Finding]:
    findings = [deploy_state.check_schema(state_dir)]
    if providers_path is None:
        findings.append(
            Finding(
                "providers.file",
                "providers",
                "skipped",
                "serve was not started with --campaign-providers; no providers file to check",
            )
        )
    else:
        findings.extend(deploy_state.check_providers(providers_path))
    return findings


@router.get("")
def read_deploy_check(actor: ExternalActor, store: Registration, request: Request):
    """The control-side deploy report; authorized exactly like the providers read."""
    store.read(actor)  # raises Forbidden/NotFound unless the caller may read provider config
    report = deploy_check.build_report(
        control_side_findings(store.database_path.parent, store.path)
    )
    report["scope"] = "control-plane"
    return report
