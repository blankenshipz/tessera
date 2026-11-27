"""Automatic reconciliation of unknown attempts whose provider claimed no create (ADR 0157).

An attempt can become ``unknown`` (its stage ``blocked_unknown``, its campaign ``blocked``)
before its provider ever claimed a create or start: the grant was issued and leased, and
nothing past that happened. No provider resource exists, nothing was spent, and the operator
reconcile route for an unclaimed grant would settle it at zero. ``assess`` decides, from
control-plane records and the provider's own ``create_claim_state`` read, whether an unknown
attempt is such a case. It is a pure read; the runner applies it in one transaction
(``CampaignRunner._auto_reconcile_unclaimed``).

Provider-neutral: whether a create was claimed comes only from the provider's optional
``create_claim_state`` hook (``campaign_providers.CreateClaim``). The core guards below hold
for every provider, and a provider without the hook is never reconciled automatically.

Fail closed. Every guard must hold, else the attempt keeps the human path and the refusal is
recorded with a closed reason:

* the attempt is unknown and unsettled, with no exit code, run receipt, manifest or provider
  verdict or evidence digest;
* its grant belongs to the campaign's project and is unresolved (``active`` or
  ``cancel_requested``);
* no operation of the grant has a provider id; no observation of the grant names a provider
  id, a running or exited state or a confirmed termination; and there is no provider or run
  receipt, output manifest, recovery record, dispatch or workspace handoff for it;
* the provider's ``create_claim_state`` returns ``none`` with a closed proof code.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from .campaign_providers import CLAIM_CODE_RE, CreateClaim
from .store import canonical_digest

# Closed refusal reasons, with the sentence a person reads.
REFUSALS = {
    "attempt_not_unknown": "the attempt is no longer unknown",
    "attempt_evidence": "the attempt recorded an exit, receipt, manifest or provider evidence",
    "tenant_mismatch": "the grant is not this campaign's",
    "grant_resolved": "the grant is no longer unresolved",
    "provider_reference": "a provider resource reference is recorded for the grant",
    "provider_observation": "a running, exited or terminated observation is recorded",
    "provider_receipt": "a receipt, manifest, dispatch or recovery record exists for the grant",
    "no_claim_hook": "the provider cannot report whether a create was claimed",
    "create_claimed": "the provider claimed a create or start for this attempt",
    "claim_state_unknown": "the provider cannot prove that no create was claimed",
    "ledger_inconsistent": "the reservation ledger cannot release the grant's hold",
    "revoke_refused": "the grant could not be revoked",
}
# Attempt columns any of which records an outcome or provider evidence.
EVIDENCE_COLUMNS = (
    "exit_code",
    "run_receipt_digest",
    "manifest_id",
    "salad_verdict",
    "salad_evidence_digest",
)
REASON_PREFIX = "unclaimed:"


@dataclass(frozen=True)
class Unclaimed:
    """An unknown attempt shown to have no claimed provider create, and the evidence."""

    proof: str
    evidence: dict[str, Any]

    @property
    def digest(self) -> str:
        return canonical_digest(self.evidence)

    @property
    def detail(self) -> str:
        # ``(reason)`` keeps the ADR 0046 detail shape; ``not_started`` is never evidence.
        return f"auto-reconciled as not started ({REASON_PREFIX}{self.proof}); no evidence recorded"


@dataclass(frozen=True)
class Refused:
    """Why an unknown attempt keeps the human path (closed codes)."""

    reason: str
    provider_reason: str | None = None

    @property
    def readable(self) -> str:
        return "not auto-reconciled: " + REFUSALS[self.reason]


def _any(connection: sqlite3.Connection, sql: str, *params: Any) -> bool:
    return connection.execute(sql, params).fetchone() is not None


def _hooks(provider: Any) -> tuple[Any, Any] | None:
    read = getattr(provider, "create_claim_state", None)
    mark = getattr(provider, "mark_unclaimed", None)
    if provider is None or not callable(read) or not callable(mark):
        return None
    return read, mark


def has_hooks(provider: Any) -> bool:
    return _hooks(provider) is not None


def _claim(provider: Any, connection: sqlite3.Connection, attempt: Any) -> CreateClaim | Refused:
    hooks = _hooks(provider)
    if hooks is None:
        return Refused("no_claim_hook")
    try:
        claim = hooks[0](connection, attempt)
    except Exception:  # noqa: BLE001 - an unreadable claim state is an unknown one
        return Refused("claim_state_unknown", "provider_error")
    if (
        type(claim) is not CreateClaim
        or not isinstance(claim.reason, str)
        or not CLAIM_CODE_RE.match(claim.reason)
    ):
        return Refused("claim_state_unknown", "invalid_claim_state")
    if claim.state == "claimed":
        return Refused("create_claimed", claim.reason)
    if (
        claim.state != "none"
        or not isinstance(claim.proof, str)
        or not CLAIM_CODE_RE.match(claim.proof)
    ):
        return Refused("claim_state_unknown", claim.reason)
    return claim


def assess(
    connection: sqlite3.Connection,
    attempt: sqlite3.Row | dict[str, Any],
    campaign: dict[str, Any],
    provider: Any,
) -> Unclaimed | Refused:
    """``Unclaimed`` when every guard holds and the provider proves no create was claimed."""
    if (
        attempt["status"] != "unknown"
        or attempt["grant_id"] is None
        or attempt["settled_cents"] is not None
        or attempt["campaign_id"] != campaign["id"]
    ):
        return Refused("attempt_not_unknown")
    if any(attempt[name] is not None for name in EVIDENCE_COLUMNS):
        return Refused("attempt_evidence")
    grant_id = attempt["grant_id"]
    grant = connection.execute(
        "SELECT * FROM execution_grants WHERE id=? AND project_id=?",
        (grant_id, campaign["project_id"]),
    ).fetchone()
    if grant is None:
        return Refused("tenant_mismatch")
    if grant["status"] not in ("active", "cancel_requested"):
        return Refused("grant_resolved")
    operations = connection.execute(
        "SELECT operation_id, provider_id FROM external_operations WHERE grant_id=? "
        "ORDER BY operation_id",
        (grant_id,),
    ).fetchall()
    if any(op["provider_id"] is not None for op in operations) or _any(
        connection,
        "SELECT 1 FROM execution_observations WHERE grant_id=? AND provider_id IS NOT NULL",
        grant_id,
    ):
        return Refused("provider_reference")
    if _any(
        connection,
        """SELECT 1 FROM execution_observations WHERE grant_id=?
        AND (observed_state!='unknown' OR termination_confirmed=1)""",
        grant_id,
    ):
        return Refused("provider_observation")
    operation_ids = [op["operation_id"] for op in operations]
    for table in ("artifact_manifests", "kubernetes_dispatches", "workspace_handoffs"):
        if _any(connection, f"SELECT 1 FROM {table} WHERE grant_id=?", grant_id):
            return Refused("provider_receipt")
    for table in (
        "external_operation_receipts",
        "execution_run_receipts",
        "execution_recovery_reconciliations",
    ):
        for operation_id in operation_ids:
            if _any(connection, f"SELECT 1 FROM {table} WHERE operation_id=?", operation_id):
                return Refused("provider_receipt")
    claim = _claim(provider, connection, attempt)
    if isinstance(claim, Refused):
        return claim
    return Unclaimed(
        claim.proof or "",
        {
            "attempt_id": attempt["id"],
            "grant_id": grant_id,
            "operation_ids": operation_ids,
            "claim_state": claim.state,
            "claim_reason": claim.reason,
            "proof": claim.proof,
            "provider_evidence": dict(claim.evidence or {}),
        },
    )


def automatic_count(connection: sqlite3.Connection, campaign: dict[str, Any]) -> int:
    """Automatic unclaimed reconciles this campaign has already applied (one the owner asked
    for through ``reconcile-unclaimed-attempts`` is not automatic and is not counted)."""
    return connection.execute(
        """SELECT count(*) FROM events WHERE project_id=?
        AND event_type='campaign.attempt_unclaimed_reconciled'
        AND json_extract(payload_json,'$.campaign_id')=?
        AND COALESCE(json_extract(payload_json,'$.automatic'), 1)=1""",
        (campaign["project_id"], campaign["id"]),
    ).fetchone()[0]
