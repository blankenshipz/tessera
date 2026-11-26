"""Running-route latch of the Salad live check (ADR 0075 running-route rule, extended).

The live check never cancels a route it protects for a non-terminal error. Before this latch,
that protection was keyed on the *last recorded* provider status, so one failed or blocked
provider read (recorded as ``unknown``) lifted it, and a route that had been started but was
still allocating or pulling its image never had it.

The latch engages once the start effect is recorded (``started``) or an observe reads the group
``running`` (``seen_running``). It is cleared only by:

* an affirmative provider observation of a non-running state: the observe's own readback has
  ``proxy_reason == "observed"`` and the state is ``stopped``, ``succeeded``, ``failed`` or
  ``deleted``;
* a reallocation recorded on the route (``reallocated``); or
* a guardian-recorded stop cause (checked by the live check's callers, which end the hold on
  it).

A failed, blocked or unknown read never clears it, never overwrites the last affirmative status
and never switches the observe to the slow pre-start cadence. The run deadline, the budget, a
verified revocation and every guardian decision still end a latched route; the guardian keeps
its own allocation deadline, start-failure count and silence bound.

Pure bookkeeping: no I/O, no clock, never raises on a malformed state.
"""

from __future__ import annotations

# The provider statuses an affirmative observation must read to clear the latch.
AFFIRMATIVE_NON_RUNNING_STATUSES = frozenset({"stopped", "succeeded", "failed", "deleted"})
# The provider status of a group whose instance runs.
RUNNING_STATUS = "running"
# The readback reason of a successful exact provider read.
OBSERVED_REASON = "observed"
# Closed receipt values (``running_latch``).
LATCH_STATES = frozenset({"started", "seen_running", "cleared"})
LATCH_CLEARED_BY = frozenset(
    {"provider_stopped", "provider_succeeded", "provider_failed", "provider_deleted", "reallocated"}
)


def observation(state):
    """``(status, affirmative)`` of one observe's returned route state.

    ``affirmative`` is True only when the observe's own readback is a successful exact read:
    the latest receipt belongs to the latest effect and carries ``proxy_reason == "observed"``.
    A state with no receipt diagnostics at all (an isolated session that reports only a status,
    never the durable route record) is taken at its word for any status it names. A durable
    receipt always carries ``proxy_reason`` (None for a failed read with no closed token), so
    such a failed read is never affirmative. Never raises."""
    try:
        status = state.get("provider_status")
        receipts = state.get("receipts")
        if not isinstance(receipts, list) or not receipts:
            return status, status is not None
        last = receipts[-1]
        if not isinstance(last, dict):
            return status, False
        effects = state.get("effects")
        latest = effects[-1] if isinstance(effects, list) and effects else None
        if (
            isinstance(latest, dict)
            and "id" in latest
            and "effect_id" in last
            and last["effect_id"] != latest["id"]
        ):
            return status, False  # not this observe's readback
        reason = last.get("proxy_reason")
        if "proxy_reason" not in last:
            return status, status is not None
        return status, reason == OBSERVED_REASON
    except Exception:
        return None, False


class RunningLatch:
    """``started`` / ``seen_running`` once engaged; ``cleared`` only on the causes above."""

    def __init__(self):
        self.state = None
        self.cleared_by = None

    def engaged(self):
        return self.state in {"started", "seen_running"}

    def note_effects(self, state):
        """Engage when the durable route already records a start effect (reconnect)."""
        try:
            effects = state.get("effects") or []
            if any(isinstance(e, dict) and e.get("action") == "start" for e in effects):
                self.note_start()
        except Exception:
            pass

    def note_start(self):
        """The start effect is recorded: protect the provisioning window too (image pull)."""
        if self.state is None:
            self.state = "started"

    def note_observe(self, state):
        """Fold one observe into the latch; True when its status may be recorded.

        A latched route records only an affirmative observation's status; an unlatched one
        keeps the earlier rule (the caller's partial-reply guard still applies)."""
        status, affirmative = observation(state)
        try:
            reallocated = state.get("reallocated") is True
        except Exception:
            reallocated = False
        if self.engaged() and reallocated:
            self._clear("reallocated")
            return True
        if status == RUNNING_STATUS and self.state != "cleared":
            # Fail toward protection: any read naming running engages the latch.
            self.state = "seen_running"
        elif affirmative and self.engaged() and status in AFFIRMATIVE_NON_RUNNING_STATUSES:
            self._clear("provider_" + status)
            return True
        if self.engaged():
            return affirmative
        return True

    def slow_cadence(self, state, prestart_statuses):
        """True when this observe may stretch to the slow pre-start cadence: its status is a
        pre-start one and, on a latched route, the read was affirmative (never a failed read)."""
        status, affirmative = observation(state)
        if status not in prestart_statuses:
            return False
        return affirmative or not self.engaged()

    def _clear(self, cause):
        self.state = "cleared"
        self.cleared_by = cause if cause in LATCH_CLEARED_BY else None

    def receipt(self):
        """The receipt's ``running_latch`` block, or None when the latch never engaged."""
        if self.state is None:
            return None
        return {"state": self.state, "cleared_by": self.cleared_by}
