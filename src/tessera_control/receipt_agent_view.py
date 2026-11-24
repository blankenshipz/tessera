"""The agent view of a campaign receipt: the stored receipt without approver identifiers.

The stored receipt (``campaign_receipts.receipt_json``) is the owner's audit record, bound by
one ``receipt_digest``; it is never rewritten and a person reads it whole. An external-scope
agent credential reading it through the receipt route (and so through
``tessera_get_campaign_receipt`` and ``tessera_campaign_wave_receipts``, which call that route)
gets this projection instead, the same approver-hiding rule the live campaign view and the gate
decisions apply: what was approved and what an agent needs to act inside the approval stays
(spec digest, decision class, approval id and expiry, delegation, pre-approval stage, caps,
expiry, and the kind, attempt, cents and class of each review resolved under it), but not who
approved, which credential grant approved, the pre-approval's id and the digests that identify
it, or the resolved decision-request ids and their review digests.

The projection is marked: the response carries ``"view": "agent_redacted"`` and ``"redacted"``
(the field paths removed, ``[]`` marking every list item), and the receipt body itself carries
``"view": "agent_redacted"``, so it cannot be taken for the stored body. ``receipt_digest`` in the
response stays the STORED receipt's digest: it names the audit record the agent was shown a view
of, so a person can look that record up. The projection is therefore NOT digest-verifiable;
recomputing a digest over it would give a value bound to no record, so none is computed.

Decision: only approver, pre-approval and decision-request identifiers are hidden.
``worker_principal_id`` (per attempt and in ``code_transfer``) and delegation / unlock ids are
deliberately KEPT visible to agents: they identify workers and delegations the agent must act on,
not approvers. The fixed ``HIDDEN_PATHS`` list is backed by a generic guard test
(``tests/test_agent_receipt_approver_guard.py``): no key matching ``approv.*principal`` may
appear anywhere in the agent view, and every such key in a stored receipt must be listed, so a
new approver field cannot ship unhidden.

Platform components (runner chaining of predecessor receipts, ``receipt_output`` resolution,
gate and unlock evaluation, the plain-words report) read ``campaign_receipts`` directly and
never this projection.
"""

from __future__ import annotations

import copy
from typing import Any

VIEW = "agent_redacted"

# Field paths removed from the agent view. ``[]`` means every item of that list.
# Approver, pre-approval and decision-request identifiers only; worker_principal_id and
# delegation / unlock ids stay visible on purpose (see the module docstring). The generic guard
# test tests/test_agent_receipt_approver_guard.py fails if a new approver key is not listed.
HIDDEN_PATHS: tuple[str, ...] = (
    # Who approved, and the smoke credential grant they approved under.
    "approval.approving_principal_id",
    "approval.smoke_grant_id",
    # The pre-approval's id and the three exact-match digests that identify it and the reviews.
    "preapprovals[].id",
    "preapprovals[].source_digest",
    "preapprovals[].plan_digest",
    "preapprovals[].source_use_digest",
    # The resolved decision request (or research source) and its review digest.
    "preapprovals[].resolutions[].subject_id",
    "preapprovals[].resolutions[].review_digest",
    # Who approved the submission and who decided on it.
    "submission.approved_by_principal_id",
    "submission.decisions[].actor_principal_id",
)


def _remove(node: Any, parts: list[str]) -> bool:
    """Remove the path ``parts`` under ``node``; True when at least one field was present."""
    if not parts:
        return False
    head, rest = parts[0], parts[1:]
    many = head.endswith("[]")
    key = head[:-2] if many else head
    if not isinstance(node, dict) or key not in node:
        return False
    if not rest:
        if many:
            return False
        del node[key]
        return True
    child = node[key]
    if many:
        found = False
        for item in child if isinstance(child, list) else ():
            found = _remove(item, rest) or found
        return found
    return _remove(child, rest)


def agent_receipt(receipt: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return ``(projected receipt, removed paths)``; ``receipt`` itself is not changed."""
    projected = copy.deepcopy(receipt)
    removed = [path for path in HIDDEN_PATHS if _remove(projected, path.split("."))]
    projected["view"] = VIEW
    return projected, removed


def agent_receipt_response(stored: dict[str, Any]) -> dict[str, Any]:
    """The receipt read response for an external-scope agent, from the stored response."""
    projected, removed = agent_receipt(stored["receipt"])
    return {**stored, "receipt": projected, "view": VIEW, "redacted": removed}
