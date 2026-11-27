"""Salad work before the create effect: bounded retries and the "never sent" reading (ADR 0205).

Nothing before ``RemoteStore.begin`` commits the create intent reaches the provider: the
registry heartbeat, use-time authority, the guardian reserve and status read, and the trusted
server's fresh preflight reads are all reads or idempotent reservations. A transient failure
there is retried a bounded number of times, and only after a fresh read of the route shows no
create or start effect. Once an effect exists nothing here retries anything.

The same module holds the one reading of "this route's create was provably never sent" that the
runner, the provider's claim state and the operator's unclaimed route share: a create or start
effect counts as sent unless control recorded the trusted server's own refusal of it at its
use-time admission. ``salad_proxy.claim`` is committed before any provider call, and a provider
receipt is accepted only for a claimed effect, so such a refusal always follows a committed
claim; it proves nothing was sent only together with the server's
``salad_proxy.claim_refused`` record for that nonce, written when its use-time recheck refused
the claim before the provider call (ADR 0205 amendment). A claim without that record still
counts as a possible provider call (``salad_unclaimed.create_claimed``), except a create a
build before v0.103.0 refused at that recheck without writing the record: its receipt is the
bare pre-call refusal with no group identity, allocation or provider exchange field
(``unrecorded_refusal_unsent``, q-2704 follow-up).
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
    """True when control recorded the trusted server's use-time admission refusal of ``effect``.

    The receipt for the effect must exist (the dispatch returned; ``claim_proxy`` refuses any
    later claim of an effect that has a receipt), come from the trusted server, name no
    provider resource, read ``unknown`` with no termination, and carry the closed pre-claim
    refusal reason. The caller must separately prove that no unrefused ``salad_proxy.claim``
    exists for the operation (``salad_unclaimed.create_claimed``): the transport can also
    answer ``admission_denied`` after a provider call, so only the server's own
    ``salad_proxy.claim_refused`` record for the nonce, or for a build before that record the
    stricter ``unrecorded_refusal_unsent``, proves its use-time recheck refused the claim before
    that call."""
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


# Receipt fields set only by a provider exchange or by the HTTP transport (a request id, an HTTP
# status, a charge, evidence links, a create rejection, or the closed failed-read diagnostics).
# The trusted server's own pre-call refusal (``ProxyResult(reason="admission_denied")`` with no
# observation) leaves every one empty; the transport's ``admission_denied`` after a create POST
# (a 201 that did not read settled) names ``proxy_request_kind``.
EXCHANGE_FIELDS = (
    "request_id",
    "response_status",
    "cost_cents",
    "provider_rejection",
    "proxy_detail",
    "proxy_field",
    "proxy_request_kind",
)


def unrecorded_refusal_unsent(state: dict[str, Any], effect: dict[str, Any]) -> bool:
    """True when ``effect`` is a create refused before any provider call even with no
    ``salad_proxy.claim_refused`` record (q-2704 follow-up, ADR 0205 amendment).

    A build before v0.103.0 (v0.102.0) refused a committed claim at its use-time recheck without
    writing that record, and once the effect has a receipt none can be added. Its trusted
    server's ``admission_denied`` with status ``unknown`` comes only from that recheck, which
    precedes the one provider call; the transport's own ``admission_denied`` after a create POST
    carries ``proxy_request_kind``. So the claim reads unsent only when: the effect is the
    route's only create/start effect and is a create; the route records no group identity and
    no allocation; no receipt of the route names a provider resource or allocation; and every
    receipt of the effect is the pre-call refusal (``effect_unsent``) with every exchange field
    empty. Anything that looks like a provider call (a provider or allocation id, request id,
    response status, charge, evidence, rejection, transport diagnostic or a start) fails
    closed. Never infers termination or a charge."""
    if (
        effect.get("action") != "create"
        or state.get("provider_id") is not None
        or state.get("allocation_id") is not None
        or [e.get("id") for e in launch_effects(state)] != [effect.get("id")]
    ):
        return False
    receipts = state.get("receipts") or []
    if any(r.get(name) is not None for r in receipts for name in ("provider_id", "allocation_id")):
        return False
    own = [r for r in receipts if r.get("effect_id") == effect.get("id")]
    return effect_unsent(state, effect) and all(
        all(r.get(name) is None for name in EXCHANGE_FIELDS) and not r.get("evidence") for r in own
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
                code = getattr(error, "code", None)
                if code in TRANSIENT_RESERVE_REFUSALS:
                    # The guardian's own closed refusal code of the last reserve (absent
                    # otherwise, so every other receipt keeps its digest): control requeues a
                    # full open-frame fence as not started instead of spending a retry.
                    receipt["create_preflight_refusal"] = code
                raise
            receipt["create_preflight_retries"] = receipt.get("create_preflight_retries", 0) + 1
            pause = CREATE_PREFLIGHT_BACKOFF_SECONDS[
                min(attempt, len(CREATE_PREFLIGHT_BACKOFF_SECONDS) - 1)
            ]
            sleep(min(pause, max(0, deadline - clock())))
    raise AssertionError("unreachable")  # pragma: no cover
