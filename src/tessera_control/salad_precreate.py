"""Salad work before the create effect: bounded retries and the "never sent" reading (ADR 0205).

Nothing before ``RemoteStore.begin`` commits the create intent reaches the provider: the
registry heartbeat, use-time authority, the guardian reserve and status read, and the trusted
server's fresh preflight reads are all reads or idempotent reservations. A transient failure
there is retried a bounded number of times, and only after a fresh read of the route shows no
create or start effect. Once an effect exists nothing here retries anything.

The same module holds the one reading of "this route's create was provably never sent" that the
runner, the provider's claim state and the operator's unclaimed route share: a create or start
effect counts as sent unless control recorded the trusted server's refusal of it *before* the
use-time claim (``salad_proxy.claim`` is committed before any provider call, so a refused claim
means no provider call happened for that effect).
"""

from __future__ import annotations

from typing import Any

from .salad_server import ProviderReadUnavailable

# One create dispatch plus at most three retries of a failed pre-effect step, spaced like the
# start preflight's (ADR 0174). Each retry repeats the guardian reserve, which refreshes the
# reservation's silence clock while it has never admitted a create (``ready``), so the
# spacing cannot push the frame into stop-only recovery.
CREATE_PREFLIGHT_ATTEMPTS = 4
CREATE_PREFLIGHT_BACKOFF_SECONDS = (2, 4, 8)
# The receipt reason of a run that ended before any create effect existed (L5): no group was
# ever asked for, so no stop is sent and no stop can be confirmed.
CREATE_NOT_ATTEMPTED = "create_not_attempted"
# Guardian refusals a later identical request can pass: its loop was not fresh, it ran out of
# its own request budget, or another frame still holds the open-frame slot (that frame may be
# released by the guardian's own reads within seconds). Anything else is final here.
TRANSIENT_RESERVE_REFUSALS = frozenset(
    {
        "guardian_loop_unready",
        "service_deadline",
        "admin_deadline_exceeded",
        "single_operation_orphan_exposure",
    }
)
# The closed reason the trusted server records on an effect whose use-time claim it refused
# before committing a claim (``TrustedSaladServer.request``).
UNSENT_PROXY_REASON = "admission_denied"
LAUNCH_ACTIONS = frozenset({"create", "start"})


class PreflightStale(ProviderReadUnavailable):
    """The fresh preflight was too old for use-time admission twice in a row (slow reads).

    Raised before any intent, so nothing was committed or sent: a ``timeout`` read failure,
    retried like one by the create and start retries."""

    failure = reason = "timeout"

    def __init__(self) -> None:
        super().__init__("Fresh provider preflight unavailable.")


class CreateNotAttempted(ValueError):
    """A stop or confirmation was asked for a route that never had a create effect."""

    def __init__(self) -> None:
        super().__init__(CREATE_NOT_ATTEMPTED)


def launch_effects(state: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The route's create and start effects (any outcome)."""
    return [e for e in (state or {}).get("effects") or [] if e.get("action") in LAUNCH_ACTIONS]


def effect_unsent(state: dict[str, Any], effect: dict[str, Any]) -> bool:
    """True when control recorded the trusted server's pre-claim refusal of ``effect``.

    The receipt for the effect must exist (the dispatch returned; ``claim_proxy`` refuses any
    later claim of an effect that has a receipt), come from the trusted server, name no
    provider resource, read ``unknown`` with no termination, and carry the closed pre-claim
    refusal reason. The
    caller must separately prove no ``salad_proxy.claim`` exists for the operation: a refusal
    after a committed claim uses the same reason, and only the missing claim proves that no
    provider call was made."""
    receipts = [r for r in state.get("receipts") or [] if r.get("effect_id") == effect.get("id")]
    return bool(receipts) and all(
        r.get("proxy_reason") == UNSENT_PROXY_REASON
        and r.get("provenance") == "provider"
        and r.get("provider_id") is None
        and r.get("allocation_id") is None
        and r.get("state") == "unknown"
        and r.get("termination_confirmed") is not True
        for r in receipts
    )


def sent_launch_effects(state: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Create/start effects that are not provably unsent (see ``effect_unsent``)."""
    return [e for e in launch_effects(state) if not effect_unsent(state or {}, e)]


def transient_precreate(error: BaseException) -> bool:
    """A pre-effect failure a later identical attempt may pass (never a refusal of scope)."""
    from .salad_live_check import _is_read_stall
    from .salad_server import GuardianRefused, GuardianUnavailable

    if isinstance(error, GuardianRefused):
        return error.code in TRANSIENT_RESERVE_REFUSALS
    if isinstance(error, GuardianUnavailable):
        return True
    return _is_read_stall(error)


def dispatch_create(dispatch, read_state, *, clock, sleep, deadline, receipt):
    """``dispatch("create")`` with bounded retries of a failed step before the create effect.

    After each failure the route is read again. If that read fails, or shows any create or
    start effect, the failure propagates unchanged and nothing is retried: the create intent
    may be committed and the create may have been sent. Otherwise nothing was sent: a transient
    failure is retried (at most ``CREATE_PREFLIGHT_ATTEMPTS`` dispatches, never past
    ``deadline``); the last or a deterministic one propagates with the receipt reason
    ``create_not_attempted``. Retries are counted on the receipt only when one happened, so a
    receipt without one keeps its digest."""
    for attempt in range(CREATE_PREFLIGHT_ATTEMPTS):
        try:
            return dispatch("create")
        except Exception as error:
            try:
                unsent = not launch_effects(read_state())
            except Exception:
                unsent = False
            if not unsent:
                raise
            if (
                attempt == CREATE_PREFLIGHT_ATTEMPTS - 1
                or not transient_precreate(error)
                or clock() >= deadline
            ):
                receipt["reason"] = CREATE_NOT_ATTEMPTED
                raise
            receipt["create_preflight_retries"] = receipt.get("create_preflight_retries", 0) + 1
            pause = CREATE_PREFLIGHT_BACKOFF_SECONDS[
                min(attempt, len(CREATE_PREFLIGHT_BACKOFF_SECONDS) - 1)
            ]
            sleep(min(pause, max(0, deadline - clock())))
    raise AssertionError("unreachable")  # pragma: no cover
