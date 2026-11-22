"""Example operator policy for code review jobs.

This is an example the operator copies and owns, not a platform default. Tessera
does not import it. Put your own copy on PYTHONPATH and pass it to the runner as
``--policy code_review_policy:ExamplePolicy``. Edit the constants to match what
you are willing to send to a model destination.
"""

ALLOWED_DESTINATIONS = {"anthropic"}
ALLOWED_TIERS = {"sonnet", "opus"}
ALLOWED_DATA_CLASSES = {"public", "private_derived"}
MAX_BUDGET_CENTS = 50


class ExamplePolicy:
    """Return None to admit a job, or a stable refusal reason code."""

    def check(
        self,
        destination_id: str,
        tier: str,
        data_classes: set[str],
        budget_cents: int,
    ) -> str | None:
        if destination_id not in ALLOWED_DESTINATIONS:
            return "destination_not_enrolled"
        if tier not in ALLOWED_TIERS:
            return "tier_exceeds_policy"
        if not set(data_classes) <= ALLOWED_DATA_CLASSES:
            return "data_class_not_allowed_for_destination"
        if budget_cents > MAX_BUDGET_CENTS:
            return "budget_exceeds_policy"
        return None
