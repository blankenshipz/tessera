"""Control API: the deploy requests the operator host recorded (ADR 0165).

``GET /admin/deploy-requests`` lists the newest deploy requests with their receipts, read from
``<state-dir>/deploy-requests``. It is read-only (it creates, evaluates and cancels nothing),
has the same audience as the deploy report (a human or an agent with ``provider_trust:read``,
never a worker) and leaves out the host-local plan and gate evidence paths. Requests are made,
evaluated and cancelled with ``tessera deploy-request`` on the operator host.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from . import deploy_request
from .external_agent_api import Actor as ExternalActor
from .provider_registration_api import ProviderRegistrationStore, registration_store

router = APIRouter(prefix="/admin/deploy-requests")
Registration = Annotated[ProviderRegistrationStore, Depends(registration_store)]
LISTED = 20
RECEIPTS_PER_REQUEST = 50


@router.get("")
def read_deploy_requests(actor: ExternalActor, store: Registration):
    """The newest deploy requests and their receipts; authorized like the deploy report."""
    store.read(actor)  # raises Forbidden/NotFound unless the caller may read provider config
    state_dir = store.database_path.parent
    requests = []
    for record in deploy_request.list_requests(state_dir, limit=LISTED):
        view = deploy_request.public_view(record)
        view["receipt_log"] = deploy_request.receipts_for(
            state_dir, record["request_id"], limit=RECEIPTS_PER_REQUEST
        )
        requests.append(view)
    return {
        "schema": "tessera.deploy-requests.v1",
        "open": [item["request_id"] for item in requests if item["status"] in deploy_request.OPEN],
        "requests": requests,
    }
