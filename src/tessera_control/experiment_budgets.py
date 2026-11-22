"""Experiment parent ledger in the control DB; all writes use the caller's transaction.

This enforces admission, not a provider financial hard cap. Evidence references are
human accounting assertions, not verification of a provider invoice. No command in
this slice releases residual liability. An unknown (NULL) bound can only be resolved
once, to a human-declared amount with evidence, by resolve_bounds; it is never inferred.
"""

import sqlite3
from datetime import UTC, datetime

from .store import Conflict, NotFound, _parse_utc, canonical_digest, utc_now

TERMINAL_ATTEMPT_STATES = frozenset({"succeeded", "failed", "cancelled"})


def create_experiment_budget_schema(db: sqlite3.Connection) -> None:
    money = "typeof({0})='integer' AND {0} BETWEEN 0 AND 9007199254740991"

    def nullable(name):
        return f"{name} IS NULL OR ({money.format(name)})"

    db.execute(f"""CREATE TABLE IF NOT EXISTS experiment_budgets (
        experiment_id TEXT PRIMARY KEY REFERENCES research_experiments(id),
        ceiling_cents INTEGER NOT NULL CHECK(typeof(ceiling_cents)='integer'
            AND ceiling_cents BETWEEN 1 AND 5000),
        authorization_revision INTEGER NOT NULL CHECK(authorization_revision>=1),
        revision INTEGER NOT NULL CHECK(revision>=1),
        currency TEXT NOT NULL CHECK(currency='USD'),
        state TEXT NOT NULL CHECK(state IN ('active','fenced','closed')),
        expires_at TEXT NOT NULL, evidence_digest TEXT NOT NULL,
        source_reference TEXT NOT NULL, created_at TEXT NOT NULL,
        actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        prior_settled_cents INTEGER CHECK({nullable("prior_settled_cents")}),
        prior_pending_cents INTEGER CHECK({nullable("prior_pending_cents")}),
        ancillary_cents INTEGER CHECK({nullable("ancillary_cents")}),
        shutdown_cents INTEGER CHECK({nullable("shutdown_cents")}))""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS experiment_budget_reservations (
        admitted_attempt_id TEXT PRIMARY KEY REFERENCES research_experiment_admitted_attempts(id),
        experiment_id TEXT NOT NULL REFERENCES experiment_budgets(experiment_id),
        grant_id TEXT NOT NULL UNIQUE REFERENCES execution_grants(id),
        authorization_revision INTEGER NOT NULL CHECK(authorization_revision>=1),
        original_cents INTEGER NOT NULL CHECK({money.format("original_cents")}),
        held_cents INTEGER NOT NULL CHECK({money.format("held_cents")}),
        settled_cents INTEGER NOT NULL CHECK({money.format("settled_cents")}),
        exposure_known INTEGER NOT NULL CHECK(exposure_known IN (0,1)),
        revision INTEGER NOT NULL CHECK(revision>=1))""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS experiment_budget_entries (
        experiment_id TEXT NOT NULL REFERENCES experiment_budgets(experiment_id),
        delivery_id TEXT NOT NULL, request_digest TEXT NOT NULL,
        admitted_attempt_id TEXT REFERENCES experiment_budget_reservations(admitted_attempt_id),
        category TEXT NOT NULL CHECK(category IN ('compute','prior','ancillary','shutdown')),
        amount_cents INTEGER NOT NULL CHECK({money.format("amount_cents")}),
        evidence_digest TEXT NOT NULL, source_reference TEXT NOT NULL,
        recorded_at TEXT NOT NULL, actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        PRIMARY KEY(experiment_id,delivery_id))""")
    create_bound_resolution_schema(db)
    create_budget_release_schema(db)


# ADR 0041 Q3: the only bases whose settlement may release an experiment hold.
RELEASE_BASES = ("provider_absent_zero", "local_zero", "unclaimed_zero")


def create_budget_release_schema(db: sqlite3.Connection) -> None:
    """Write-once experiment hold releases (ADR 0041 Q3; idempotent, schema 66).

    One row per admitted attempt, written only by ``campaign_settlement.settle_frame`` in
    the settlement transaction. It is not a charge and never edits the reservation: the
    reservation's hold simply stops counting in ``budget_status``. Rows are never reversed;
    a later sighting opens its own hold and its charges are late charges."""
    bases = ", ".join(f"'{basis}'" for basis in RELEASE_BASES)
    db.execute(f"""CREATE TABLE IF NOT EXISTS experiment_budget_releases (
        admitted_attempt_id TEXT PRIMARY KEY
            REFERENCES experiment_budget_reservations(admitted_attempt_id),
        experiment_id TEXT NOT NULL REFERENCES experiment_budgets(experiment_id),
        frame_id TEXT NOT NULL UNIQUE,
        basis TEXT NOT NULL CHECK(basis IN ({bases})),
        released_cents INTEGER NOT NULL CHECK(typeof(released_cents)='integer'
            AND released_cents BETWEEN 0 AND 9007199254740991),
        evidence_digest TEXT NOT NULL CHECK(length(evidence_digest) > 0),
        released_at TEXT NOT NULL)""")
    for event in ("UPDATE", "DELETE"):
        name = f"experiment_budget_releases_no_{event.lower()}"
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS {name}
            BEFORE {event} ON experiment_budget_releases
            BEGIN SELECT RAISE(ABORT,'experiment budget releases are write-once'); END""")


def release_hold(db, *, admitted_attempt_id, frame_id, basis, evidence_digest, now):
    """Release one admitted attempt's experiment hold, at most once (caller's transaction).

    Returns the release row, or None when the attempt has no experiment reservation. A
    second release for the same attempt returns the stored row when it names the same frame
    and conflicts otherwise."""
    if basis not in RELEASE_BASES:
        raise Conflict(f"{basis} cannot release an experiment hold")
    if not evidence_digest:
        raise Conflict("an experiment hold release needs evidence")
    reservation = db.execute(
        "SELECT experiment_id, held_cents FROM experiment_budget_reservations "
        "WHERE admitted_attempt_id=?",
        (admitted_attempt_id,),
    ).fetchone()
    if reservation is None:
        return None
    prior = db.execute(
        "SELECT * FROM experiment_budget_releases WHERE admitted_attempt_id=?",
        (admitted_attempt_id,),
    ).fetchone()
    if prior is not None:
        if prior["frame_id"] != frame_id:
            raise Conflict("experiment hold was already released by another frame")
        return dict(prior)
    db.execute(
        "INSERT INTO experiment_budget_releases VALUES (?,?,?,?,?,?,?)",
        (
            admitted_attempt_id,
            reservation["experiment_id"],
            frame_id,
            basis,
            reservation["held_cents"],
            evidence_digest,
            now,
        ),
    )
    return dict(
        db.execute(
            "SELECT * FROM experiment_budget_releases WHERE admitted_attempt_id=?",
            (admitted_attempt_id,),
        ).fetchone()
    )


BOUND_FIELDS = ("prior_settled_cents", "prior_pending_cents", "ancillary_cents", "shutdown_cents")


def create_bound_resolution_schema(db: sqlite3.Connection) -> None:
    """Append-only evidence of one-shot NULL->amount bound resolutions (idempotent)."""
    db.execute("""CREATE TABLE IF NOT EXISTS experiment_budget_bound_resolutions (
        experiment_id TEXT NOT NULL REFERENCES experiment_budgets(experiment_id),
        revision INTEGER NOT NULL CHECK(revision>=2),
        field TEXT NOT NULL CHECK(field IN ('prior_settled_cents','prior_pending_cents',
            'ancillary_cents','shutdown_cents')),
        amount_cents INTEGER NOT NULL CHECK(typeof(amount_cents)='integer'
            AND amount_cents BETWEEN 0 AND 9007199254740991),
        evidence_digest TEXT NOT NULL, source_reference TEXT NOT NULL,
        resolved_at TEXT NOT NULL, actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        PRIMARY KEY(experiment_id,field))""")
    for event in ("UPDATE", "DELETE"):
        name = f"experiment_budget_bound_resolutions_no_{event.lower()}"
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS {name}
            BEFORE {event} ON experiment_budget_bound_resolutions
            BEGIN SELECT RAISE(ABORT,'bound resolutions are append-only'); END""")


def load_budget(db, actor, experiment_id):
    row = db.execute(
        """SELECT b.* FROM experiment_budgets b
        JOIN research_experiments e ON e.id=b.experiment_id
        JOIN projects p ON p.id=e.project_id WHERE b.experiment_id=? AND p.org_id=?
        AND (? IS NULL OR e.project_id=?)""",
        (experiment_id, actor["org_id"], actor.get("project_id"), actor.get("project_id")),
    ).fetchone()
    if row is None:
        raise NotFound("Experiment budget was not found.")
    return dict(row)


def budget_status(db, actor, experiment_id):
    budget = load_budget(db, actor, experiment_id)
    # A released hold (ADR 0041 Q3) stops counting; the reservation row is unchanged.
    holds = db.execute(
        "SELECT CASE WHEN rel.admitted_attempt_id IS NULL THEN r.held_cents ELSE 0 END,"
        "r.exposure_known FROM experiment_budget_reservations r "
        "LEFT JOIN experiment_budget_releases rel "
        "ON rel.admitted_attempt_id=r.admitted_attempt_id WHERE r.experiment_id=?",
        (experiment_id,),
    ).fetchall()
    charges = sum(
        row[0]
        for row in db.execute(
            "SELECT amount_cents FROM experiment_budget_entries WHERE experiment_id=?",
            (experiment_id,),
        )
    )
    bounds = [budget[k] for k in BOUND_FIELDS]
    unknown = None in bounds or any(not row[1] for row in holds)
    total = None if unknown else sum(bounds) + charges + sum(row[0] for row in holds)
    budget.update(
        accounted_exposure_cents=total,
        exposure_unknown=unknown,
        remaining_cents=None if total is None else max(0, budget["ceiling_cents"] - total),
        provider_hard_cap_verified=False,
    )
    return budget


def check_budget(db, actor, experiment_id, additional=0):
    budget = budget_status(db, actor, experiment_id)
    if (
        budget["state"] != "active"
        or budget["exposure_unknown"]
        or _parse_utc(budget["expires_at"]) <= datetime.now(UTC)
        or budget["accounted_exposure_cents"] + additional > budget["ceiling_cents"]
    ):
        raise Conflict("Experiment budget is expired, fenced, unknown or insufficient.")
    return budget


def authorize_budget(db, actor, experiment_id, body):
    # Caller validates current human authority and experiment ownership before entry.
    if (
        body["expected_revision"] != 0
        or db.execute(
            "SELECT 1 FROM experiment_budgets WHERE experiment_id=?", (experiment_id,)
        ).fetchone()
    ):
        raise Conflict("Experiment budget already exists; history cannot be reset.")
    if _parse_utc(body["expires_at"]) <= datetime.now(UTC):
        raise Conflict("Budget authorization must expire in the future.")
    # Earlier unbudgeted attempts (e.g. a CPU baseline) may precede a budget only when
    # all are terminal and the human declares their historical liability as known
    # amounts; their cost is carried in the prior_* bounds, never inferred as zero.
    states = {
        row[0]
        for row in db.execute(
            "SELECT state FROM research_experiment_admitted_attempts WHERE experiment_id=?",
            (experiment_id,),
        )
    }
    if states and (
        not states <= TERMINAL_ATTEMPT_STATES
        or body["prior_settled_cents"] is None
        or body["prior_pending_cents"] is None
    ):
        raise Conflict(
            "Existing attempts must be terminal with declared prior settled/pending costs."
        )
    db.execute(
        """INSERT INTO experiment_budgets VALUES
        (?, ?, 1, 1, 'USD', 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            experiment_id,
            body["ceiling_cents"],
            body["expires_at"],
            body["evidence_digest"],
            body["source_reference"],
            utc_now(),
            actor["id"],
            body["prior_settled_cents"],
            body["prior_pending_cents"],
            body["ancillary_cents"],
            body["shutdown_cents"],
        ),
    )
    return budget_status(db, actor, experiment_id)


def resolve_bounds(db, actor, experiment_id, body):
    """One-shot: set currently-NULL bounds to declared amounts; known bounds never change."""
    budget = load_budget(db, actor, experiment_id)
    if budget["revision"] != body["expected_revision"]:
        raise Conflict("Experiment budget revision is no longer current.")
    fields = {k: body[k] for k in BOUND_FIELDS if body.get(k) is not None}
    if not fields:
        raise Conflict("At least one bound must be resolved.")
    known = [k for k in fields if budget[k] is not None]
    if known:
        raise Conflict("A known bound cannot be changed: " + ", ".join(known))
    create_bound_resolution_schema(db)  # idempotent; covers pre-existing schema-57 databases
    revision = budget["revision"] + 1
    now = utc_now()
    for field, amount in fields.items():
        db.execute(
            "INSERT INTO experiment_budget_bound_resolutions VALUES (?,?,?,?,?,?,?,?)",
            (
                experiment_id,
                revision,
                field,
                amount,
                body["evidence_digest"],
                body["source_reference"],
                now,
                actor["id"],
            ),
        )
        db.execute(
            f"UPDATE experiment_budgets SET {field}=? WHERE experiment_id=?",
            (amount, experiment_id),
        )
    status = budget_status(db, actor, experiment_id)
    breached = (
        status["accounted_exposure_cents"] is not None
        and status["accounted_exposure_cents"] > status["ceiling_cents"]
    )
    # Unknown accounting cannot mask a known lower-bound breach.
    settled = sum(
        row[0]
        for row in db.execute(
            "SELECT amount_cents FROM experiment_budget_entries WHERE experiment_id=?",
            (experiment_id,),
        )
    ) + (status["prior_settled_cents"] or 0)
    db.execute(
        """UPDATE experiment_budgets SET revision=?,
        state=CASE WHEN ? THEN 'fenced' ELSE state END WHERE experiment_id=?""",
        (revision, breached or settled > budget["ceiling_cents"], experiment_id),
    )
    return budget_status(db, actor, experiment_id)


def reserve_attempt(db, actor, attempt, grant):
    """Must run in the SAME transaction as admitted-attempt/event/idempotency writes."""
    amount = grant["max_exposure_cents"]
    budget = check_budget(db, actor, attempt["experiment_id"], amount)
    db.execute(
        """INSERT INTO experiment_budget_reservations VALUES (?,?,?,?,?,?,0,1,1)""",
        (
            attempt["id"],
            attempt["experiment_id"],
            grant["id"],
            budget["authorization_revision"],
            amount,
            amount,
        ),
    )
    db.execute(
        "UPDATE experiment_budgets SET revision=revision+1 WHERE experiment_id=?",
        (attempt["experiment_id"],),
    )


def record_charge(db, actor, experiment_id, body):
    budget = load_budget(db, actor, experiment_id)
    digest = canonical_digest(body)
    prior = db.execute(
        "SELECT request_digest FROM experiment_budget_entries "
        "WHERE experiment_id=? AND delivery_id=?",
        (experiment_id, body["delivery_id"]),
    ).fetchone()
    if prior:
        if prior[0] != digest:
            raise Conflict("Charge delivery identity was reused with different content.")
        return budget_status(db, actor, experiment_id)
    if budget["revision"] != body["expected_revision"]:
        raise Conflict("Experiment budget revision is no longer current.")
    amount = body["amount_cents"]
    attempt_id = body["admitted_attempt_id"]
    if body["category"] == "compute":
        row = db.execute(
            "SELECT * FROM experiment_budget_reservations "
            "WHERE admitted_attempt_id=? AND experiment_id=?",
            (attempt_id, experiment_id),
        ).fetchone()
        if row is None:
            raise NotFound("Experiment reservation was not found.")
        # Never free residual liability; late charges beyond the hold remain recordable.
        db.execute(
            """UPDATE experiment_budget_reservations SET held_cents=?,settled_cents=?,
            revision=revision+1 WHERE admitted_attempt_id=?""",
            (max(0, row["held_cents"] - amount), row["settled_cents"] + amount, attempt_id),
        )
    else:
        if attempt_id is not None:
            raise Conflict("Non-compute charge cannot select an attempt.")
        column = {
            "prior": "prior_pending_cents",
            "ancillary": "ancillary_cents",
            "shutdown": "shutdown_cents",
        }[body["category"]]
        bound = budget[column]
        if bound is not None:
            db.execute(
                f"UPDATE experiment_budgets SET {column}=? WHERE experiment_id=?",
                (max(0, bound - amount), experiment_id),
            )
    db.execute(
        "INSERT INTO experiment_budget_entries VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            experiment_id,
            body["delivery_id"],
            digest,
            attempt_id,
            body["category"],
            amount,
            body["evidence_digest"],
            body["source_reference"],
            utc_now(),
            actor["id"],
        ),
    )
    status = budget_status(db, actor, experiment_id)
    breached = (
        status["accounted_exposure_cents"] is not None
        and status["accounted_exposure_cents"] > status["ceiling_cents"]
    )
    # Unknown accounting cannot mask a known lower-bound breach.
    settled = sum(
        row[0]
        for row in db.execute(
            "SELECT amount_cents FROM experiment_budget_entries WHERE experiment_id=?",
            (experiment_id,),
        )
    ) + (budget["prior_settled_cents"] or 0)
    db.execute(
        """UPDATE experiment_budgets SET revision=revision+1,
        state=CASE WHEN ? THEN 'fenced' ELSE state END WHERE experiment_id=?""",
        (breached or settled > budget["ceiling_cents"], experiment_id),
    )
    return budget_status(db, actor, experiment_id)


def salad_budget_binding(db, actor, proposal, operation):
    """Use inside live-authority transaction; never trust a request's parent balance."""
    from .experiment_budget_models import SaladBudgetBinding

    row = db.execute(
        """SELECT r.*,a.operation_id,a.package_digest,a.task_id FROM
        experiment_budget_reservations r JOIN research_experiment_admitted_attempts a
        ON a.id=r.admitted_attempt_id WHERE r.admitted_attempt_id=?""",
        (proposal["attempt_id"],),
    ).fetchone()
    if row is None:
        raise Conflict("Salad requires an authoritative experiment reservation.")
    budget = check_budget(db, actor, row["experiment_id"])
    # Legacy offline routes carry no executable profile; the full grant hold applies.
    execution = (proposal["selection"].get("salad") or {}).get("execution") or {}
    if (
        row["grant_id"] != operation["grant_id"]
        or row["operation_id"] != proposal["operation_id"]
        or row["package_digest"] != proposal["package_digest"]
        or row["task_id"] != operation["task_id"]
        or row["authorization_revision"] != budget["authorization_revision"]
        or not row["exposure_known"]
        or row["held_cents"] < execution.get("max_cost_cents", row["original_cents"])
    ):
        raise Conflict("Salad experiment reservation identity or remaining liability mismatch.")
    grant = db.execute("SELECT * FROM execution_grants WHERE id=?", (row["grant_id"],)).fetchone()
    if grant is None or grant["max_exposure_cents"] != row["original_cents"]:
        raise Conflict("Salad grant exposure changed.")
    binding = dict(
        experiment_id=row["experiment_id"],
        admitted_attempt_id=row["admitted_attempt_id"],
        grant_id=row["grant_id"],
        authorization_revision=row["authorization_revision"],
        operation_id=proposal["operation_id"],
        lease_id=proposal["lease_id"],
        epoch=proposal["epoch"],
        package_digest=proposal["package_digest"],
        max_exposure_cents=row["original_cents"],
        expires_at=budget["expires_at"],
        reservation_digest=canonical_digest(
            [
                row["experiment_id"],
                row["admitted_attempt_id"],
                row["grant_id"],
                row["authorization_revision"],
                row["original_cents"],
            ]
        ),
    )
    return SaladBudgetBinding.model_validate(binding).model_dump(mode="json")
