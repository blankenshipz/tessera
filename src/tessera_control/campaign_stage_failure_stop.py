"""What a sibling stage's failure does to the rest of a campaign (a fail-fast stop).

``_mark_stage_failed`` records ``stage_failed:<key>:<reason>`` as the campaign's stop reason.
That stop is a decision about *scientific* work: stages that were never admitted do not start,
and no further scientific attempt is minted. It is not evidence about another stage's
infrastructure, so:

* a stage whose latest attempt ended for an infrastructure reason that cost nothing (an
  ``infra_*`` class, a pre-custody reallocation, ``prestart_no_spend``) keeps the bound the
  approver saw (``retry.infra_retries``); its scientific bound and the attempt-number cap stay
  binding, and nothing here changes a person's cancel or a budget stop;
* an attempt that was reserved before the stop and has not sent anything to the provider
  (``route_started`` with no create or start effect) is not launched after it: it is released at
  0 and ends as ``not_started`` instead of spending. An attempt that is itself an honoured retry
  of such an infrastructure failure still launches.

Nothing here weakens a claim, admission, spend, cancel or sealing guard: it only keeps a 0¢
retry alive and keeps an unsent, unspent route from starting.
"""

from __future__ import annotations

import sqlite3

from .campaign_class_fallback import PRESTART_NO_SPEND
from .campaign_infra_retry import REALLOCATED_PRECUSTODY, is_infra_class

STAGE_FAILED_PREFIX = "stage_failed:"
# The reason a queued, never-sent route is released at 0 after a sibling's failure.
STAGE_FAILED_STOP = "campaign_stage_failed_stop"
# The status a held route is listed under, so the executor never launches it.
HELD_CAMPAIGN_STATUS = "stage_failed_stop"


def is_stage_failure_stop(stop_reason: object) -> bool:
    return isinstance(stop_reason, str) and stop_reason.startswith(STAGE_FAILED_PREFIX)


def keeps_retry(failure_class: object) -> bool:
    """True for a failure that cost nothing and is bounded by ``retry.infra_retries`` (or its
    own repeat bound), so a sibling's failure does not end the stage's retries."""
    return (
        is_infra_class(failure_class)
        or failure_class == REALLOCATED_PRECUSTODY
        or failure_class == PRESTART_NO_SPEND
    )


def attempt_held(connection: sqlite3.Connection, attempt_id: str) -> bool:
    """True when the attempt's campaign stopped on a sibling's failure and the attempt is not an
    honoured retry (its predecessor did not end for a 0¢ infrastructure reason)."""
    row = connection.execute(
        """SELECT c.stop_reason, a.campaign_id, a.stage_key, a.attempt
        FROM campaign_stage_attempts a JOIN campaigns c ON c.id = a.campaign_id
        WHERE a.id=?""",
        (attempt_id,),
    ).fetchone()
    if row is None or not is_stage_failure_stop(row["stop_reason"]):
        return False
    previous = connection.execute(
        """SELECT failure_class FROM campaign_stage_attempts
        WHERE campaign_id=? AND stage_key=? AND attempt<? AND status='failed_transient'
        ORDER BY attempt DESC LIMIT 1""",
        (row["campaign_id"], row["stage_key"], row["attempt"]),
    ).fetchone()
    return previous is None or not keeps_retry(previous["failure_class"])
