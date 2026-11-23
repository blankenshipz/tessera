"""Model-spend budget and ledger (ADR 0090): the control plane's own limit on model calls.

Model calls (the reasoning loop's calls to a registered model destination) were receipted and
capped inside the supervisor's private journal, but nothing in the control plane knew what they
cost. This is a separate budget, distinct from compute, Salad and grant budgets: an operator
(a human) sets per-call and per-UTC-day caps per project and provider; the supervisor reserves a
worst-case cost *before* each billed call and the call is refused, fail closed, when no limit is
configured or a cap would be exceeded; after the call it settles the actual cost with the
receipt's digest. The ledger never chooses or changes a model: a refusal is a refusal.

Units are integer micro-cents (1 cent = 1_000_000) so sums are exact. Nothing here stores a
prompt, a completion, a key or an upstream error body: only ids, the provider and model names,
amounts, the UTC day and the receipt's sha256.

A provider-specific limit row governs that provider alone; the ``*`` row governs the spend of
every provider that has no row of its own.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .approval_trust import require_decision_actor
from .external_agents import ExternalAgentStore
from .grant_model_spend import grant_refusal
from .store import Conflict, Forbidden, NotFound, StoreError, _connect, canonical_digest, utc_now


class RunIdRequired(StoreError):
    """An agent read a call without naming its run (the API answers 422)."""


MICRO = 1_000_000
DEFAULT_PROVIDER = "*"
SCOPE_RECORD = "model_spend:record"
SCOPE_READ = "model_spend:read"
MAX_CENTS = 10**9
STATUSES = ("reserved", "settled", "released")


def to_micro(cents: float | int | str) -> int:
    """Cents (as a JSON number) to integer micro-cents, rounded half up."""
    value = Decimal(str(cents)) * MICRO
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def to_cents(micro: int) -> float:
    return micro / MICRO


def create_model_spend_schema(connection: sqlite3.Connection) -> None:
    """Shared by the migration and fresh databases so both converge. Additive, idempotent."""
    for statement in (
        """CREATE TABLE IF NOT EXISTS model_spend_limits (
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        provider_id TEXT NOT NULL CHECK(length(provider_id) BETWEEN 1 AND 64),
        per_call_micro INTEGER NOT NULL CHECK(per_call_micro >= 0),
        daily_micro INTEGER NOT NULL CHECK(daily_micro >= 0),
        revision INTEGER NOT NULL CHECK(revision >= 1),
        updated_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        updated_at TEXT NOT NULL,
        PRIMARY KEY (org_id, project_id, provider_id))""",
        """CREATE TABLE IF NOT EXISTS model_spend_calls (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        run_id TEXT NOT NULL CHECK(length(run_id) BETWEEN 1 AND 200),
        seq INTEGER NOT NULL CHECK(seq >= 1),
        attempt INTEGER NOT NULL DEFAULT 1 CHECK(attempt >= 1),
        provider_id TEXT NOT NULL CHECK(length(provider_id) BETWEEN 1 AND 64),
        model TEXT NOT NULL CHECK(length(model) BETWEEN 1 AND 200),
        status TEXT NOT NULL CHECK(status IN ('reserved','settled','released')),
        utc_day TEXT NOT NULL CHECK(length(utc_day) = 10),
        reserved_micro INTEGER NOT NULL CHECK(reserved_micro >= 0),
        settled_micro INTEGER CHECK(settled_micro IS NULL OR settled_micro >= 0),
        receipt_sha256 TEXT CHECK(receipt_sha256 IS NULL OR length(receipt_sha256) = 64),
        actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        created_at TEXT NOT NULL,
        resolved_at TEXT,
        resolved_by_principal_id TEXT REFERENCES principals(id),
        resolution TEXT CHECK(resolution IS NULL OR resolution IN
            ('agent_settled','agent_released','person_released','person_settled')),
        resolution_reason TEXT CHECK(resolution_reason IS NULL OR length(resolution_reason) <= 500),
        UNIQUE (org_id, project_id, run_id, seq, attempt))""",
        """CREATE INDEX IF NOT EXISTS model_spend_calls_day
        ON model_spend_calls (org_id, project_id, utc_day, status)""",
        # A ledger row is never deleted, and only an open reservation moves, once, to settled or
        # released; its identity and what was reserved never change.
        """CREATE TRIGGER IF NOT EXISTS model_spend_calls_no_delete
        BEFORE DELETE ON model_spend_calls
        BEGIN SELECT RAISE(ABORT, 'model spend ledger rows are permanent'); END""",
        """CREATE TRIGGER IF NOT EXISTS model_spend_calls_closed
        BEFORE UPDATE ON model_spend_calls WHEN OLD.status <> 'reserved'
        BEGIN SELECT RAISE(ABORT, 'a resolved model spend call cannot change'); END""",
        """CREATE TRIGGER IF NOT EXISTS model_spend_calls_identity
        BEFORE UPDATE ON model_spend_calls WHEN
            NEW.id IS NOT OLD.id OR NEW.org_id IS NOT OLD.org_id
            OR NEW.project_id IS NOT OLD.project_id OR NEW.run_id IS NOT OLD.run_id
            OR NEW.seq IS NOT OLD.seq OR NEW.attempt IS NOT OLD.attempt
            OR NEW.provider_id IS NOT OLD.provider_id OR NEW.model IS NOT OLD.model
            OR NEW.utc_day IS NOT OLD.utc_day OR NEW.reserved_micro IS NOT OLD.reserved_micro
            OR NEW.actor_principal_id IS NOT OLD.actor_principal_id
            OR NEW.created_at IS NOT OLD.created_at
            OR NEW.status NOT IN ('settled','released')
        BEGIN SELECT RAISE(ABORT, 'a model spend call keeps its identity'); END""",
    ):
        connection.execute(statement)


def _limit_row(connection, org_id: str, project_id: str, provider_id: str):
    for candidate in (provider_id, DEFAULT_PROVIDER):
        row = connection.execute(
            """SELECT * FROM model_spend_limits
            WHERE org_id=? AND project_id=? AND provider_id=?""",
            (org_id, project_id, candidate),
        ).fetchone()
        if row is not None:
            return row
    return None


def _governed_totals(connection, limit, org_id: str, project_id: str, day: str) -> tuple[int, int]:
    """(settled, reserved) micro-cents of the UTC day for the providers ``limit`` governs."""
    if limit["provider_id"] == DEFAULT_PROVIDER:
        scope = """provider_id NOT IN (SELECT provider_id FROM model_spend_limits
                   WHERE org_id=? AND project_id=? AND provider_id<>'*')"""
        arguments: tuple[Any, ...] = (org_id, project_id, day, org_id, project_id)
    else:
        scope = "provider_id=?"
        arguments = (org_id, project_id, day, limit["provider_id"])
    row = connection.execute(
        f"""SELECT
        COALESCE(SUM(CASE WHEN status='settled' THEN settled_micro END), 0) AS settled,
        COALESCE(SUM(CASE WHEN status='reserved' THEN reserved_micro END), 0) AS reserved
        FROM model_spend_calls WHERE org_id=? AND project_id=? AND utc_day=? AND {scope}""",
        arguments,
    ).fetchone()
    return int(row["settled"]), int(row["reserved"])


def _limits_view(row) -> dict[str, Any]:
    return {
        "per_call_cents": to_cents(row["per_call_micro"]),
        "daily_cents": to_cents(row["daily_micro"]),
    }


def _reservation_view(row, limit, daily: tuple[int, int]) -> dict[str, Any]:
    return {
        "allowed": True,
        "call_id": row["id"],
        "status": row["status"],
        "reserved_micro_cents": row["reserved_micro"],
        "settled_micro_cents": row["settled_micro"],
        "day": row["utc_day"],
        "limits": _limits_view(limit),
        "daily": {"settled_cents": to_cents(daily[0]), "reserved_cents": to_cents(daily[1])},
    }


class ModelSpendStore(ExternalAgentStore):
    """Limits (human only), reservations and settlements (an enrolled agent), reads."""

    # -- helpers ------------------------------------------------------------------------------

    def _call(self, connection, actor, call_id: str):
        row = connection.execute(
            "SELECT * FROM model_spend_calls WHERE id=? AND org_id=?", (call_id, actor["org_id"])
        ).fetchone()
        if row is None:
            raise NotFound("Model spend call was not found.")
        self._project(connection, actor, row["project_id"])
        return row

    def _event_payload(self, row, **extra: Any) -> dict[str, Any]:
        return {
            "call_id": row["id"],
            "run_id": row["run_id"],
            "seq": row["seq"],
            "provider": row["provider_id"],
            "model": row["model"],
            "day": row["utc_day"],
            **extra,
        }

    # -- limits -------------------------------------------------------------------------------

    def set_limits(self, principal, key: str, payload: dict[str, Any]) -> tuple[int, dict]:
        project_id = payload["project_id"]
        provider_id = payload["provider_id"]
        per_call, daily = to_micro(payload["per_call_cents"]), to_micro(payload["daily_cents"])
        if per_call > daily:
            raise StoreError("per_call_cents cannot exceed daily_cents.")

        def command(connection, actor):
            if actor["role"] != "human":
                raise Forbidden("Only a human may set model spend limits.")
            require_decision_actor(actor, forbidden=Forbidden)
            self._project(connection, actor, project_id)
            now = utc_now()
            row = connection.execute(
                """SELECT revision FROM model_spend_limits
                WHERE org_id=? AND project_id=? AND provider_id=?""",
                (actor["org_id"], project_id, provider_id),
            ).fetchone()
            revision = 1 if row is None else row["revision"] + 1
            connection.execute(
                """INSERT INTO model_spend_limits VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(org_id, project_id, provider_id) DO UPDATE SET
                per_call_micro=excluded.per_call_micro, daily_micro=excluded.daily_micro,
                revision=excluded.revision,
                updated_by_principal_id=excluded.updated_by_principal_id,
                updated_at=excluded.updated_at""",
                (
                    actor["org_id"],
                    project_id,
                    provider_id,
                    per_call,
                    daily,
                    revision,
                    actor["id"],
                    now,
                ),
            )
            self._event(
                connection,
                actor,
                project_id,
                None,
                "model_spend.limits_set",
                revision,
                {
                    "provider": provider_id,
                    "per_call_micro_cents": per_call,
                    "daily_micro_cents": daily,
                },
            )
            return 200, {
                "project_id": project_id,
                "provider_id": provider_id,
                "per_call_cents": to_cents(per_call),
                "daily_cents": to_cents(daily),
                "revision": revision,
                "updated_at": now,
            }

        return self._mutate(
            principal, key, "PUT", "/model-spend/limits", canonical_digest(payload), command
        )

    # -- reservation ----------------------------------------------------------------------------

    def reserve(self, principal, key: str, payload: dict[str, Any]) -> tuple[int, dict]:
        project_id = payload["project_id"]
        worst = to_micro(payload["worst_case_cents"])

        def command(connection, actor):
            self._scope(connection, actor, SCOPE_RECORD)
            self._project(connection, actor, project_id)
            org = actor["org_id"]
            existing = connection.execute(
                """SELECT * FROM model_spend_calls WHERE org_id=? AND project_id=? AND run_id=?
                AND seq=? ORDER BY attempt DESC LIMIT 1""",
                (org, project_id, payload["run_id"], payload["seq"]),
            ).fetchone()
            attempt = 1
            if existing is not None:
                if (existing["provider_id"], existing["model"]) != (
                    payload["provider_id"],
                    payload["model"],
                ):
                    raise Conflict("This run and sequence were reserved for another model.")
                if existing["status"] != "released":
                    limit = _limit_row(connection, org, project_id, existing["provider_id"])
                    day = _governed_totals(connection, limit, org, project_id, existing["utc_day"])
                    return 200, _reservation_view(existing, limit, day)
                attempt = existing["attempt"] + 1  # a released call (not sent) may be retried
            limit = _limit_row(connection, org, project_id, payload["provider_id"])
            now = utc_now()
            day_key = now[:10]
            refusal = None
            if limit is None:
                refusal = {"code": "model_spend_not_configured"}
            else:
                settled, reserved = _governed_totals(connection, limit, org, project_id, day_key)
                if worst > limit["per_call_micro"]:
                    refusal = {
                        "code": "model_spend_cap_exceeded",
                        "cap": "per_call",
                        "limit_cents": to_cents(limit["per_call_micro"]),
                        "would_be_cents": to_cents(worst),
                    }
                elif settled + reserved + worst > limit["daily_micro"]:
                    refusal = {
                        "code": "model_spend_cap_exceeded",
                        "cap": "daily",
                        "limit_cents": to_cents(limit["daily_micro"]),
                        "would_be_cents": to_cents(settled + reserved + worst),
                    }
            grant_id = payload.get("grant_id")
            if grant_id is not None:
                # ADR 0142: the grant that pays for the call must be the caller's own, in this
                # project, active, and have the worst case left. Judged with the ledger caps.
                grant_refused, grant_row = grant_refusal(
                    connection,
                    actor,
                    project_id,
                    grant_id,
                    worst,
                    payload["provider_id"],
                    payload["model"],
                )
                if grant_row is None:
                    raise NotFound("Agent authority grant was not found.")
                if refusal is None:
                    refusal = grant_refused
            if refusal is not None:
                self._event(
                    connection,
                    actor,
                    project_id,
                    None,
                    "model_spend.refused",
                    1,
                    {
                        "run_id": payload["run_id"],
                        "seq": payload["seq"],
                        "provider": payload["provider_id"],
                        "model": payload["model"],
                        "day": day_key,
                        "worst_case_micro_cents": worst,
                        **refusal,
                    },
                )
                return 200, {"allowed": False, "day": day_key, **refusal}
            ident = "modelspend_" + uuid.uuid4().hex
            connection.execute(
                """INSERT INTO model_spend_calls (id, org_id, project_id, run_id, seq, attempt,
                provider_id, model, status, utc_day, reserved_micro, actor_principal_id, created_at)
                VALUES (?,?,?,?,?,?,?,?,'reserved',?,?,?,?)""",
                (
                    ident,
                    org,
                    project_id,
                    payload["run_id"],
                    payload["seq"],
                    attempt,
                    payload["provider_id"],
                    payload["model"],
                    day_key,
                    worst,
                    actor["id"],
                    now,
                ),
            )
            if grant_id is not None:
                connection.execute(
                    "INSERT INTO model_spend_grant_links (call_id, org_id, grant_id, created_at) "
                    "VALUES (?,?,?,?)",
                    (ident, org, grant_id, now),
                )
            row = connection.execute(
                "SELECT * FROM model_spend_calls WHERE id=?", (ident,)
            ).fetchone()
            self._event(
                connection,
                actor,
                project_id,
                None,
                "model_spend.reserved",
                1,
                self._event_payload(
                    row,
                    reserved_micro_cents=worst,
                    attempt=attempt,
                    # The receipt names the grant that pays for the call (ADR 0142).
                    **({"grant_id": grant_id} if grant_id is not None else {}),
                ),
            )
            totals = _governed_totals(connection, limit, org, project_id, day_key)
            return 200, _reservation_view(row, limit, totals)

        return self._mutate(
            principal,
            key,
            "POST",
            "/model-spend/reservations",
            canonical_digest(payload),
            command,
        )

    # -- settlement and release -----------------------------------------------------------------

    def settle(self, principal, call_id: str, key: str, payload: dict[str, Any]):
        actual = to_micro(payload["actual_cents"])

        def command(connection, actor):
            self._scope(connection, actor, SCOPE_RECORD)
            row = self._call(connection, actor, call_id)
            limit = _limit_row(connection, row["org_id"], row["project_id"], row["provider_id"])
            if row["status"] == "released":
                raise Conflict("The call was released and cannot be settled.")
            if row["status"] == "settled":
                if (row["settled_micro"], row["receipt_sha256"]) != (
                    actual,
                    payload["receipt_sha256"],
                ):
                    raise Conflict("The call is already settled with different values.")
            else:
                now = utc_now()
                connection.execute(
                    """UPDATE model_spend_calls SET status='settled', settled_micro=?,
                    receipt_sha256=?, resolved_at=?, resolved_by_principal_id=?,
                    resolution='agent_settled' WHERE id=?""",
                    (actual, payload["receipt_sha256"], now, actor["id"], call_id),
                )
                row = connection.execute(
                    "SELECT * FROM model_spend_calls WHERE id=?", (call_id,)
                ).fetchone()
                self._event(
                    connection,
                    actor,
                    row["project_id"],
                    None,
                    "model_spend.settled",
                    1,
                    self._event_payload(
                        row,
                        reserved_micro_cents=row["reserved_micro"],
                        settled_micro_cents=actual,
                        receipt_sha256=payload["receipt_sha256"],
                    ),
                )
            exceeded = False
            daily = (0, 0)
            if limit is not None:
                daily = _governed_totals(
                    connection, limit, row["org_id"], row["project_id"], row["utc_day"]
                )
                exceeded = actual > limit["per_call_micro"] or daily[0] > limit["daily_micro"]
            return 200, {
                "call_id": call_id,
                "status": "settled",
                "settled_micro_cents": row["settled_micro"],
                "reserved_micro_cents": row["reserved_micro"],
                "receipt_sha256": row["receipt_sha256"],
                "day": row["utc_day"],
                "over_reservation": row["settled_micro"] > row["reserved_micro"],
                "cap_exceeded": exceeded,
                "daily": {
                    "settled_cents": to_cents(daily[0]),
                    "reserved_cents": to_cents(daily[1]),
                },
            }

        return self._mutate(
            principal,
            key,
            "POST",
            f"/model-spend/reservations/{call_id}/settlement",
            canonical_digest(payload),
            command,
        )

    def release(self, principal, call_id: str, key: str, payload: dict[str, Any]):
        def command(connection, actor):
            self._scope(connection, actor, SCOPE_RECORD)
            row = self._call(connection, actor, call_id)
            if row["status"] == "settled":
                raise Conflict("A settled call cannot be released.")
            if row["status"] == "reserved":
                connection.execute(
                    """UPDATE model_spend_calls SET status='released', resolved_at=?,
                    resolved_by_principal_id=?, resolution='agent_released' WHERE id=?""",
                    (utc_now(), actor["id"], call_id),
                )
                row = connection.execute(
                    "SELECT * FROM model_spend_calls WHERE id=?", (call_id,)
                ).fetchone()
                self._event(
                    connection,
                    actor,
                    row["project_id"],
                    None,
                    "model_spend.released",
                    1,
                    self._event_payload(
                        row, reserved_micro_cents=row["reserved_micro"], reason=payload["reason"]
                    ),
                )
            return 200, {"call_id": call_id, "status": "released", "day": row["utc_day"]}

        return self._mutate(
            principal,
            key,
            "POST",
            f"/model-spend/reservations/{call_id}/release",
            canonical_digest(payload),
            command,
        )

    # -- resolution by a person -------------------------------------------------------------------

    def resolve(self, principal, call_id: str, key: str, payload: dict[str, Any]):
        """A person closes an open reservation the supervisor could not (an uncertain call).

        ``not_sent`` releases it (the day's budget is free again); ``spent`` settles it at the
        amount the person states. The reason is recorded on the call and on a control event."""
        outcome = payload["outcome"]
        spent = outcome == "spent"
        actual = to_micro(payload["actual_cents"]) if spent else None
        want = "person_settled" if spent else "person_released"

        def command(connection, actor):
            if actor["role"] != "human":
                raise Forbidden("Only a human may resolve a model spend reservation.")
            require_decision_actor(actor, forbidden=Forbidden)
            row = self._call(connection, actor, call_id)
            if row["status"] != "reserved":
                if row["resolution"] != want or (spent and row["settled_micro"] != actual):
                    raise Conflict("The call is already resolved differently.")
            else:
                connection.execute(
                    """UPDATE model_spend_calls SET status=?, settled_micro=?, resolved_at=?,
                    resolved_by_principal_id=?, resolution=?, resolution_reason=? WHERE id=?""",
                    (
                        "settled" if spent else "released",
                        actual,
                        utc_now(),
                        actor["id"],
                        want,
                        payload["reason"],
                        call_id,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM model_spend_calls WHERE id=?", (call_id,)
                ).fetchone()
                self._event(
                    connection,
                    actor,
                    row["project_id"],
                    None,
                    "model_spend.resolved",
                    1,
                    self._event_payload(
                        row,
                        outcome=outcome,
                        resolution=want,
                        reserved_micro_cents=row["reserved_micro"],
                        settled_micro_cents=row["settled_micro"] or 0,
                        reason=payload["reason"],
                    ),
                )
            limit = _limit_row(connection, row["org_id"], row["project_id"], row["provider_id"])
            daily, exceeded = (0, 0), False
            if limit is not None:
                daily = _governed_totals(
                    connection, limit, row["org_id"], row["project_id"], row["utc_day"]
                )
                if spent:
                    exceeded = actual > limit["per_call_micro"] or daily[0] > limit["daily_micro"]
            return 200, {
                "call_id": call_id,
                "status": row["status"],
                "settled_micro_cents": row["settled_micro"] or 0,
                "resolution": row["resolution"],
                "over_reservation": bool(spent and row["settled_micro"] > row["reserved_micro"]),
                "cap_exceeded": exceeded,
                "day": row["utc_day"],
                "daily": {
                    "settled_cents": to_cents(daily[0]),
                    "reserved_cents": to_cents(daily[1]),
                },
            }

        return self._mutate(
            principal,
            key,
            "POST",
            f"/model-spend/reservations/{call_id}/resolution",
            canonical_digest(payload),
            command,
        )

    # -- reads ------------------------------------------------------------------------------------

    def reservation(self, principal, call_id: str, run_id: str | None = None) -> dict[str, Any]:
        """The state of one call, so a supervisor can see a person's resolution. Read only.

        A human, or an enrolled agent with ``model_spend:record`` for its own project. Only the
        status, who-kind of resolution, the settled amount and the day are returned: no reason
        text, no run content, no key.

        An agent may read only a call of its own run: it must name ``run_id`` (a missing one is a
        422) and a call of another run answers 404, the same as an unknown call. A human may omit
        it; if given it must match."""
        connection = _connect(self.database_path)
        try:
            actor = self._actor(connection, principal)
            if actor["kind"] == "worker":
                raise Forbidden("Workers cannot read model spend.")
            if actor["role"] == "agent":
                self._scope(connection, actor, SCOPE_RECORD)
            elif actor["role"] != "human":
                raise Forbidden("A human or a model_spend:record agent is required.")
            if actor["role"] == "agent" and run_id is None:
                raise RunIdRequired("An agent must name the run_id of the call it reads.")
            row = self._call(connection, actor, call_id)
            if run_id is not None and row["run_id"] != run_id:
                raise NotFound("Model spend call was not found.")
            return {
                "call_id": row["id"],
                "status": row["status"],
                "resolution": row["resolution"],
                "settled_micro_cents": row["settled_micro"],
                "day": row["utc_day"],
            }
        finally:
            connection.close()

    def read(self, principal, project_id: str, days: int) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            actor = self._actor(connection, principal)
            if actor["kind"] == "worker":
                raise Forbidden("Workers cannot read model spend.")
            self._project(connection, actor, project_id)
            if actor["role"] == "agent":
                self._scope(connection, actor, SCOPE_READ)
            elif actor["role"] != "human":
                raise Forbidden("A human or a model_spend:read agent is required.")
            org = actor["org_id"]
            today = utc_now()[:10]
            limits = [
                {
                    "provider_id": r["provider_id"],
                    "revision": r["revision"],
                    **_limits_view(r),
                }
                for r in connection.execute(
                    """SELECT * FROM model_spend_limits WHERE org_id=? AND project_id=?
                    ORDER BY provider_id""",
                    (org, project_id),
                )
            ]
            totals = connection.execute(
                """SELECT COALESCE(SUM(CASE WHEN status='settled' THEN settled_micro END),0) AS s,
                COALESCE(SUM(CASE WHEN status='reserved' THEN reserved_micro END),0) AS r,
                SUM(CASE WHEN status<>'released' THEN 1 ELSE 0 END) AS n
                FROM model_spend_calls WHERE org_id=? AND project_id=? AND utc_day=?""",
                (org, project_id, today),
            ).fetchone()
            since = (date.fromisoformat(today) - timedelta(days=days - 1)).isoformat()
            by_day = [
                {"day": r["utc_day"], "settled_cents": to_cents(r["s"]), "calls": r["n"]}
                for r in connection.execute(
                    """SELECT utc_day, SUM(settled_micro) AS s, COUNT(*) AS n FROM model_spend_calls
                    WHERE org_id=? AND project_id=? AND status='settled' AND utc_day>=?
                    GROUP BY utc_day ORDER BY utc_day DESC""",
                    (org, project_id, since),
                )
            ]
            by_provider = {
                r["provider_id"]: {"settled_cents": to_cents(r["s"]), "calls": r["n"]}
                for r in connection.execute(
                    """SELECT provider_id, SUM(settled_micro) AS s, COUNT(*) AS n
                    FROM model_spend_calls WHERE org_id=? AND project_id=? AND status='settled'
                    AND utc_day>=? GROUP BY provider_id""",
                    (org, project_id, since),
                )
            }
            open_rows = [
                {
                    "call_id": r["id"],
                    "run_id": r["run_id"],
                    "seq": r["seq"],
                    "provider_id": r["provider_id"],
                    "model": r["model"],
                    "reserved_cents": to_cents(r["reserved_micro"]),
                    "day": r["utc_day"],
                }
                for r in connection.execute(
                    """SELECT * FROM model_spend_calls WHERE org_id=? AND project_id=?
                    AND status='reserved' ORDER BY created_at LIMIT 100""",
                    (org, project_id),
                )
            ]
            return {
                "project_id": project_id,
                "day": today,
                "limits": limits,
                "today": {
                    "settled_cents": to_cents(totals["s"]),
                    "reserved_cents": to_cents(totals["r"]),
                    "calls": totals["n"] or 0,
                },
                "by_day": by_day,
                "by_provider": by_provider,
                "open_reservations": open_rows,
            }
        finally:
            connection.close()
