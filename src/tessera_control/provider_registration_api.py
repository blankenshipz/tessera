"""Control API for validated provider registration (ADR 0081): list, add and update.

Reads are for a human or an agent with ``provider_trust:read``; a change is human only and
idempotent. The service edits only the file it was started with (``--campaign-providers``) and
never registers anything in the running process: a change applies at the next serve start. Every
change and every refusal is audited as an operator event (names, digests and field names, never
entry values), in addition to the hash-chained audit file next to the providers file.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, StringConstraints

from . import provider_registration as registration
from . import provider_trust
from .approval_trust import require_decision_actor
from .execution_api import Actor, Key, result
from .external_agent_api import Actor as ExternalActor
from .external_agents import ExternalAgentStore
from .store import LOCAL_ORG_ID, Conflict, Forbidden, Store, _connect, canonical_digest, utc_now

CHANGED = "provider_config.changed"
REFUSED = "provider_config.refused"
NAME_UNAVAILABLE = "That profile name is not available."
NOT_CONFIGURED = "Start serve with --campaign-providers FILE to register providers through the API."
router = APIRouter(prefix="/admin/campaign-providers")


class RegistrationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["add", "update"]
    section: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    key: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = None
    entry: dict[str, Any]
    expected_digest: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")] | None = None
    dry_run: bool = False


class ProviderRegistrationStore(ExternalAgentStore):
    def __init__(self, state_dir, *, path=None):
        super().__init__(state_dir)
        self.path = path

    def read(self, principal) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            actor = self._actor(connection, principal)
            if actor["kind"] == "worker":
                raise Forbidden("Workers cannot read the provider configuration.")
            if actor["role"] == "agent":
                self._scope(connection, actor, "provider_trust:read")
            elif actor["role"] != "human":
                raise Forbidden("A human or a provider_trust:read agent is required.")
            root = self.deployment_owner(actor)
        finally:
            connection.close()
        if self.path is None:
            return {"configured": False, "reason": NOT_CONFIGURED}
        try:
            document, digest, existed = registration.read_document(self.path)
        except ValueError as error:
            raise Conflict(str(error)) from None
        try:
            audit = registration.read_audit(self.path, 20)
        except ValueError:
            audit = []  # a damaged audit file never hides the configuration
        # The file is deployment-wide; an organization sees its own profiles and the entries
        # that name no organization (providers shared by the deployment), never another org's.
        org = actor["org_id"]
        entries = [
            row for row in registration.list_document(document) if row.get("org_id") in (None, org)
        ]
        if not root:  # another tenant's worker ids in deployment-wide entries stay private
            entries = [
                {**r, "worker_principal_id": None} if r["scope"] == "deployment" else r
                for r in entries
            ]
        return {
            "configured": True,
            "exists": existed,
            "digest": digest,
            "sections": registration.known_section_names(document),
            "entries": entries,
            "recent_audit": [
                {k: r.get(k) for k in ("at", "actor", "action", "section", "key", "after_digest")}
                for r in audit
            ],
            "applies": "at the next serve start",
        }

    @staticmethod
    def deployment_owner(principal) -> bool:
        """The root owner of the deployment's bootstrap organization (``LOCAL_ORG_ID``).

        ``Store._is_root_owner`` is per organization; deployment-wide configuration needs the
        owner of the deployment itself, so a root owner of any later organization is not one."""
        if not Store._is_root_owner(principal):
            return False
        return principal["org_id"] == LOCAL_ORG_ID

    def _destination_refusal(
        self, connection, live, body: RegistrationBody, document: dict[str, Any]
    ) -> str | None:
        """Who may change this entry, and may it name this destination.

        Organization-scoped entries (Salad profiles) belong to the organization named in them: a
        human may add profiles for its own organization and project and may change only profiles
        its organization already owns; another organization's name is "not available" without
        saying whether it exists. Everything else is deployment-wide (it decides which worker
        receives every organization's grants, and under ADR 0047's owner mode what a destination
        may receive), so only the deployment's root owner may change it. Every worker principal an
        entry names is checked against the database, on the entry as it would be stored.

        ``document`` is the file as read under the file lock by ``apply_change``."""
        spec = registration.sections(document).get(body.section)
        entry = {**(body.entry or {})}
        if spec is None or spec.scope == "deployment":
            if not self.deployment_owner(live):
                return (
                    "Deployment-wide provider configuration can be changed only by the "
                    "deployment's owner."
                )
            if spec is not None:
                stored = self._stored(document, spec, body)
                entry = {**stored, **entry} if body.action == "update" else entry
                worker = spec.worker(entry)
                if worker is not None:
                    return self._worker_refusal(connection, live, worker, None)
            return None
        stored = self._stored(document, spec, body)
        if stored and stored.get("org_id") != live["org_id"]:
            return NAME_UNAVAILABLE
        if body.action == "update":
            entry = {**stored, **entry}
        project_id, org_id = entry.get("project_id"), entry.get("org_id")
        worker = entry.get("worker_principal_id")
        if not all(isinstance(v, str) for v in (project_id, org_id, worker)):
            return (
                "Unknown destination: the profile must name its project, organization and worker."
            )
        project = connection.execute(
            "SELECT org_id FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if project is None or project["org_id"] != live["org_id"] or org_id != live["org_id"]:
            return "Unknown destination: the profile's project is not in this organization."
        return self._worker_refusal(connection, live, worker, project_id)

    @staticmethod
    def _stored(document, spec, body: RegistrationBody) -> dict[str, Any]:
        name = body.key or (body.entry or {}).get(spec.key_field or "")
        found = [
            e
            for e in registration.entries_of(document, spec)
            if spec.key_field is None or e.get(spec.key_field) == name
        ]
        return found[0] if found else {}

    @staticmethod
    def _worker_refusal(connection, live, worker, project_id) -> str | None:
        if not isinstance(worker, str):
            return "Unknown destination: the worker principal must be a string."
        try:
            found = Store._live_principal(
                connection, {"id": worker, "org_id": live["org_id"]}, audience="worker"
            )
        except Forbidden:
            return "Unknown destination: the worker principal is not registered."
        if found["kind"] != "worker" or found["role"] != "agent":
            return "Unknown destination: the principal is not a worker."
        if project_id is not None and found["project_id"] != project_id:
            return "Unknown destination: the worker principal is not in the profile's project."
        return None

    def register(self, principal, key: str, body: RegistrationBody) -> tuple[int, dict]:
        payload = body.model_dump(mode="json")

        def command(connection, live):
            if live["role"] != "human":
                raise Forbidden("Only a human may register providers.")
            require_decision_actor(live, forbidden=Forbidden)
            if self.path is None:
                return 409, {"status": "refused", "reason": NOT_CONFIGURED}
            now = utc_now()
            names = {"section": body.section, "key": body.key, "action": body.action}

            def refuse(reason: str):
                provider_trust.audit_operator_event(
                    connection,
                    REFUSED,
                    "denied",
                    live["id"],
                    {**names, "reason": reason[:300]},
                    now,
                )
                return 409, {"status": "refused", "reason": reason}

            try:
                outcome = registration.apply_change(
                    self.path,
                    body.action,
                    body.section,
                    body.key,
                    body.entry,
                    actor=live["id"],
                    expected_digest=body.expected_digest,
                    policy=provider_trust.current_policy(connection),
                    dry_run=body.dry_run,
                    authorize=lambda document: self._destination_refusal(
                        connection, live, body, document
                    ),
                )
            except (registration.RegistrationRefused, ValueError) as error:
                return refuse(str(error))
            if outcome["written"]:
                provider_trust.audit_operator_event(
                    connection,
                    CHANGED,
                    "allowed",
                    live["id"],
                    {
                        **names,
                        "key": outcome["key"],
                        "changed_fields": outcome["changed_fields"],
                        "before_digest": outcome["before_digest"],
                        "after_digest": outcome["after_digest"],
                    },
                    now,
                )
            return 200, {"status": "written" if outcome["written"] else "validated", **outcome}

        return self._mutate(
            principal, key, "POST", "/admin/campaign-providers", canonical_digest(payload), command
        )


def registration_store(request: Request) -> ProviderRegistrationStore:
    state = request.app.state
    store = getattr(state, "provider_registration", None)
    if store is None:
        store = ProviderRegistrationStore(
            state.admin.database_path.parent, path=getattr(state, "campaign_providers_path", None)
        )
        state.provider_registration = store
    return store


Registration = Annotated[ProviderRegistrationStore, Depends(registration_store)]


@router.get("")
def read_campaign_providers(actor: ExternalActor, store: Registration):
    """Entries (no secrets by construction), digest and recent audit of the providers file."""
    return store.read(actor)


@router.post("")
def register_campaign_provider(body: RegistrationBody, actor: Actor, key: Key, store: Registration):
    """Human only: validate and write one provider or profile change; applies at next start."""
    return result(store.register(actor, key, body))
