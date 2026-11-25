"""Review-job model tiers from operator configuration, and the per-job tier approval's grant.

The set of tiers a review job may name, their order, and which of them need a per-job human
approval are configuration, not code. :data:`DEFAULT_TIERS` reproduces the original behaviour
(four tiers; only the highest needs approval), so a deployment with no tiers file is unchanged.
An operator replaces it with ``tessera serve --review-tiers FILE`` (env fallback
``TESSERA_REVIEW_TIERS``)::

    {"tiers": ["small", "medium", "large"],      # ordered, lowest first
     "approval_from": "large",                   # this tier and every later one need approval;
                                                 # null: no tier needs one
     "models": {"large": ["vendor-large-2"]}}    # optional: exact model ids per tier

A job at or above the approval threshold is submitted as ``awaiting_approval``. A person approves
it only by naming a reasoning-kind agent authority grant (ADR 0142) of the job's project that pays
for the job's destination (``provider_id == destination_id``) and a model bound to the job's tier,
with a remaining budget that covers the job's cap. A model is bound to a tier by the tier's
``models`` list when one is configured, else when the tier name is one of the model id's
``-``/``.``/``_``/``/``-separated tokens (the receipt tier classification). Anything else fails
closed. The binding is recorded beside the approval and re-checked when the job starts, so a
grant revoked, expired, or spent after approval stops the run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

TIER_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
MAX_TIERS = 16
MAX_CONFIG_BYTES = 64 * 1024
MICRO = 1_000_000

# Stable reason codes (submit refuses with the first; approval and start with the others).
TIER_NOT_CONFIGURED = "tier_not_configured"
TIER_GRANT_REQUIRED = "tier_grant_required"
TIER_GRANT_NOT_FOUND = "tier_grant_not_found"
TIER_GRANT_NOT_ACTIVE = "tier_grant_not_active"
TIER_GRANT_SCOPE_MISMATCH = "tier_grant_scope_mismatch"
TIER_GRANT_BUDGET = "tier_grant_budget_insufficient"
TIER_START_REASONS = (
    TIER_GRANT_REQUIRED,
    TIER_GRANT_NOT_FOUND,
    TIER_GRANT_NOT_ACTIVE,
    TIER_GRANT_SCOPE_MISMATCH,
    TIER_GRANT_BUDGET,
)


@dataclass(frozen=True)
class ReviewTiers:
    tiers: tuple[str, ...]
    approval_from: str | None
    models: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self):
        if not 1 <= len(self.tiers) <= MAX_TIERS or len(set(self.tiers)) != len(self.tiers):
            raise ValueError("review tiers must be 1-16 unique names")
        if not all(isinstance(t, str) and TIER_NAME.match(t) for t in self.tiers):
            raise ValueError("a review tier name is lowercase letters, digits and dashes")
        if self.approval_from is not None and self.approval_from not in self.tiers:
            raise ValueError("approval_from must name a configured tier")
        for tier, models in self.models.items():
            if tier not in self.tiers:
                raise ValueError("models may only name configured tiers")
            if not models or not all(
                isinstance(m, str) and 0 < len(m) <= 200 and m.strip() == m for m in models
            ):
                raise ValueError("a tier's models must be a non-empty list of model ids")

    def known(self, tier: str) -> bool:
        return tier in self.tiers

    def requires_approval(self, tier: str) -> bool:
        if self.approval_from is None:
            return False
        return self.tiers.index(tier) >= self.tiers.index(self.approval_from)

    def model_bound(self, tier: str, model: str) -> bool:
        if tier not in self.tiers:
            return False
        listed = self.models.get(tier)
        if listed is not None:
            return model in listed
        return tier in re.split(r"[-._/]", model.lower())

    def describe(self) -> dict:
        return {
            "tiers": list(self.tiers),
            "approval_from": self.approval_from,
            "models": {k: list(v) for k, v in sorted(self.models.items())},
        }


DEFAULT_TIERS = ReviewTiers(tiers=("haiku", "sonnet", "opus", "fable"), approval_from="fable")


def parse_review_tiers(data: object) -> ReviewTiers:
    if not isinstance(data, dict) or not {"tiers"} <= set(data) <= {
        "tiers",
        "approval_from",
        "models",
    }:
        raise ValueError('review tiers config is {"tiers": [...], "approval_from", "models"}')
    tiers, threshold, models = data["tiers"], data.get("approval_from"), data.get("models", {})
    if not isinstance(tiers, list) or not isinstance(models, dict):
        raise ValueError("review tiers must be a list and models an object")
    if threshold is not None and not isinstance(threshold, str):
        raise ValueError("approval_from must be a tier name or null")
    if not all(isinstance(v, list) for v in models.values()):
        raise ValueError("a tier's models must be a list")
    return ReviewTiers(tuple(tiers), threshold, {k: tuple(v) for k, v in models.items()})


def load_review_tiers(path: Path | str) -> ReviewTiers:
    """Load and validate a tiers file; raises ValueError (or OSError) on a bad one."""
    with open(path, "rb") as handle:
        raw = handle.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("review tiers config is too large")
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("review tiers config is not valid JSON") from None
    return parse_review_tiers(data)


def create_review_tier_grants_schema(connection) -> None:
    """The grant a tier approval binds (one per approved job). Additive and append-only."""
    connection.execute("""CREATE TABLE IF NOT EXISTS review_job_tier_grants (
        job_id TEXT PRIMARY KEY REFERENCES review_job_approvals(job_id),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        grant_id TEXT NOT NULL REFERENCES agent_authority_grants(id),
        tier TEXT NOT NULL, provider_id TEXT NOT NULL, model TEXT NOT NULL,
        subject_credential_id TEXT NOT NULL, created_at TEXT NOT NULL)""")
    for action in ("UPDATE", "DELETE"):
        connection.execute(
            f"CREATE TRIGGER IF NOT EXISTS review_job_tier_grants_no_{action.lower()} "
            f"BEFORE {action} ON review_job_tier_grants "
            "BEGIN SELECT RAISE(ABORT,'review decisions are immutable'); END"
        )


def tier_grant_check(connection, job, grant_id, tiers: ReviewTiers | None = None, bound=None):
    """``(reason, binding)`` for paying ``job``'s tier with ``grant_id``; reason None = admitted.

    At approval ``tiers`` is the deployment's config (the model must be bound to the tier). At
    start ``bound`` is the recorded binding: the grant must still name that provider and model.
    Every check reads inside the caller's transaction and is tenant- and project-scoped."""
    from .agent_authority import require_active_agent_lineage
    from .grant_model_spend import grant_remaining_micro
    from .store import Forbidden

    if not grant_id:
        return TIER_GRANT_REQUIRED, None
    grant = connection.execute(
        "SELECT * FROM agent_authority_grants WHERE id=? AND org_id=?", (grant_id, job["org_id"])
    ).fetchone()
    if grant is None or grant["project_id"] != job["project_id"]:
        return TIER_GRANT_NOT_FOUND, None
    bounds = json.loads(grant["bounds_json"])
    if grant["status"] != "active":
        return TIER_GRANT_NOT_ACTIVE, None
    try:
        require_active_agent_lineage(connection, grant_id)
    except Forbidden:
        return TIER_GRANT_NOT_ACTIVE, None
    if bounds.get("kind") != "reasoning" or bounds.get("provider_id") != job["destination_id"]:
        return TIER_GRANT_SCOPE_MISMATCH, None
    model = bounds.get("model")
    if not isinstance(model, str):
        return TIER_GRANT_SCOPE_MISMATCH, None
    if tiers is not None and not tiers.model_bound(job["tier"], model):
        return TIER_GRANT_SCOPE_MISMATCH, None
    if bound is not None and (
        bound["grant_id"] != grant_id
        or bound["tier"] != job["tier"]
        or bound["provider_id"] != bounds["provider_id"]
        or bound["model"] != model
    ):
        return TIER_GRANT_SCOPE_MISMATCH, None
    if grant_remaining_micro(connection, grant) < job["budget_cents"] * MICRO:
        return TIER_GRANT_BUDGET, None
    return None, {
        "grant_id": grant_id,
        "tier": job["tier"],
        "provider_id": bounds["provider_id"],
        "model": model,
        "subject_credential_id": grant["subject_credential_id"],
    }


def tier_start_refusal(connection, job) -> str | None:
    """Reason a queued job may not start because of its tier approval, else None.

    A job with a recorded tier approval starts only while the grant bound to that approval is
    still live, unchanged and funded; an approval without a bound grant (recorded before grants
    were required) does not start. A job that never needed approval has no approval row."""
    approval = connection.execute(
        "SELECT 1 FROM review_job_approvals WHERE job_id=? AND org_id=?",
        (job["id"], job["org_id"]),
    ).fetchone()
    if approval is None:
        return None
    bound = connection.execute(
        "SELECT * FROM review_job_tier_grants WHERE job_id=? AND org_id=?",
        (job["id"], job["org_id"]),
    ).fetchone()
    if bound is None:
        return TIER_GRANT_REQUIRED
    reason, _ = tier_grant_check(connection, job, bound["grant_id"], bound=bound)
    return reason


def approval_record(connection, job_id, approval) -> dict:
    """A job's approval with its bound grant: ``grant_id`` (what a runner passes to the
    model-spend reservation) and the full ``tier_grant`` binding; both None for an approval
    recorded before grants were required."""
    bound = tier_binding_record(connection, job_id)
    return {
        **dict(approval),
        "grant_id": bound["grant_id"] if bound else None,
        "tier_grant": bound,
    }


def tier_binding_record(connection, job_id) -> dict | None:
    row = connection.execute(
        "SELECT grant_id,tier,provider_id,model,subject_credential_id,created_at "
        "FROM review_job_tier_grants WHERE job_id=?",
        (job_id,),
    ).fetchone()
    return dict(row) if row else None
