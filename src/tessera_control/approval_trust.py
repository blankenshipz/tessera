"""Approval-trust policy: who may make a human decision, and how it is labelled.

The root ``local_owner`` credential is written to disk by ``tessera init`` and is
therefore reachable by any local script. A decision (plan approval, grant issue,
attempt admission, budget authority, route approval, capability approval, review)
made with it is only accepted when the caller explicitly declares itself as
owner-operated automation via ``X-Tessera-Automation``. The label grants nothing
by itself: every ordinary role/ownership check still applies, and the label is
recorded on the decision so a script's approval is never mistaken for a person's.
"""

from __future__ import annotations

from typing import Any

# Campaign delegation (ADR 0038). The marker exists only on an in-process principal dict
# and is stripped by Store.authenticate; DELEGATED_ACTOR_KEY is set only by the
# campaign_delegation hook on the live actor rebuilt inside the transaction.
CAMPAIGN_DELEGATION_KEY = "campaign_delegation"
DELEGATED_ACTOR_KEY = "campaign_delegated"
CLASS_CAMPAIGN_DELEGATED = "campaign_delegated"
# A campaign started under a human-created standing approval (ADR 0042).
CLASS_STANDING_APPROVAL = "standing_approval"

AUTOMATION_HEADER = "X-Tessera-Automation"
AUTOMATION_KEY = "automation_label"
MAX_LABEL_LENGTH = 128

# Label the desktop app sends when it is connected with the root owner credential.
# Any holder of the owner token can send it, so it is recorded as unverified.
DESKTOP_OWNER_LABEL = "desktop-owner"

# Provenance classes stamped on every decision (``decision_class``).
CLASS_HUMAN_SESSION = "human_session"
CLASS_OWNER_AUTOMATION_UNVERIFIED = "owner_automation_unverified"
CLASS_OWNER_AUTOMATION_LABELLED = "owner_automation_labelled"
CLASS_OWNER_UNLABELLED = "owner_unlabelled"
CLASS_OTHER_CREDENTIAL = "other_credential"

# Event types whose payload carries decision provenance (stamped in Store._event).
DECISION_EVENT_TYPES = frozenset(
    {
        "task.approved",
        "execution.grant_issued",
        "experiment.attempt_admitted",
        "experiment.budget_authorization",
        "experiment.budget_charges",
        "experiment.budget_bounds",
        "experiment.conclusion_reviewed",
        "capability.approved",
        "decision_resolved",
        "research.dataset_ingestion_reviewed",
        "agent_authority.issued",
        "agent_authority.revoked",
        "credential_access.lease_created",
        "credential_access.lease_renewed",
        "credential_access.lease_revoked",
        "workspace.snapshot_released",
        "workspace.snapshot_release_revoked",
        "execution.grant_cancel_requested",
        "campaign.approved",
        "campaign.cancel_requested",
        "campaign.standing_approval_created",
        "campaign.standing_approval_revoked",
    }
)


def valid_automation_label(value: str) -> bool:
    return (
        bool(value.strip())
        and value.strip() == value
        and len(value) <= MAX_LABEL_LENGTH
        and value.isprintable()
    )


def decision_class(actor: dict[str, Any]) -> str:
    """Classify who stands behind a decision, without overstating it.

    ``human_session``: a separately issued interactive human credential.
    ``owner_automation_unverified``: root owner credential labelled ``desktop-owner``.
    The label is self-asserted by anything holding the owner token, so it is not
    evidence that a person clicked.
    ``owner_automation_labelled``: root owner credential with any other label.
    ``owner_unlabelled``: root owner credential without a label (only possible on
    reduce-only actions, which are not gated).
    ``other_credential``: any other credential kind (agent, worker, ...); the
    label does not affect the class.
    """
    if actor.get(DELEGATED_ACTOR_KEY):
        return CLASS_CAMPAIGN_DELEGATED
    kind = actor.get("kind")
    if kind == "human_session":
        return CLASS_HUMAN_SESSION
    if kind == "local_owner":
        label = actor.get(AUTOMATION_KEY)
        if not label:
            return CLASS_OWNER_UNLABELLED
        if label == DESKTOP_OWNER_LABEL:
            return CLASS_OWNER_AUTOMATION_UNVERIFIED
        return CLASS_OWNER_AUTOMATION_LABELLED
    return CLASS_OTHER_CREDENTIAL


def decision_provenance(actor: dict[str, Any]) -> dict[str, Any]:
    """Credential kind, class and automation flag for a decision event payload."""
    label = actor.get(AUTOMATION_KEY)
    result: dict[str, Any] = {
        "credential_kind": actor.get("kind"),
        "decision_class": decision_class(actor),
        "automation": bool(label),
    }
    if label:
        result["automation_label"] = label
    delegated = actor.get(DELEGATED_ACTOR_KEY)
    if delegated:
        result["campaign_id"] = delegated["campaign_id"]
        result["approval_id"] = delegated["approval_id"]
        result["intent_id"] = delegated["intent_id"]
        result["approval_decision_class"] = delegated["approval_decision_class"]
    return result


def require_decision_actor(actor: dict[str, Any], *, forbidden: type[Exception]) -> None:
    """Refuse a bare root owner credential; accept it only as labelled automation.

    A hook-verified campaign-delegated actor was decided by the recorded approval.
    """
    if actor.get(DELEGATED_ACTOR_KEY):
        return
    if actor.get("kind") == "local_owner" and not actor.get(AUTOMATION_KEY):
        raise forbidden(
            "This decision requires a separately issued human_session credential. "
            "Owner-operated automation must send a non-empty X-Tessera-Automation label."
        )
