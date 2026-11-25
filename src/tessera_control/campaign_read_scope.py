"""Which campaigns a reader may see (audit O-05).

A human reads every campaign of their organization (project-bound credentials stay inside their
project). Any other principal, which in practice is an agent credential holding ``campaign:read``,
reads only the campaigns it submitted: ``campaigns.submitted_by_principal_id`` is the caller (the
rule cancel and the steering tools use), or the caller is the agent that consumed the owner's
approved submission that started the campaign (ADR 0093 records the owner as the submitter and the
consuming agent in the immutable ``campaign_submission_decisions`` row).

The filter is applied inside the SQL that finds the rows, so a campaign the caller may not read
takes the same path as one that does not exist: the same query, no row, the same ``NotFound``.
Lists and counts built with it hold only the caller's own rows.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_OWN = (
    "({c}.submitted_by_principal_id = ? OR EXISTS ("
    "SELECT 1 FROM campaign_submission_approvals own_sa "
    "JOIN campaign_submission_decisions own_sd ON own_sd.approval_id = own_sa.id "
    "WHERE own_sa.campaign_id = {c}.id AND own_sd.decision = 'submitted' "
    "AND own_sd.actor_principal_id = ?))"
)


def reads_every_campaign(actor: Mapping[str, Any]) -> bool:
    """Only a human reads the organization's campaigns regardless of who submitted them."""
    return actor.get("role") == "human" and actor.get("kind") != "worker"


def own_clause(actor: Mapping[str, Any], campaign_id_column: str) -> tuple[str, tuple[str, ...]]:
    """An ``AND ...`` SQL fragment (and its arguments) keeping only campaigns the actor may read.

    ``campaign_id_column`` names the column holding the campaign id (``c.id`` on ``campaigns``,
    ``d.campaign_id`` on a table keyed by campaign). Empty for a human."""
    if reads_every_campaign(actor):
        return "", ()
    column = campaign_id_column
    clause = (
        f" AND EXISTS (SELECT 1 FROM campaigns own_c WHERE own_c.id = {column} AND "
        + _OWN.format(c="own_c")
        + ")"
    )
    return clause, (actor["id"], actor["id"])


def may_read(connection, actor: Mapping[str, Any], campaign_id: str | None) -> bool:
    """Whether the actor may read this campaign (tenant checks are the caller's)."""
    if reads_every_campaign(actor):
        return True
    if campaign_id is None:
        return False
    clause, args = own_clause(actor, "?")
    return (
        connection.execute("SELECT 1 WHERE 1" + clause, (campaign_id, *args)).fetchone() is not None
    )


def own_decision_clause(
    actor: Mapping[str, Any], campaign_id_column: str, actor_column: str
) -> tuple[str, tuple[str, ...]]:
    """Like ``own_clause`` for a decision row: also the decisions the caller itself triggered.

    A delegate agent that attempted a decision on a campaign it did not submit still sees the
    outcome (for example its own escalation), and nothing else about that campaign."""
    if reads_every_campaign(actor):
        return "", ()
    clause, args = own_clause(actor, campaign_id_column)
    inner = clause[len(" AND ") :]
    return f" AND ({inner} OR {actor_column} = ?)", (*args, actor["id"])
