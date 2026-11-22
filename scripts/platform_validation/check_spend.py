"""Item 4: experiment spend admission accounting (synthetic; not a provider cap)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from .common import AUTOMATION_LABEL, Context, Journey, Result, db_counts, db_rows

CAPABILITY = "spend-accounting"
LABEL = "admission accounting, not a provider cap"
CEILING = 7  # cents; one 5-cent hold fits, a second does not
# Events approval_trust stamps with decision provenance (DECISION_EVENT_TYPES subset used here).
DECISION_EVENTS = frozenset(
    {
        "task.approved",
        "execution.grant_issued",
        "experiment.attempt_admitted",
        "experiment.budget_authorization",
        "experiment.budget_charges",
        "experiment.budget_bounds",
        "workspace.snapshot_released",
    }
)
COUNTS = {
    "attempts": "SELECT count(*) FROM research_experiment_admitted_attempts",
    "admit_events": "SELECT count(*) FROM events WHERE event_type='experiment.attempt_admitted'",
    "reservations": "SELECT count(*) FROM experiment_budget_reservations",
    "admit_keys": "SELECT count(*) FROM idempotency WHERE path LIKE '%/admitted-attempts'",
}


def provenance(rows: list[tuple]) -> tuple[bool, str]:
    # Recorded provenance: every decision event the server stored must carry labelled
    # owner-automation provenance (not a person's session), at least one approval or
    # grant decision must be among them besides the budget authorization, and
    # non-decision events must carry no label. Anything missing or malformed is a FAIL.
    stamped: dict[str, list[dict]] = {}
    problems: list[str] = []
    for event_type, payload in rows:
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            problems.append(f"{event_type}: payload is not JSON")
            continue
        decision = event_type in DECISION_EVENTS
        if not isinstance(data, dict):
            if decision:
                problems.append(f"{event_type}: payload is not an object")
            continue
        if decision:
            if (
                data.get("decision_class") == "owner_automation_labelled"
                and data.get("automation_label") == AUTOMATION_LABEL
                and data.get("credential_kind") == "local_owner"
                and data.get("automation") is True
            ):
                stamped.setdefault(event_type, []).append(data)
            else:
                problems.append(f"{event_type}: missing or wrong provenance")
        elif "automation_label" in data or "decision_class" in data:
            problems.append(f"{event_type}: non-decision event carries provenance")
    budget = stamped.get("experiment.budget_authorization", [])
    approval = next(
        (
            (kind, stamped[kind][0])
            for kind in ("task.approved", "execution.grant_issued")
            if stamped.get(kind)
        ),
        None,
    )
    sample = budget[0] if budget else {}
    return (
        not problems and bool(budget) and approval is not None,
        f"{sum(map(len, stamped.values()))} decision events stamped; budget_authorization: "
        f"decision_class={sample.get('decision_class')} "
        f"automation_label={sample.get('automation_label')} "
        f"credential_kind={sample.get('credential_kind')}; "
        f"{approval[0] if approval else 'approval/grant'}: "
        f"decision_class={approval[1].get('decision_class') if approval else None}; "
        f"problems={problems[:3]}",
    )


def run(ctx: Context) -> list[Result]:
    out: list[Result] = []

    def add(case: str, ok: bool, evidence: str) -> None:
        out.append(Result(CAPABILITY, case, "PASS" if ok else "FAIL", f"{evidence} [{LABEL}]"))

    ctx.start_service()
    journey = Journey(ctx, limit_cents=20, grant_cents=5)
    base = f"/experiments/{journey.experiment['id']}/budget"
    status, _ = ctx.request("GET", base)
    add("no-budget-before-authorization", status == 404, f"GET budget status={status}")

    body = {
        "expected_revision": 0,
        "ceiling_cents": CEILING,
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "evidence_digest": "a" * 64,
        "source_reference": "synthetic-local-cap",
        "prior_settled_cents": 0,
        "prior_pending_cents": 0,
        "ancillary_cents": 0,
        "shutdown_cents": 0,
    }
    key = ctx.new_key()
    status, auth = ctx.request("POST", base + "/authorization", body, idempotency_key=key)
    ok = status == 200 and auth["ceiling_cents"] == CEILING and not auth["exposure_unknown"]
    add("authorize-small-cap", ok, f"status={status} ceiling={CEILING} state={auth.get('state')}")
    status, replay = ctx.request("POST", base + "/authorization", body, idempotency_key=key)
    add("authorize-idempotent-replay", status == 200 and replay == auth, f"status={status}")
    status, _ = ctx.request("POST", base + "/authorization", body, idempotency_key=ctx.new_key())
    add("budget-reset-refused", status == 409, f"second authorization status={status}")

    _, _, first, _ = journey.admit("baseline")
    status, budget = ctx.request("GET", base)
    add(
        "admission-reserves-hold",
        status == 200
        and budget["accounted_exposure_cents"] == 5
        and budget["remaining_cents"] == 2,
        f"exposure={budget.get('accounted_exposure_cents')} "
        f"remaining={budget.get('remaining_cents')}",
    )
    before = db_counts(ctx.state_dir, COUNTS)

    # The second admission needs a fresh grant first; only the admission itself must roll back.
    _, status, refused, key2 = journey.admit("intervention", expected=None)
    after = db_counts(ctx.state_dir, COUNTS)
    add(
        "over-cap-admission-refused-409",
        status == 409 and "budget" in str(refused).lower(),
        f"status={status} detail={refused}",
    )
    add("over-cap-admission-rolled-back", after == before, f"before={before} after={after}")
    status, budget2 = ctx.request("GET", base)
    add(
        "refusal-leaves-budget-unchanged",
        budget2["accounted_exposure_cents"] == 5 and budget2["revision"] == budget["revision"],
        f"exposure={budget2.get('accounted_exposure_cents')} revision={budget2.get('revision')}",
    )

    charge = {
        "expected_revision": budget2["revision"],
        "delivery_id": "pv-charge-1",
        "amount_cents": 3,
        "evidence_digest": "b" * 64,
        "source_reference": "synthetic-human-assertion",
        "category": "compute",
        "admitted_attempt_id": first["id"],
    }
    ckey = ctx.new_key()
    status, charged = ctx.request("POST", base + "/charges", charge, idempotency_key=ckey)
    ok = status == 200 and charged["revision"] == budget2["revision"] + 1
    add("human-charge-recorded", ok, f"status={status} revision={charged.get('revision')}")
    status, again = ctx.request("POST", base + "/charges", charge, idempotency_key=ckey)
    add("charge-idempotent-replay", status == 200 and again == charged, f"status={status}")
    status, current = ctx.request("GET", base)
    consistent = (
        current["remaining_cents"] == CEILING - current["accounted_exposure_cents"]
        and current["accounted_exposure_cents"] <= CEILING
    )
    add(
        "reconciled-remaining-consistent",
        status == 200 and consistent,
        f"exposure={current.get('accounted_exposure_cents')} "
        f"remaining={current.get('remaining_cents')}",
    )
    add(
        "provider-hard-cap-not-claimed",
        current.get("provider_hard_cap_verified") is False
        and charged.get("provider_hard_cap_verified") is False,
        f"provider_hard_cap_verified={current.get('provider_hard_cap_verified')}",
    )
    status, _ = ctx.request(
        "POST", base + "/charges", charge, auth="none", idempotency_key=ctx.new_key()
    )
    add("charge-requires-human-auth", status in (401, 403), f"unauthenticated status={status}")

    ok, evidence = provenance(db_rows(ctx.state_dir, "SELECT event_type, payload_json FROM events"))
    add("decision-provenance-recorded", ok, evidence)
    return out
